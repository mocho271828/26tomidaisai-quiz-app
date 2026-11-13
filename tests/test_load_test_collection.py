from __future__ import annotations

import io
import tarfile
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import TypeVar
from unittest.mock import MagicMock

import pytest
from botocore.response import StreamingBody
from pydantic import BaseModel
from tests.test_load_test_manifest import REPOSITORY_ROOT, _manifest
from tests.test_load_test_orchestrator import AdvancingClock, _files, _prepared

from quiz_app import load_test_orchestrator as entry
from quiz_app.load_test import collection
from quiz_app.load_test.collection import (
    ACK_KEY,
    ARCHIVE_KEY,
    CollectionAck,
    build_bundle,
    collect_locally,
    collection_deadline,
    publish_and_wait,
    unpack_bundle,
)
from quiz_app.load_test.collection_inventory import (
    CollectedFile,
    allowed_path,
    collect_inventory,
    validate_inventory,
)
from quiz_app.load_test.controller_contracts import canonical_json_bytes, manifest_context
from quiz_app.load_test.db_verifier_artifacts import write_canonical_artifact
from quiz_app.load_test.orchestrator_contracts import OrchestratorError, RunFiles, RunResult
from quiz_app.load_test.orchestrator_process import RunInterrupted
from quiz_app.load_test.verification_contracts import ArtifactIdentity
from quiz_app.load_test_aws.aws import AwsCallError


def fixture_items():
    manifest = _manifest(public_origin="https://10.0.1.20")
    prepared = _prepared().model_copy(
        update={
            "prepared_at": manifest.created_at,
            "expires_at": manifest.scheduled_start_at + timedelta(hours=3),
        }
    )
    result = RunResult(
        run_id=manifest.run_id,
        manifest_sha256=manifest_context(manifest).manifest_sha256,
        decision="FAIL",
        completed_rounds=0,
        artifacts=(),
        finished_at=manifest.scheduled_start_at + timedelta(seconds=4800),
    )
    items = tuple(
        CollectedFile(path=path, body=canonical_json_bytes(model) + b"\n")
        for path, model in (
            ("inputs/manifest.json", manifest),
            ("inputs/preparation.json", prepared),
            ("results/result.json", result),
        )
    )
    return manifest, prepared, result, items


ResponseT = TypeVar("ResponseT", bound=BaseModel)


class MemoryAws:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.calls = []

    def call(
        self, service: str, operation: str, response: type[ResponseT], **parameters: object
    ) -> ResponseT:
        self.calls.append((service, operation, parameters))
        if service == "sts":
            return response.model_validate({"Account": "123456789012", "Arn": "arn:test"})
        if operation == "list_objects_v2":
            assert parameters["Prefix"] == ACK_KEY and parameters["MaxKeys"] == 2
            return response.model_validate(
                {
                    "IsTruncated": False,
                    "Contents": [{"Key": ACK_KEY}] if ACK_KEY in self.objects else [],
                }
            )
        key = parameters["Key"]
        assert isinstance(key, str)
        if operation == "put_object":
            if key in self.objects:
                raise AwsCallError("s3", operation, "PreconditionFailed")
            body = parameters["Body"]
            assert isinstance(body, bytes)
            self.objects[key] = body
            return response.model_validate({})
        if key not in self.objects:
            raise AwsCallError("s3", operation, "NoSuchKey")
        body = self.objects[key]
        return response.model_validate(
            {
                "Body": StreamingBody(io.BytesIO(body), len(body)),
                "ContentLength": len(body),
                "Metadata": {"sha256": sha256(body).hexdigest()},
            }
        )


def clock_at(value: datetime, seconds: int = 600) -> AdvancingClock:
    clock = AdvancingClock(value)
    clock.deadline = value + timedelta(seconds=seconds)
    return clock


def test_failed_run_bundle_round_trip_and_every_byte_inventory():
    manifest, prepared, result, items = fixture_items()
    bundle = build_bundle(items, manifest, prepared)
    recovered, extracted, actual = unpack_bundle(bundle.body, manifest, prepared)
    assert recovered == bundle
    assert extracted == items and actual == result
    assert recovered.index.artifacts[2].artifact == items[2].identity


@pytest.mark.parametrize("mode", ["gzip-filename", "trailing", "tar-header"])
def test_archive_metadata_cannot_hide_bytes_outside_inventory(mode: str):
    import gzip

    manifest, prepared, _, items = fixture_items()
    body = build_bundle(items, manifest, prepared).body
    if mode == "gzip-filename":
        body = body[:3] + b"\x08" + body[4:10] + b"SECRET\x00" + body[10:]
    elif mode == "trailing":
        body += gzip.compress(b"", mtime=0)
    else:
        source = tarfile.open(fileobj=io.BytesIO(body), mode="r:gz")
        output = io.BytesIO()
        with source, tarfile.open(fileobj=output, mode="w:") as destination:
            for member in source:
                member.uname = "SECRET"
                destination.addfile(member, source.extractfile(member))
        body = gzip.compress(output.getvalue(), mtime=0)
    with pytest.raises(OrchestratorError, match="noncanonical"):
        unpack_bundle(body, manifest, prepared)


@pytest.mark.parametrize(
    "path",
    [
        "inputs/operator.json",
        "inputs/verifier-database-url",
        "generator-process/output.jsonl",
        "../result.json",
        "/results/result.json",
        "results/nested/result.json",
        "runtime/runtime.env",
    ],
)
def test_secret_and_traversal_paths_are_not_collectable(path: str):
    assert not allowed_path(path)


def test_input_credentials_and_raw_logs_are_never_read(tmp_path: Path):
    manifest, prepared, _, items = fixture_items()
    files = _files(tmp_path)
    for item in items:
        path = files.root / item.path
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.write_bytes(item.body)
        path.chmod(0o600)
    (files.input("operator.json")).write_text("SECRET")
    logs = files.output("generator-process") / "output.jsonl"
    logs.write_text("Authorization: Bearer SECRET")
    assert set(collect_inventory(files, manifest, prepared)) == set(items)
    assert (
        b"SECRET"
        not in build_bundle(collect_inventory(files, manifest, prepared), manifest, prepared).body
    )


def test_result_referenced_artifact_missing_is_rejected():
    manifest, prepared, result, items = fixture_items()
    missing = ArtifactIdentity(name="missing.json", sha256="a" * 64, size_bytes=1)
    changed = result.model_copy(update={"artifacts": (missing,)})
    items = (
        *items[:2],
        CollectedFile(path="results/result.json", body=canonical_json_bytes(changed)),
    )
    with pytest.raises(OrchestratorError, match="result_artifact_missing"):
        build_bundle(items, manifest, prepared)


def test_passing_ten_round_count_alone_cannot_replace_full_evidence():
    manifest, prepared, result, items = fixture_items()
    changed = result.model_copy(update={"decision": "PASS", "completed_rounds": 10})
    items = (
        *items[:2],
        CollectedFile(path="results/result.json", body=canonical_json_bytes(changed)),
    )
    with pytest.raises(OrchestratorError, match="pass_evidence_incomplete"):
        build_bundle(items, manifest, prepared)


def test_duplicate_inventory_and_foreign_run_are_refused():
    manifest, prepared, _, items = fixture_items()
    with pytest.raises(OrchestratorError, match="duplicate"):
        validate_inventory((*items, items[0]), manifest, prepared)
    other = prepared.model_copy(update={"manifest_sha256": "f" * 64})
    with pytest.raises(OrchestratorError, match="binding"):
        build_bundle(items, manifest, other)


@pytest.mark.parametrize("mode", ["changed", "duplicate", "traversal", "link", "extra"])
def test_malicious_archive_refused_before_any_extraction(mode: str):
    manifest, prepared, _, items = fixture_items()
    bundle = build_bundle(items, manifest, prepared)
    source = tarfile.open(fileobj=io.BytesIO(bundle.body), mode="r:gz")
    output = io.BytesIO()
    with source, tarfile.open(fileobj=output, mode="w:gz") as destination:
        for member in source:
            body = source.extractfile(member).read()
            if member.name == "results/result.json" and mode == "changed":
                body += b" "
                member.size = len(body)
            destination.addfile(member, io.BytesIO(body))
            if member.name == "results/result.json" and mode == "duplicate":
                destination.addfile(member, io.BytesIO(body))
        if mode in {"traversal", "link", "extra"}:
            member = tarfile.TarInfo(
                "../outside" if mode == "traversal" else "inputs/operator.json"
            )
            member.size = 6
            if mode == "link":
                member.type = tarfile.SYMTYPE
                member.linkname = "../outside"
            destination.addfile(member, io.BytesIO(b"SECRET"))
    with pytest.raises(OrchestratorError):
        unpack_bundle(output.getvalue(), manifest, prepared)


def test_collection_persists_and_rechecks_full_inventory_before_ack(tmp_path: Path):
    manifest, prepared, result, items = fixture_items()
    api = MemoryAws()
    bundle = build_bundle(items, manifest, prepared)
    api.objects[ARCHIVE_KEY] = bundle.body
    output = (tmp_path / "collected").resolve()
    actual = collect_locally(
        api, manifest, prepared, output, REPOSITORY_ROOT, clock_at(result.finished_at)
    )
    assert actual == result
    assert output.stat().st_mode & 0o777 == 0o700
    for item in items:
        path = output / item.path
        assert path.read_bytes() == item.body and path.stat().st_mode & 0o777 == 0o600
    ack = CollectionAck.model_validate_json(api.objects[ACK_KEY])
    assert ack.archive_sha256 == bundle.sha256 and ack.inventory_sha256 == bundle.index_sha256
    assert (output / "collection-ack.json").read_bytes() == api.objects[ACK_KEY]


def test_existing_collection_directory_is_never_overwritten(tmp_path: Path):
    manifest, prepared, result, _ = fixture_items()
    output = (tmp_path / "collected").resolve()
    output.mkdir(mode=0o700)
    api = MemoryAws()
    with pytest.raises(FileExistsError):
        collect_locally(
            api, manifest, prepared, output, REPOSITORY_ROOT, clock_at(result.finished_at)
        )
    assert ACK_KEY not in api.objects


def test_corrupt_download_and_failed_local_write_never_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manifest, prepared, result, items = fixture_items()
    api = MemoryAws()
    api.objects[ARCHIVE_KEY] = build_bundle(items, manifest, prepared).body
    monkeypatch.setattr(collection, "_write_new", MagicMock(side_effect=OSError("disk full")))
    with pytest.raises(OSError):
        collect_locally(
            api,
            manifest,
            prepared,
            (tmp_path / "collected").resolve(),
            REPOSITORY_ROOT,
            clock_at(result.finished_at),
        )
    assert ACK_KEY not in api.objects


def test_s3_checksum_mismatch_is_rejected_and_stream_closed():
    _, prepared, _, _ = fixture_items()
    stream = StreamingBody(io.BytesIO(b"changed"), 7)
    response = collection.ArchiveObject(Body=stream, ContentLength=7, Metadata={"sha256": "a" * 64})
    api = MagicMock()
    api.call.return_value = response
    with pytest.raises(OrchestratorError, match="checksum"):
        collection._get(api, prepared, ARCHIVE_KEY, 100)
    assert stream._raw_stream.closed


def test_local_tampering_during_save_prevents_ack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manifest, prepared, result, items = fixture_items()
    api = MemoryAws()
    api.objects[ARCHIVE_KEY] = build_bundle(items, manifest, prepared).body
    output = (tmp_path / "collected").resolve()
    write = collection._write_new

    def altered_write(path: Path, body: bytes) -> None:
        write(path, body)
        if path.name == "archive.sha256":
            with (output / "results/result.json").open("ab") as destination:
                destination.write(b" ")

    monkeypatch.setattr(collection, "_write_new", altered_write)
    with pytest.raises(OrchestratorError, match="local_artifact_changed"):
        collect_locally(
            api, manifest, prepared, output, REPOSITORY_ROOT, clock_at(result.finished_at)
        )
    assert ACK_KEY not in api.objects


def test_missing_ack_is_bounded_and_wrong_ack_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manifest, prepared, result, items = fixture_items()
    files = _files(tmp_path)
    bundle = build_bundle(items, manifest, prepared)
    monkeypatch.setattr(collection, "collect_inventory", lambda *args: items)
    monkeypatch.setattr(collection, "build_bundle", lambda *args: bundle)
    api = MemoryAws()
    with pytest.raises(OrchestratorError, match="deadline"):
        publish_and_wait(api, files, manifest, prepared, clock_at(result.finished_at, 4))
    assert ARCHIVE_KEY in api.objects
    assert not any(operation == "get_object" for _, operation, _ in api.calls)
    api = MemoryAws()
    ack = CollectionAck(
        run_id=manifest.run_id,
        manifest_sha256=prepared.manifest_sha256,
        archive_sha256="f" * 64,
        inventory_sha256=bundle.index_sha256,
        artifact_count=len(items),
        collected_at=result.finished_at,
    )
    api.objects[ACK_KEY] = canonical_json_bytes(ack)
    with pytest.raises(OrchestratorError, match="ack_binding"):
        publish_and_wait(api, files, manifest, prepared, clock_at(result.finished_at))


def test_correct_ack_and_interruption_respect_collection_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manifest, prepared, result, items = fixture_items()
    bundle = build_bundle(items, manifest, prepared)
    monkeypatch.setattr(collection, "collect_inventory", lambda *args: items)
    monkeypatch.setattr(collection, "build_bundle", lambda *args: bundle)
    ack = CollectionAck(
        run_id=manifest.run_id,
        manifest_sha256=prepared.manifest_sha256,
        archive_sha256=bundle.sha256,
        inventory_sha256=bundle.index_sha256,
        artifact_count=len(items),
        collected_at=result.finished_at,
    )
    api = MemoryAws()
    api.objects[ACK_KEY] = canonical_json_bytes(ack)
    files = _files(tmp_path)
    assert publish_and_wait(api, files, manifest, prepared, clock_at(result.finished_at)) == ack
    clock = clock_at(result.finished_at)
    clock.stop.set()
    with pytest.raises(RunInterrupted):
        publish_and_wait(MemoryAws(), files, manifest, prepared, clock)


def test_collection_uses_earliest_of_ten_minutes_t0_90_minutes_and_aws_deadline():
    manifest, prepared, result, _ = fixture_items()
    assert collection_deadline(
        manifest, prepared, result.finished_at
    ) == manifest.scheduled_start_at + timedelta(seconds=5400)
    early = manifest.scheduled_start_at + timedelta(seconds=10)
    assert collection_deadline(manifest, prepared, early) == early + timedelta(seconds=600)
    shorter = prepared.model_copy(update={"expires_at": result.finished_at + timedelta(seconds=60)})
    assert collection_deadline(
        manifest, shorter, result.finished_at
    ) == result.finished_at + timedelta(seconds=30)


@pytest.mark.parametrize("error", [RuntimeError("private-message"), RunInterrupted("stop")])
def test_entry_collection_failure_always_cleans_and_never_prints_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: BaseException,
):
    manifest, prepared, result, _ = fixture_items()
    files = _files(tmp_path)
    write_canonical_artifact(files.root / "inputs", "preparation.json", prepared)
    monkeypatch.setattr(entry, "BotoAwsApi", MagicMock())
    monkeypatch.setattr(RunFiles, "load_binding", lambda self, **kwargs: (manifest, prepared))
    monkeypatch.setattr(entry, "load_runner_configuration", MagicMock())
    runner = MagicMock(manifest=manifest, prepared=prepared)
    runner.run.return_value = result.model_copy(update={"decision": "PASS", "completed_rounds": 10})
    monkeypatch.setattr(entry, "LoadTestOrchestrator", MagicMock(return_value=runner))
    monkeypatch.setattr(entry, "publish_and_wait", MagicMock(side_effect=error))
    cleanup = MagicMock(return_value=SimpleNamespace(arn="arn:cleanup"))
    monkeypatch.setattr(entry, "request_cleanup", cleanup)
    monkeypatch.setattr(entry, "log_failure", MagicMock())
    assert entry.main(["--run-directory", str(files.root)]) == 1
    cleanup.assert_called_once()
    text = capsys.readouterr()
    assert "PASS" not in text.out and "private-message" not in text.err
