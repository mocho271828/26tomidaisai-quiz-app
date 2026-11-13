from __future__ import annotations

import gzip
import io
import json
import os
import tarfile
from collections.abc import Callable
from datetime import datetime, timedelta, tzinfo
from hashlib import sha256
from pathlib import Path
from typing import TypeVar

import pytest
from pydantic import BaseModel, ValidationError
from tests.test_load_test_aws import FakeAws, plan
from tests.test_load_test_manifest import CREATED_AT, _manifest

from quiz_app.load_test.controller_contracts import manifest_context
from quiz_app.load_test_aws import deployment_images as module
from quiz_app.load_test_aws.aws import AwsApi, AwsCallError
from quiz_app.load_test_aws.deployment_images import (
    MIN_PART_BYTES,
    OCI_CONFIG,
    OCI_INDEX,
    OCI_MANIFEST,
    inspect_oci_archive,
    publish_image,
)
from quiz_app.load_test_aws.models import AwsRunPlan
from quiz_app.load_test_aws.responses import Stack

ResponseT = TypeVar("ResponseT", bound=BaseModel)
COMMIT = "a" * 40
UPLOAD_ID = "00000000-0000-0000-0000-000000000001"


def encode(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def digest(body: bytes) -> str:
    return "sha256:" + sha256(body).hexdigest()


def descriptor(body: bytes, media_type: str) -> dict[str, object]:
    return {"digest": digest(body), "size": len(body), "mediaType": media_type}


def make_archive(
    tmp_path: Path,
    *,
    architecture: str = "amd64",
    environment: str = "aws-demo",
    source: str = COMMIT,
    index_mutation: Callable[[dict[str, object]], None] | None = None,
    entries_mutation: Callable[[dict[str, bytes]], None] | None = None,
    layer: bytes | None = None,
    layer_type: str = "application/vnd.oci.image.layer.v1.tar+gzip",
    nested: bool = False,
    link: bool = False,
    duplicate: bool = False,
) -> tuple[Path, bytes, bytes, bytes]:
    layer = layer or gzip.compress(b"test layer")
    config = encode(
        {
            "architecture": architecture,
            "os": "linux",
            "config": {
                "Labels": {
                    "org.opencontainers.image.revision": source,
                    "io.tomidaisai.deployment-environment": environment,
                },
                "Cmd": ["python", "-m", "quiz_app"],
            },
            "rootfs": {"type": "layers", "diff_ids": [digest(b"test layer")]},
            "history": [],
        }
    )
    manifest = encode(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": descriptor(config, OCI_CONFIG),
            "layers": [descriptor(layer, layer_type)],
        }
    )
    index: dict[str, object] = {
        "schemaVersion": 2,
        "mediaType": OCI_INDEX,
        "manifests": [descriptor(manifest, OCI_MANIFEST)],
    }
    entries = {
        "oci-layout": encode({"imageLayoutVersion": "1.0.0"}),
        **{"blobs/sha256/" + sha256(body).hexdigest(): body for body in (config, layer, manifest)},
    }
    if nested:
        inner = encode(index)
        entries["blobs/sha256/" + sha256(inner).hexdigest()] = inner
        index["manifests"] = [descriptor(inner, OCI_INDEX)]
    if index_mutation:
        index_mutation(index)
    entries["index.json"] = encode(index)
    if entries_mutation:
        entries_mutation(entries)
    archive_path = tmp_path.resolve() / "app.oci.tar"
    with tarfile.open(archive_path, "w") as archive:
        for name, body in entries.items():
            info = tarfile.TarInfo(name)
            info.mode, info.size = 0o600, len(body)
            archive.addfile(info, io.BytesIO(body))
        if link:
            info = tarfile.TarInfo("escape")
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
            archive.addfile(info)
        if duplicate:
            info = tarfile.TarInfo("index.json")
            info.size = len(entries["index.json"])
            archive.addfile(info, io.BytesIO(entries["index.json"]))
    archive_path.chmod(0o600)
    return archive_path, manifest, config, layer


@pytest.mark.parametrize("nested", [False, True])
def test_oci_digest_is_manifest_bytes_not_config(tmp_path: Path, nested: bool) -> None:
    path, manifest, config, layer = make_archive(tmp_path, nested=nested)
    result = inspect_oci_archive(path, COMMIT, "app")
    assert result.manifest_digest == digest(manifest) != digest(config)
    assert result.manifest_body == manifest
    assert {blob.digest for blob in result.blobs} == {digest(config), digest(layer)}


@pytest.mark.parametrize(
    "updates",
    [
        {"architecture": "arm64"},
        {"environment": "local-demo"},
        {"source": "b" * 40},
        {"layer_type": "application/vnd.oci.image.layer.nondistributable.v1.tar+gzip"},
        {"layer_type": "application/vnd.oci.image.layer.v1.tar+zstd"},
        {"link": True},
        {"duplicate": True},
    ],
)
def test_rejects_incompatible_oci_content(tmp_path: Path, updates: dict[str, object]) -> None:
    path, _, _, _ = make_archive(tmp_path, **updates)
    with pytest.raises(ValueError):
        inspect_oci_archive(path, COMMIT, "app")


@pytest.mark.parametrize("field", ["multiple", "urls"])
def test_refuses_multi_platform_and_external_descriptor(tmp_path: Path, field: str) -> None:
    def change(index: dict[str, object]) -> None:
        manifests = index["manifests"]
        if field == "multiple":
            manifests.append(manifests[0])
        else:
            manifests[0]["urls"] = ["https://untrusted.invalid/blob"]

    path, _, _, _ = make_archive(tmp_path, index_mutation=change)
    with pytest.raises(ValidationError):
        inspect_oci_archive(path, COMMIT, "app")


@pytest.mark.parametrize("change", ["missing", "sha", "size", "unused", "traversal"])
def test_rejects_missing_modified_or_unreferenced_blobs(tmp_path: Path, change: str) -> None:
    def mutate(entries: dict[str, bytes]) -> None:
        name = next(key for key in entries if key.startswith("blobs/"))
        if change == "missing":
            del entries[name]
        elif change == "sha":
            entries[name] = entries[name].replace(b"amd64", b"amd65")
        elif change == "size":
            entries[name] += b" "
        elif change == "unused":
            entries["blobs/sha256/" + "f" * 64] = b"not referenced"
        else:
            entries["../escape"] = b"x"

    path, _, _, _ = make_archive(tmp_path, entries_mutation=mutate)
    with pytest.raises(ValueError):
        inspect_oci_archive(path, COMMIT, "app")


def test_expected_digest_must_not_be_config_digest(tmp_path: Path) -> None:
    path, _, config, _ = make_archive(tmp_path)
    with pytest.raises(ValueError, match="manifest_digest_mismatch"):
        inspect_oci_archive(path, COMMIT, "app", digest(config))


@pytest.mark.parametrize("kind", ["mode", "symlink", "hardlink", "oversize"])
def test_external_archive_security_boundary(tmp_path: Path, kind: str) -> None:
    path, _, _, _ = make_archive(tmp_path)
    if kind == "mode":
        path.chmod(0o644)
    elif kind == "symlink":
        other = path.with_name("link.tar")
        other.symlink_to(path)
        path = other
    elif kind == "hardlink":
        os.link(path, path.with_name("hardlink.tar"))
    else:
        with path.open("ab") as stream:
            stream.truncate(module.MAX_ARCHIVE_BYTES + 1)
    with pytest.raises(ValueError):
        inspect_oci_archive(path, COMMIT, "app")


class FakeEcr(FakeAws):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[bytes] = []
        self.available: dict[str, int] = {}
        self.stored_image: dict[str, object] | None = None
        self.fail_operation: str | None = None
        self.readback_wrong_bytes = False
        self.already_exists: set[str] = set()
        self.response_mutation: Callable[[str, dict[str, object]], None] | None = None

    def call(
        self, service: str, operation: str, response: type[ResponseT], **kwargs: object
    ) -> ResponseT:
        if service != "ecr":
            return super().call(service, operation, response, **kwargs)
        self.calls.append((service, operation, kwargs))
        if operation == self.fail_operation:
            raise AwsCallError(service, operation, "AccessDeniedException")
        binding = {"registryId": kwargs["registryId"], "repositoryName": kwargs["repositoryName"]}
        if operation == "batch_check_layer_availability":
            requested = kwargs["layerDigests"][0]
            raw = {
                "layers": [
                    {
                        "layerDigest": requested,
                        "layerAvailability": "AVAILABLE"
                        if requested in self.available
                        else "UNAVAILABLE",
                        "layerSize": self.available.get(requested),
                    }
                ]
            }
        elif operation == "initiate_layer_upload":
            self.parts = []
            raw = {"uploadId": UPLOAD_ID, "partSize": MIN_PART_BYTES}
        elif operation == "upload_layer_part":
            assert kwargs["partFirstByte"] == sum(len(part) for part in self.parts)
            self.parts.append(kwargs["layerPartBlob"])
            raw = {**binding, "uploadId": UPLOAD_ID, "lastByteReceived": kwargs["partLastByte"]}
        elif operation == "complete_layer_upload":
            body = b"".join(self.parts)
            assert kwargs["layerDigests"] == [digest(body)]
            self.available[digest(body)] = len(body)
            raw = {**binding, "uploadId": UPLOAD_ID, "layerDigest": digest(body)}
        elif operation == "put_image":
            assert digest(kwargs["imageManifest"].encode()) == kwargs["imageDigest"]
            self.stored_image = {
                **binding,
                "imageId": {"imageDigest": kwargs["imageDigest"]},
                "imageManifest": kwargs["imageManifest"],
                "imageManifestMediaType": OCI_MANIFEST,
            }
            raw = {"image": self.stored_image}
        elif operation == "batch_get_image":
            assert self.stored_image is not None
            image = dict(self.stored_image)
            if self.readback_wrong_bytes:
                image["imageManifest"] += " "
            raw = {"images": [image], "failures": []}
        else:
            raise AssertionError(operation)
        if operation in self.already_exists:
            code = (
                "LayerAlreadyExistsException"
                if operation == "complete_layer_upload"
                else "ImageAlreadyExistsException"
            )
            raise AwsCallError(service, operation, code)
        if self.response_mutation:
            self.response_mutation(operation, raw)
        return response.model_validate(raw)


def publish_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **archive_options: object):
    path, body, config, layer = make_archive(tmp_path, **archive_options)
    image = inspect_oci_archive(path, COMMIT, "app")
    manifest = _manifest(public_origin="https://10.77.1.10")
    run = plan().model_copy(update={"run_id": manifest.run_id, "created_at": CREATED_AT})
    uri = f"{run.account_id}.dkr.ecr.{run.region}.amazonaws.com/{run.prefix}/app"
    manifest = manifest.model_copy(
        update={
            "artifacts": manifest.artifacts.model_copy(
                update={"api_image": uri + "@" + digest(body), "source_commit": COMMIT}
            )
        }
    )
    run = run.model_copy(update={"manifest_sha256": manifest_context(manifest).manifest_sha256})
    stack = Stack(
        StackId="stack-id",
        StackName=run.workload_stack,
        StackStatus="CREATE_COMPLETE",
        Outputs=[{"OutputKey": "AppRepositoryUri", "OutputValue": uri}],
    )
    guards: list[object] = []

    def preflight(api: AwsApi, plan: AwsRunPlan, *, now: datetime) -> Stack:
        guards.append((api, plan, now))
        return stack

    monkeypatch.setattr(module, "deployment_preflight", preflight)
    return run, manifest, stack, image, config, layer, guards


def test_pushes_exact_manifest_and_only_missing_blobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, manifest, stack, image, config, layer, guards = publish_fixture(tmp_path, monkeypatch)
    api = FakeEcr()
    api.available[digest(layer)] = len(layer)
    result = publish_image(api, run, stack, image, manifest=manifest, now=CREATED_AT)
    assert guards == [(api, run, CREATED_AT)]
    assert result.image_uri == manifest.artifacts.api_image
    uploads = [call[2]["layerPartBlob"] for call in api.calls if call[1] == "upload_layer_part"]
    assert uploads == [config]
    assert api.stored_image["imageManifest"].encode() == image.manifest_body
    assert api.calls[-1][1] == "batch_get_image"


def test_uploads_multiple_parts_with_exact_byte_ranges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, manifest, stack, image, _, layer, _ = publish_fixture(
        tmp_path,
        monkeypatch,
        layer=b"x" * (MIN_PART_BYTES + 7),
        layer_type="application/vnd.oci.image.layer.v1.tar",
    )
    api = FakeEcr()
    publish_image(api, run, stack, image, manifest=manifest, now=CREATED_AT)
    parts = [item[2] for item in api.calls if item[1] == "upload_layer_part"][-2:]
    assert [(item["partFirstByte"], item["partLastByte"]) for item in parts] == [
        (0, MIN_PART_BYTES - 1),
        (MIN_PART_BYTES, len(layer) - 1),
    ]
    assert b"".join(item["layerPartBlob"] for item in parts) == layer


@pytest.mark.parametrize("failure", ["guard", "run", "repository", "digest", "source", "archive"])
def test_refuses_before_ecr_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    run, manifest, stack, image, _, _, _ = publish_fixture(tmp_path, monkeypatch)
    api = FakeEcr()
    if failure == "guard":
        monkeypatch.setattr(
            module,
            "deployment_preflight",
            lambda *a, **k: (_ for _ in ()).throw(ValueError("guard")),
        )
    elif failure == "run":
        run = run.model_copy(update={"manifest_sha256": "0" * 64})
    elif failure == "repository":
        stack.outputs[0].value = "another-repository"
    elif failure == "digest":
        image = image.model_copy(update={"manifest_digest": "sha256:" + "0" * 64})
    elif failure == "source":
        image = image.model_copy(update={"source_commit": "b" * 40})
    else:
        image.archive.write_bytes(b"changed")
    with pytest.raises(ValueError):
        publish_image(api, run, stack, image, manifest=manifest, now=CREATED_AT)
    assert api.calls == []


@pytest.mark.parametrize(
    "operation",
    [
        "initiate_layer_upload",
        "upload_layer_part",
        "complete_layer_upload",
        "put_image",
        "batch_get_image",
    ],
)
def test_ecr_failures_are_not_converted_to_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    run, manifest, stack, image, _, _, _ = publish_fixture(tmp_path, monkeypatch)
    api = FakeEcr()
    api.fail_operation = operation
    with pytest.raises(AwsCallError):
        publish_image(api, run, stack, image, manifest=manifest, now=CREATED_AT)


def test_ecr_digest_claim_does_not_replace_manifest_bytes_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, manifest, stack, image, _, _, _ = publish_fixture(tmp_path, monkeypatch)
    api = FakeEcr()
    api.readback_wrong_bytes = True
    with pytest.raises(ValueError, match="manifest_readback_invalid"):
        publish_image(api, run, stack, image, manifest=manifest, now=CREATED_AT)


def test_expired_run_fails_before_upload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run, manifest, stack, image, _, _, _ = publish_fixture(tmp_path, monkeypatch)
    api = FakeEcr()
    # 実guardを通した時刻境界。期限後はSTSを含むAWS呼出にも到達しない。
    from quiz_app.load_test_aws.deployment_api import deployment_preflight

    monkeypatch.setattr(module, "deployment_preflight", deployment_preflight)
    with pytest.raises(ValueError, match="remaining_time"):
        publish_image(
            api, run, stack, image, manifest=manifest, now=run.expires_at + timedelta(seconds=1)
        )
    assert api.calls == []


@pytest.mark.parametrize(
    ("operation", "replacement"),
    [
        ("batch_check_layer_availability", {"layers": [], "failures": []}),
        ("initiate_layer_upload", {"partSize": MIN_PART_BYTES - 1}),
        ("upload_layer_part", {"lastByteReceived": -1}),
        ("upload_layer_part", {"repositoryName": "another-run/app"}),
        ("complete_layer_upload", {"layerDigest": "sha256:" + "0" * 64}),
        ("complete_layer_upload", {"registryId": "999999999999"}),
        ("batch_get_image", {"images": []}),
        ("batch_get_image", {"failures": [{"failureCode": "ImageNotFound"}]}),
    ],
)
def test_refuses_unbound_or_missing_ecr_acknowledgement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    replacement: dict[str, object],
) -> None:
    run, manifest, stack, image, _, _, _ = publish_fixture(tmp_path, monkeypatch)
    api = FakeEcr()

    def mutate(name: str, raw: dict[str, object]) -> None:
        if name == operation:
            raw.update(replacement)

    api.response_mutation = mutate
    with pytest.raises(ValueError):
        publish_image(api, run, stack, image, manifest=manifest, now=CREATED_AT)


def test_already_existing_upload_and_image_still_require_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, manifest, stack, image, _, _, _ = publish_fixture(tmp_path, monkeypatch)
    api = FakeEcr()
    api.already_exists = {"complete_layer_upload", "put_image"}
    result = publish_image(api, run, stack, image, manifest=manifest, now=CREATED_AT)
    assert result.image_uri == manifest.artifacts.api_image
    assert [item[1] for item in api.calls].count("batch_check_layer_availability") == 4
    assert api.calls[-1][1] == "batch_get_image"


def test_upload_stops_if_remaining_time_expires_between_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, manifest, stack, image, _, _, _ = publish_fixture(tmp_path, monkeypatch)
    observed_time = [CREATED_AT]

    class Clock:
        @staticmethod
        def now(zone: tzinfo) -> datetime:
            return observed_time[0].astimezone(zone)

    def advance_after_upload(operation: str, raw: dict[str, object]) -> None:
        if operation == "upload_layer_part":
            observed_time[0] = run.expires_at

    monkeypatch.setattr(module, "datetime", Clock)
    api = FakeEcr()
    api.response_mutation = advance_after_upload
    with pytest.raises(ValueError, match="remaining_time"):
        publish_image(api, run, stack, image, manifest=manifest)
    assert not {"complete_layer_upload", "put_image"} & {item[1] for item in api.calls}
