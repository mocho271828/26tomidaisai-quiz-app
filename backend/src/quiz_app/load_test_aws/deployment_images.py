"""OCI manifestを再serializeせず、検証した同一bytesだけをrun専用ECRへ送る。"""

from __future__ import annotations

import os
import re
import tarfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Annotated, BinaryIO, Literal

from pydantic import BaseModel, ConfigDict, Field

from quiz_app.aws_demo_paths import RUNTIME_ENVIRONMENT_FILE_POLICY, validate_external_regular_file
from quiz_app.load_test.controller_contracts import manifest_context
from quiz_app.load_test.manifest import LoadTestManifest
from quiz_app.load_test_aws.aws import ApiModel, AwsApi, AwsCallError
from quiz_app.load_test_aws.deployment_api import deployment_preflight
from quiz_app.load_test_aws.lifecycle import output
from quiz_app.load_test_aws.models import AwsRunPlan
from quiz_app.load_test_aws.responses import Stack

MAX_ARCHIVE_BYTES = 1500 * 1024 * 1024
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_PART_BYTES = 20 * 1024 * 1024
MIN_PART_BYTES = 5 * 1024 * 1024
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_LAYERS = {
    "application/vnd.oci.image.layer.v1.tar",
    "application/vnd.oci.image.layer.v1.tar+gzip",
}
DIGEST = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
COMMIT = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
ImageKind = Literal["app", "gateway"]


class OciModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)


class Platform(OciModel):
    architecture: Literal["amd64"]
    os: Literal["linux"]


class Descriptor(OciModel):
    media_type: str = Field(alias="mediaType")
    digest: DIGEST
    size: int = Field(ge=1, le=MAX_ARCHIVE_BYTES)
    annotations: dict[str, str] = Field(default_factory=dict)
    platform: Platform | None = None


class ImageIndex(OciModel):
    schema_version: Literal[2] = Field(alias="schemaVersion")
    media_type: Literal[OCI_INDEX] = Field(default=OCI_INDEX, alias="mediaType")
    manifests: tuple[Descriptor, ...] = Field(min_length=1, max_length=1)
    annotations: dict[str, str] = Field(default_factory=dict)


class ImageManifest(OciModel):
    schema_version: Literal[2] = Field(alias="schemaVersion")
    media_type: Literal[OCI_MANIFEST] = Field(alias="mediaType")
    config: Descriptor
    layers: tuple[Descriptor, ...] = Field(min_length=1, max_length=100)
    annotations: dict[str, str] = Field(default_factory=dict)


class RuntimeConfig(ApiModel):
    # Env/Cmd/history等のOCI標準fieldは実行せず、公開契約のlabelだけ照合する。
    labels: dict[str, str] = Field(alias="Labels")


class RootFilesystem(OciModel):
    type: Literal["layers"]
    diff_ids: tuple[DIGEST, ...] = Field(min_length=1, max_length=100)


class ImageConfig(ApiModel):
    architecture: Literal["amd64"]
    os: Literal["linux"]
    config: RuntimeConfig
    rootfs: RootFilesystem


class Layout(OciModel):
    version: Literal["1.0.0"] = Field(alias="imageLayoutVersion")


class Blob(OciModel):
    digest: DIGEST
    size: int
    offset: int


class ValidatedOciImage(OciModel):
    archive: Path
    kind: ImageKind
    source_commit: COMMIT
    manifest_digest: DIGEST
    manifest_body: bytes = Field(repr=False)
    blobs: tuple[Blob, ...] = Field(repr=False)
    file_identity: tuple[int, ...] = Field(repr=False)


class PublishedImage(OciModel):
    image_uri: str
    manifest_digest: DIGEST


def _identity(status: os.stat_result) -> tuple[int, ...]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
        status.st_mode,
        status.st_nlink,
        status.st_uid,
    )


@contextmanager
def _open_archive(path: Path, expected: tuple[int, ...] | None = None) -> Iterator[BinaryIO]:
    root = Path(__file__).resolve().parents[4]
    checked = validate_external_regular_file(path, root, RUNTIME_ENVIRONMENT_FILE_POLICY)
    if not 1 <= checked.status.st_size <= MAX_ARCHIVE_BYTES:
        raise ValueError("aws_load_test_oci_size_invalid")
    with os.fdopen(os.open(checked.resolved, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        observed = _identity(os.fstat(stream.fileno()))
        if observed != _identity(checked.status) or (expected is not None and observed != expected):
            raise ValueError("aws_load_test_oci_file_changed")
        yield stream
        if observed != _identity(os.fstat(stream.fileno())):
            raise ValueError("aws_load_test_oci_file_changed")


def _read(stream: BinaryIO, member: tarfile.TarInfo, maximum: int = MAX_JSON_BYTES) -> bytes:
    if not 1 <= member.size <= maximum:
        raise ValueError("aws_load_test_oci_member_size_invalid")
    stream.seek(member.offset_data)
    result = stream.read(member.size)
    if len(result) != member.size:
        raise ValueError("aws_load_test_oci_member_truncated")
    return result


def _members(stream: BinaryIO) -> dict[str, tarfile.TarInfo]:
    entries: dict[str, tarfile.TarInfo] = {}
    with tarfile.open(fileobj=stream, mode="r:") as archive:
        for count, member in enumerate(archive):
            if count >= 256 or member.name in entries:
                raise ValueError("aws_load_test_oci_member_duplicate_or_excessive")
            if member.isdir() and member.name in {"blobs", "blobs/sha256"}:
                continue
            if (
                member.issparse()
                or not member.isfile()
                or not (
                    member.name in {"index.json", "oci-layout"}
                    or re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", member.name)
                )
            ):
                raise ValueError("aws_load_test_oci_member_invalid")
            if (
                member.size < 1
                or member.offset_data + member.size > os.fstat(stream.fileno()).st_size
            ):
                raise ValueError("aws_load_test_oci_member_size_invalid")
            entries[member.name] = member
    if not {"index.json", "oci-layout"} <= entries.keys():
        raise ValueError("aws_load_test_oci_layout_missing")
    return entries


def _blob(entries: dict[str, tarfile.TarInfo], descriptor: Descriptor) -> tarfile.TarInfo:
    member = entries.get("blobs/sha256/" + descriptor.digest.removeprefix("sha256:"))
    if member is None or member.size != descriptor.size:
        raise ValueError("aws_load_test_oci_descriptor_mismatch")
    return member


def _inspect(stream: BinaryIO, source_commit: str) -> tuple[Descriptor, bytes, tuple[Blob, ...]]:
    entries = _members(stream)
    Layout.model_validate_json(_read(stream, entries["oci-layout"]))
    index = ImageIndex.model_validate_json(_read(stream, entries["index.json"]))
    descriptor = index.manifests[0]
    referenced = {"index.json", "oci-layout"}
    for _ in range(3):
        member = _blob(entries, descriptor)
        body = _read(stream, member)
        referenced.add(member.name)
        if "sha256:" + sha256(body).hexdigest() != descriptor.digest:
            raise ValueError("aws_load_test_oci_digest_mismatch")
        if descriptor.media_type == OCI_MANIFEST:
            break
        if descriptor.media_type != OCI_INDEX:
            raise ValueError("aws_load_test_oci_manifest_media_type_invalid")
        descriptor = ImageIndex.model_validate_json(body).manifests[0]
    else:
        raise ValueError("aws_load_test_oci_index_depth_invalid")
    manifest = ImageManifest.model_validate_json(body)
    if manifest.config.media_type != OCI_CONFIG or any(
        d.media_type not in OCI_LAYERS for d in manifest.layers
    ):
        raise ValueError("aws_load_test_oci_blob_media_type_invalid")
    config = ImageConfig.model_validate_json(_read(stream, _blob(entries, manifest.config)))
    if (
        config.config.labels.get("org.opencontainers.image.revision") != source_commit
        or config.config.labels.get("io.tomidaisai.deployment-environment") != "aws-demo"
        or len(config.rootfs.diff_ids) != len(manifest.layers)
    ):
        raise ValueError("aws_load_test_oci_source_or_environment_invalid")
    blobs: dict[str, Blob] = {}
    for item in (manifest.config, *manifest.layers):
        member = _blob(entries, item)
        referenced.add(member.name)
        digest = sha256()
        stream.seek(member.offset_data)
        remaining = member.size
        while remaining:
            part = stream.read(min(1024 * 1024, remaining))
            if not part:
                raise ValueError("aws_load_test_oci_member_truncated")
            digest.update(part)
            remaining -= len(part)
        if "sha256:" + digest.hexdigest() != item.digest:
            raise ValueError("aws_load_test_oci_digest_mismatch")
        blobs[item.digest] = Blob(digest=item.digest, size=member.size, offset=member.offset_data)
    if entries.keys() != referenced:
        raise ValueError("aws_load_test_oci_unreferenced_content")
    return descriptor, body, tuple(blobs.values())


def inspect_oci_archive(
    path: Path,
    expected_source_commit: str,
    image_kind: ImageKind,
    expected_manifest_digest: str | None = None,
) -> ValidatedOciImage:
    """外部0600 OCI tarを有限streamで検証し、configではなくmanifest digestを返す。"""
    try:
        with _open_archive(path) as stream:
            descriptor, body, blobs = _inspect(stream, expected_source_commit)
            if (
                expected_manifest_digest is not None
                and descriptor.digest != expected_manifest_digest
            ):
                raise ValueError("aws_load_test_oci_manifest_digest_mismatch")
            return ValidatedOciImage(
                archive=path,
                kind=image_kind,
                source_commit=expected_source_commit,
                manifest_digest=descriptor.digest,
                manifest_body=body,
                blobs=blobs,
                file_identity=_identity(os.fstat(stream.fileno())),
            )
    except tarfile.TarError:
        raise ValueError("aws_load_test_oci_archive_invalid") from None


class LayerStatus(ApiModel):
    digest: DIGEST = Field(alias="layerDigest")
    availability: Literal["AVAILABLE", "UNAVAILABLE"] = Field(alias="layerAvailability")
    size: int | None = Field(default=None, alias="layerSize")


class LayerFailure(ApiModel):
    digest: DIGEST = Field(alias="layerDigest")
    code: str = Field(alias="failureCode")


class LayerAvailability(ApiModel):
    layers: tuple[LayerStatus, ...] = ()
    failures: tuple[LayerFailure, ...] = ()


class InitiatedUpload(ApiModel):
    upload_id: str = Field(alias="uploadId", pattern=r"^[0-9a-fA-F-]{36}$")
    part_size: int = Field(alias="partSize", ge=MIN_PART_BYTES, le=MAX_PART_BYTES)


class UploadedPart(ApiModel):
    registry_id: str = Field(alias="registryId")
    repository: str = Field(alias="repositoryName")
    upload_id: str = Field(alias="uploadId")
    last_byte: int = Field(alias="lastByteReceived")


class CompletedUpload(ApiModel):
    registry_id: str = Field(alias="registryId")
    repository: str = Field(alias="repositoryName")
    upload_id: str = Field(alias="uploadId")
    digest: DIGEST = Field(alias="layerDigest")


class ImageIdentifier(ApiModel):
    digest: DIGEST = Field(alias="imageDigest")


class EcrImage(ApiModel):
    registry_id: str = Field(alias="registryId")
    repository: str = Field(alias="repositoryName")
    identifier: ImageIdentifier = Field(alias="imageId")
    manifest: str = Field(alias="imageManifest", min_length=1, max_length=MAX_JSON_BYTES)
    media_type: Literal[OCI_MANIFEST] = Field(alias="imageManifestMediaType")


class PutImageResponse(ApiModel):
    image: EcrImage


class ImageFailure(ApiModel):
    code: str = Field(alias="failureCode")


class GetImageResponse(ApiModel):
    images: tuple[EcrImage, ...] = ()
    failures: tuple[ImageFailure, ...] = ()


def _is_available(api: AwsApi, account: str, repository: str, blob: Blob) -> bool:
    observed = api.call(
        "ecr",
        "batch_check_layer_availability",
        LayerAvailability,
        registryId=account,
        repositoryName=repository,
        layerDigests=[blob.digest],
    )
    if len(observed.layers) == 1 and not observed.failures:
        layer = observed.layers[0]
        if layer.digest == blob.digest and layer.availability == "UNAVAILABLE":
            return False
        if (
            layer.digest == blob.digest
            and layer.size == blob.size
            and layer.availability == "AVAILABLE"
        ):
            return True
    if not observed.layers and len(observed.failures) == 1:
        failure = observed.failures[0]
        if failure.digest == blob.digest and failure.code == "MissingLayerDigest":
            return False
    raise ValueError("aws_load_test_ecr_layer_readback_invalid")


def _upload_blob(
    api: AwsApi,
    plan: AwsRunPlan,
    repository: str,
    blob: Blob,
    stream: BinaryIO,
    now: datetime | None,
) -> None:
    account = plan.account_id
    plan.require_creation_window(now or datetime.now(UTC))
    begun = api.call(
        "ecr",
        "initiate_layer_upload",
        InitiatedUpload,
        registryId=account,
        repositoryName=repository,
    )
    stream.seek(blob.offset)
    cursor = 0
    while cursor < blob.size:
        plan.require_creation_window(now or datetime.now(UTC))
        part = stream.read(min(begun.part_size, blob.size - cursor))
        if not part:
            raise ValueError("aws_load_test_oci_member_truncated")
        last = cursor + len(part) - 1
        received = api.call(
            "ecr",
            "upload_layer_part",
            UploadedPart,
            registryId=account,
            repositoryName=repository,
            uploadId=begun.upload_id,
            partFirstByte=cursor,
            partLastByte=last,
            layerPartBlob=part,
        )
        if (received.registry_id, received.repository, received.upload_id, received.last_byte) != (
            account,
            repository,
            begun.upload_id,
            last,
        ):
            raise ValueError("aws_load_test_ecr_part_readback_invalid")
        cursor = last + 1
    plan.require_creation_window(now or datetime.now(UTC))
    try:
        completed = api.call(
            "ecr",
            "complete_layer_upload",
            CompletedUpload,
            registryId=account,
            repositoryName=repository,
            uploadId=begun.upload_id,
            layerDigests=[blob.digest],
        )
    except AwsCallError as error:
        if error.code != "LayerAlreadyExistsException" or not _is_available(
            api, account, repository, blob
        ):
            raise
    else:
        if (completed.registry_id, completed.repository, completed.upload_id, completed.digest) != (
            account,
            repository,
            begun.upload_id,
            blob.digest,
        ):
            raise ValueError("aws_load_test_ecr_layer_readback_invalid")


def _check_image(
    observed: EcrImage, plan: AwsRunPlan, repository: str, image: ValidatedOciImage
) -> None:
    if (
        observed.registry_id != plan.account_id
        or observed.repository != repository
        or observed.identifier.digest != image.manifest_digest
        or observed.manifest.encode() != image.manifest_body
    ):
        raise ValueError("aws_load_test_ecr_manifest_readback_invalid")


def publish_image(
    api: AwsApi,
    plan: AwsRunPlan,
    stack: Stack,
    image: ValidatedOciImage,
    *,
    manifest: LoadTestManifest,
    now: datetime | None = None,
) -> PublishedImage:
    """AWS側guardを再確認し、固定run repositoryへverified blobとmanifestだけを配る。"""
    expected_uri = (
        f"{plan.account_id}.dkr.ecr.{plan.region}.amazonaws.com/{plan.prefix}/{image.kind}"
    )
    expected_image = (
        manifest.artifacts.api_image if image.kind == "app" else manifest.artifacts.gateway_image
    )
    if (
        manifest_context(manifest).manifest_sha256 != plan.manifest_sha256
        or manifest.run_id != plan.run_id
        or image.source_commit != plan.source_commit
        or manifest.artifacts.source_commit != plan.source_commit
        or expected_image != f"{expected_uri}@{image.manifest_digest}"
    ):
        raise ValueError("aws_load_test_image_run_binding_invalid")
    image = inspect_oci_archive(
        image.archive, plan.source_commit, image.kind, image.manifest_digest
    )
    current = deployment_preflight(api, plan, now=now or datetime.now(UTC))
    output_key = "AppRepositoryUri" if image.kind == "app" else "GatewayRepositoryUri"
    if current.stack_id != stack.stack_id or output(current, output_key) != expected_uri:
        raise ValueError("aws_load_test_image_repository_binding_invalid")
    repository = f"{plan.prefix}/{image.kind}"
    with _open_archive(image.archive, image.file_identity) as stream:
        for blob in image.blobs:
            if not _is_available(api, plan.account_id, repository, blob):
                _upload_blob(api, plan, repository, blob, stream, now)
        if _identity(os.fstat(stream.fileno())) != image.file_identity:
            raise ValueError("aws_load_test_oci_file_changed")
        plan.require_creation_window(now or datetime.now(UTC))
        try:
            result = api.call(
                "ecr",
                "put_image",
                PutImageResponse,
                registryId=plan.account_id,
                repositoryName=repository,
                imageManifest=image.manifest_body.decode(),
                imageManifestMediaType=OCI_MANIFEST,
                imageDigest=image.manifest_digest,
                imageTag="source-" + plan.source_commit,
            )
        except AwsCallError as error:
            if error.code != "ImageAlreadyExistsException":
                raise
        else:
            _check_image(result.image, plan, repository, image)
        fetched = api.call(
            "ecr",
            "batch_get_image",
            GetImageResponse,
            registryId=plan.account_id,
            repositoryName=repository,
            imageIds=[{"imageDigest": image.manifest_digest}],
            acceptedMediaTypes=[OCI_MANIFEST],
        )
        if fetched.failures or len(fetched.images) != 1:
            raise ValueError("aws_load_test_ecr_image_readback_missing")
        _check_image(fetched.images[0], plan, repository, image)
    return PublishedImage(
        image_uri=f"{expected_uri}@{image.manifest_digest}", manifest_digest=image.manifest_digest
    )
