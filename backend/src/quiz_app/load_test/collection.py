from __future__ import annotations

import gzip
import io
import os
import tarfile
import zlib
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from quiz_app.load_test.collection_inventory import (
    MAX_COLLECTION_BYTES,
    MAX_COLLECTION_FILES,
    CollectedFile,
    allowed_path,
    collect_inventory,
    validate_inventory,
)
from quiz_app.load_test.controller_contracts import canonical_json_bytes, manifest_context
from quiz_app.load_test.db_verifier_artifacts import read_secure_external_file
from quiz_app.load_test.manifest import LoadTestManifest
from quiz_app.load_test.orchestrator_contracts import (
    OrchestratorError,
    RunFiles,
    RunResult,
    StrictRunModel,
    require_private_directory,
)
from quiz_app.load_test.orchestrator_process import RunClock
from quiz_app.load_test.orchestrator_remote import ArchiveObject, evidence_bucket
from quiz_app.load_test.verification_contracts import ArtifactIdentity, Sha256
from quiz_app.load_test_aws.aws import (
    ApiModel,
    AwsApi,
    AwsCallError,
    EmptyResponse,
    require_account,
)
from quiz_app.load_test_aws.models import AwsPreparationEvidence

ARCHIVE_KEY = "evidence/runner/archive.tar.gz"
ACK_KEY = "evidence/runner/collected.json"
_INDEX_NAME = "inventory.json"
_TAR_LIMIT = MAX_COLLECTION_BYTES + 1024 * 1024


class ListedAck(ApiModel):
    key: Literal["evidence/runner/collected.json"] = Field(alias="Key")


class AckListing(ApiModel):
    is_truncated: Literal[False] = Field(alias="IsTruncated")
    contents: tuple[ListedAck, ...] = Field(default=(), alias="Contents", max_length=1)


class CollectionItem(StrictRunModel):
    path: str = Field(pattern=r"^[a-z-]+/[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
    artifact: ArtifactIdentity


class CollectionIndex(StrictRunModel):
    format: Literal["load-test-collection-index-v1"] = "load-test-collection-index-v1"
    run_id: UUID
    manifest_sha256: Sha256
    artifacts: tuple[CollectionItem, ...] = Field(min_length=3, max_length=MAX_COLLECTION_FILES)


class CollectionAck(StrictRunModel):
    format: Literal["load-test-collection-ack-v1"] = "load-test-collection-ack-v1"
    run_id: UUID
    manifest_sha256: Sha256
    archive_sha256: Sha256
    inventory_sha256: Sha256
    artifact_count: int = Field(ge=3, le=MAX_COLLECTION_FILES)
    collected_at: AwareDatetime


class CollectionBundle(StrictRunModel):
    body: bytes
    index: CollectionIndex
    index_sha256: Sha256

    @property
    def sha256(self) -> str:
        return sha256(self.body).hexdigest()


def collection_deadline(
    manifest: LoadTestManifest, prepared: AwsPreparationEvidence, finished_at: datetime
) -> datetime:
    """回収は最長10分。試験開始90分とAWS期限30秒前を越えない。"""
    return min(
        finished_at + timedelta(seconds=600),
        manifest.scheduled_start_at + timedelta(seconds=5400),
        prepared.expires_at - timedelta(seconds=30),
    )


def build_bundle(
    items: tuple[CollectedFile, ...], manifest: LoadTestManifest, prepared: AwsPreparationEvidence
) -> CollectionBundle:
    validate_inventory(items, manifest, prepared)
    index = CollectionIndex(
        run_id=manifest.run_id,
        manifest_sha256=prepared.manifest_sha256,
        artifacts=tuple(CollectionItem(path=item.path, artifact=item.identity) for item in items),
    )
    index_body = canonical_json_bytes(index) + b"\n"
    body = gzip.compress(_tar_bytes(index_body, items), mtime=0)
    if len(body) > _TAR_LIMIT:
        raise OrchestratorError("collection_archive_too_large")
    return CollectionBundle(body=body, index=index, index_sha256=sha256(index_body).hexdigest())


def _tar_bytes(index_body: bytes, items: tuple[CollectedFile, ...]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:", format=tarfile.USTAR_FORMAT) as archive:
        for name, body in ((_INDEX_NAME, index_body), *((item.path, item.body) for item in items)):
            member = tarfile.TarInfo(name)
            member.size = len(body)
            member.mode = 0o600
            archive.addfile(member, io.BytesIO(body))
    return output.getvalue()


def _get(api: AwsApi, prepared: AwsPreparationEvidence, key: str, maximum: int) -> bytes:
    response = api.call(
        "s3",
        "get_object",
        ArchiveObject,
        Bucket=evidence_bucket(prepared),
        Key=key,
        ExpectedBucketOwner=prepared.account_id,
    )
    try:
        if response.length > maximum:
            raise OrchestratorError("collection_object_too_large")
        body = response.body.read(maximum + 1)
    finally:
        response.body.close()
    if len(body) != response.length or sha256(body).hexdigest() != response.metadata.get("sha256"):
        raise OrchestratorError("collection_object_checksum_invalid")
    return body


def _put(api: AwsApi, prepared: AwsPreparationEvidence, key: str, body: bytes) -> None:
    api.call(
        "s3",
        "put_object",
        EmptyResponse,
        Bucket=evidence_bucket(prepared),
        Key=key,
        Body=body,
        ContentType="application/gzip" if key == ARCHIVE_KEY else "application/json",
        ServerSideEncryption="AES256",
        ExpectedBucketOwner=prepared.account_id,
        IfNoneMatch="*",
        Metadata={"sha256": sha256(body).hexdigest()},
    )


def publish_and_wait(
    api: AwsApi,
    files: RunFiles,
    manifest: LoadTestManifest,
    prepared: AwsPreparationEvidence,
    clock: RunClock,
) -> CollectionAck:
    """ACKのrun・manifest・全archive hashを照合するまで回収成功にしない。"""
    clock.check()
    bundle = build_bundle(collect_inventory(files, manifest, prepared), manifest, prepared)
    clock.check()
    _put(api, prepared, ARCHIVE_KEY, bundle.body)
    while True:
        clock.check()
        # ListBucketはこのprefixだけを許可する。未作成GETの403を未回収と誤分類しない。
        listed = api.call(
            "s3",
            "list_objects_v2",
            AckListing,
            Bucket=evidence_bucket(prepared),
            Prefix=ACK_KEY,
            MaxKeys=2,
            ExpectedBucketOwner=prepared.account_id,
        )
        if not listed.contents:
            clock.sleep(min(2, (clock.deadline - clock.now()).total_seconds()))
            continue
        clock.check()
        body = _get(api, prepared, ACK_KEY, 4096)
        ack = CollectionAck.model_validate_json(body)
        if (
            ack.run_id != prepared.run_id
            or ack.manifest_sha256 != prepared.manifest_sha256
            or ack.archive_sha256 != bundle.sha256
            or ack.inventory_sha256 != bundle.index_sha256
            or ack.artifact_count != len(bundle.index.artifacts)
            or not prepared.prepared_at <= ack.collected_at <= clock.now()
        ):
            raise OrchestratorError("collection_ack_binding_invalid")
        clock.check()
        return ack


def unpack_bundle(
    body: bytes, manifest: LoadTestManifest, prepared: AwsPreparationEvidence
) -> tuple[CollectionBundle, tuple[CollectedFile, ...], RunResult]:
    """extractallを使わず、boundedなregular fileと完全一致するinventoryだけを扱う。"""
    if len(body) > _TAR_LIMIT:
        raise OrchestratorError("collection_archive_too_large")
    try:
        # producerは任意filename/commentなし・mtime=0の単一gzip memberを作る。
        if len(body) < 18 or body[:8] != b"\x1f\x8b\x08\x00\x00\x00\x00\x00":
            raise OrchestratorError("collection_archive_noncanonical")
        decoder = zlib.decompressobj(wbits=31)
        expanded = decoder.decompress(body, _TAR_LIMIT + 1)
        if len(expanded) > _TAR_LIMIT:
            raise OrchestratorError("collection_archive_too_large")
        if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise OrchestratorError("collection_archive_noncanonical")
        bodies: dict[str, bytes] = {}
        with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
            for member in archive:
                if (
                    len(bodies) > MAX_COLLECTION_FILES
                    or member.name in bodies
                    or not member.isfile()
                    or member.issparse()
                    or member.pax_headers
                    or not 0 < member.size <= MAX_COLLECTION_BYTES
                    or (member.name != _INDEX_NAME and not allowed_path(member.name))
                ):
                    raise OrchestratorError("collection_archive_member_invalid")
                source = archive.extractfile(member)
                if source is None:
                    raise OrchestratorError("collection_archive_member_invalid")
                with source:
                    content = source.read(member.size + 1)
                if len(content) != member.size:
                    raise OrchestratorError("collection_archive_member_invalid")
                bodies[member.name] = content
        index_body = bodies.pop(_INDEX_NAME)
        index = CollectionIndex.model_validate_json(index_body)
    except (KeyError, tarfile.TarError, gzip.BadGzipFile, EOFError, zlib.error):
        raise OrchestratorError("collection_archive_invalid") from None
    if (
        index.run_id != prepared.run_id
        or index.manifest_sha256 != prepared.manifest_sha256
        or len(index.artifacts) != len(bodies)
        or len({item.path for item in index.artifacts}) != len(bodies)
        or {item.path for item in index.artifacts} != set(bodies)
    ):
        raise OrchestratorError("collection_inventory_mismatch")
    items = tuple(CollectedFile(path=item.path, body=bodies[item.path]) for item in index.artifacts)
    if any(
        item.identity != expected.artifact
        for item, expected in zip(items, index.artifacts, strict=True)
    ):
        raise OrchestratorError("collection_artifact_checksum_invalid")
    # header・EOF後・indexの重複keyへ、inventoryにないbytesを隠す経路も拒否する。
    if index_body != canonical_json_bytes(index) + b"\n" or expanded != _tar_bytes(
        index_body, items
    ):
        raise OrchestratorError("collection_archive_noncanonical")
    result = validate_inventory(items, manifest, prepared)
    return (
        CollectionBundle(body=body, index=index, index_sha256=sha256(index_body).hexdigest()),
        items,
        result,
    )


def _write_new(path: Path, body: bytes) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(body)
        output.flush()
        os.fsync(output.fileno())


def collect_locally(
    api: AwsApi,
    manifest: LoadTestManifest,
    prepared: AwsPreparationEvidence,
    directory: Path,
    repository_root: Path,
    clock: RunClock,
) -> RunResult:
    """新規0700へ全証跡を永続化・再読込し、その後だけAWSへ回収ACKを書く。"""
    if prepared.manifest_sha256 != manifest_context(manifest).manifest_sha256:
        raise OrchestratorError("collection_binding_invalid")
    require_account(api, prepared.account_id)
    directory.mkdir(mode=0o700)
    require_private_directory(directory, repository_root)
    while True:
        clock.check()
        try:
            body = _get(api, prepared, ARCHIVE_KEY, _TAR_LIMIT)
            break
        except AwsCallError as error:
            if error.code != "NoSuchKey":
                raise
            clock.sleep(min(2, (clock.deadline - clock.now()).total_seconds()))
    bundle, items, result = unpack_bundle(body, manifest, prepared)
    for item in items:
        clock.check()
        path = directory / item.path
        path.parent.mkdir(mode=0o700, exist_ok=True)
        require_private_directory(path.parent, repository_root)
        _write_new(path, item.body)
    _write_new(directory / "inventory.json", canonical_json_bytes(bundle.index) + b"\n")
    _write_new(directory / "archive.tar.gz", bundle.body)
    _write_new(directory / "archive.sha256", f"{bundle.sha256}  archive.tar.gz\n".encode("ascii"))
    reread = tuple(
        CollectedFile(
            path=item.path,
            body=read_secure_external_file(directory / item.path, maximum_bytes=256 * 1024 * 1024)[
                0
            ],
        )
        for item in items
    )
    if any(
        actual.identity != expected.identity for actual, expected in zip(reread, items, strict=True)
    ):
        raise OrchestratorError("collection_local_artifact_changed")
    validate_inventory(reread, manifest, prepared)
    # fileだけでなく各directoryのentryもACKより前に永続化する。
    folders = {directory.parent, directory, *((directory / item.path).parent for item in items)}
    for folder in folders:
        descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    clock.check()
    if clock.now() >= collection_deadline(manifest, prepared, result.finished_at):
        raise OrchestratorError("collection_deadline_exceeded")
    ack = CollectionAck(
        run_id=prepared.run_id,
        manifest_sha256=prepared.manifest_sha256,
        archive_sha256=bundle.sha256,
        inventory_sha256=bundle.index_sha256,
        artifact_count=len(items),
        collected_at=clock.now(),
    )
    ack_body = canonical_json_bytes(ack) + b"\n"
    _write_new(directory / "collection-ack.json", ack_body)
    _put(api, prepared, ACK_KEY, ack_body)
    return result
