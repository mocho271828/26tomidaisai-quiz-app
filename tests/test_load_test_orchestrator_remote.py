from __future__ import annotations

import gzip
import io
import json
import tarfile
from hashlib import sha256
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.response import StreamingBody
from tests.test_load_test_orchestrator import _files, _prepared

from quiz_app.load_test import orchestrator_remote as module
from quiz_app.load_test.orchestrator_contracts import OrchestratorError
from quiz_app.load_test.orchestrator_remote import (
    MAX_ARCHIVE_BYTES,
    ArchiveObject,
    CommandInvocation,
    app_worker_status,
    download_archive,
    start_app_worker,
    upload_archive,
)
from quiz_app.load_test_aws.aws import AwsCallError
from quiz_app.load_test_aws.responses import CommandResponse


def test_ssm_commands_are_fixed_to_one_run_app_host() -> None:
    api = MagicMock()
    api.call.return_value = CommandResponse.model_validate({"Command": {"CommandId": "command-id"}})
    prepared = _prepared()
    assert start_app_worker(api, prepared, "faults") == "command-id"
    parameters = api.call.call_args.kwargs
    assert parameters["InstanceIds"] == [prepared.app_instance_id]
    assert parameters["DocumentName"] == f"gs-lt-{prepared.run_id.hex}-app-worker"
    assert parameters["Parameters"] == {"Action": ["faults"]}
    template = json.loads(
        (Path(__file__).parents[1] / "infra/aws-load-test/control.json").read_text()
    )
    document = template["Resources"]["AppWorkerDocument"]["Properties"]["Content"]
    assert set(parameters["Parameters"]) == set(document["parameters"])
    assert parameters["CloudWatchOutputConfig"] == {"CloudWatchOutputEnabled": False}


@pytest.mark.parametrize("status", ["Cancelled", "TimedOut", "Failed", "Cancelling"])
def test_failed_fault_worker_cannot_be_reported_as_success(status: str) -> None:
    api = MagicMock()
    api.call.return_value = CommandInvocation.model_validate({"Status": status, "ResponseCode": 1})
    with pytest.raises(OrchestratorError, match="worker_failed"):
        app_worker_status(api, _prepared(), "command-id")


def test_only_ssm_eventual_consistency_is_retried() -> None:
    api = MagicMock()
    api.call.side_effect = AwsCallError("ssm", "get_command_invocation", "InvocationDoesNotExist")
    assert not app_worker_status(api, _prepared(), "command-id")
    api.call.side_effect = AwsCallError("ssm", "get_command_invocation", "AccessDeniedException")
    with pytest.raises(AwsCallError):
        app_worker_status(api, _prepared(), "command-id")


def test_archive_upload_contains_only_explicit_files_and_roundtrips(tmp_path: Path) -> None:
    files = _files(tmp_path)
    path = files.output("evidence") / "fault-status.json"
    path.write_text('{"status":"completed"}')
    path.chmod(0o600)
    secret = path.parent / "secret.txt"
    secret.write_text("must-never-upload")
    secret.chmod(0o600)
    journal = path.parent / "fault.jsonl"
    journal.write_text('{"record":"fixture"}')
    journal.chmod(0o600)
    api = MagicMock()
    prepared = _prepared()
    upload_archive(api, prepared, "faults", (path, journal))
    sent = api.call.call_args.kwargs
    assert sent["Key"] == "evidence/app-worker/faults.tar.gz"
    assert sent["ExpectedBucketOwner"] == prepared.account_id
    assert sent["IfNoneMatch"] == "*"
    body = sent["Body"]
    stream = StreamingBody(io.BytesIO(body), len(body))
    api.call.return_value = ArchiveObject(
        body=stream, length=len(body), metadata={"sha256": sha256(body).hexdigest()}
    )
    outputs = download_archive(api, prepared, "faults", files.output("received"))
    assert [item.name for item in outputs] == ["fault-status.json", "fault.jsonl"]
    assert outputs[0].read_bytes() == path.read_bytes()
    assert outputs[0].stat().st_mode & 0o777 == 0o600
    assert stream._raw_stream.closed


@pytest.mark.parametrize(
    "name,kind",
    [("../escape", tarfile.REGTYPE), ("link", tarfile.SYMTYPE), ("/absolute", tarfile.REGTYPE)],
)
def test_untrusted_archive_paths_and_links_are_rejected(
    tmp_path: Path, name: str, kind: bytes
) -> None:
    files = _files(tmp_path)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        item = tarfile.TarInfo(name)
        item.size = 1
        item.type = kind
        item.linkname = "../secret"
        archive.addfile(item, io.BytesIO(b"x"))
    body = output.getvalue()
    api = MagicMock()
    api.call.return_value = ArchiveObject(
        body=StreamingBody(io.BytesIO(body), len(body)),
        length=len(body),
        metadata={"sha256": sha256(body).hexdigest()},
    )
    with pytest.raises(OrchestratorError, match="archive_member_invalid"):
        download_archive(api, _prepared(), "faults", files.output("received"))


@pytest.mark.parametrize(
    "length,digest,code",
    [(MAX_ARCHIVE_BYTES + 1, "a" * 64, "too_large"), (1, "a" * 64, "checksum_invalid")],
)
def test_archive_stream_closes_on_length_or_checksum_refusal(
    tmp_path: Path, length: int, digest: str, code: str
) -> None:
    files = _files(tmp_path)
    stream = StreamingBody(io.BytesIO(b"x"), 1)
    api = MagicMock()
    api.call.return_value = ArchiveObject(body=stream, length=length, metadata={"sha256": digest})
    with pytest.raises(OrchestratorError, match=code):
        download_archive(api, _prepared(), "faults", files.output("received"))
    assert stream._raw_stream.closed


def test_compressed_archive_expansion_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _files(tmp_path)
    body = gzip.compress(b"x" * 4096)
    stream = StreamingBody(io.BytesIO(body), len(body))
    api = MagicMock()
    api.call.return_value = ArchiveObject(
        body=stream, length=len(body), metadata={"sha256": sha256(body).hexdigest()}
    )
    monkeypatch.setattr(module, "MAX_ARCHIVE_BYTES", 1024)
    with pytest.raises(OrchestratorError, match="too_large"):
        download_archive(api, _prepared(), "faults", files.output("received"))
    assert stream._raw_stream.closed


def test_missing_fault_journal_is_not_a_successful_archive(tmp_path: Path) -> None:
    files = _files(tmp_path)
    path = files.output("source") / "fault-status.json"
    path.write_text('{"status":"completed"}')
    path.chmod(0o600)
    api = MagicMock()
    prepared = _prepared()
    upload_archive(api, prepared, "faults", (path,))
    body = api.call.call_args.kwargs["Body"]
    api.call.return_value = ArchiveObject(
        body=StreamingBody(io.BytesIO(body), len(body)),
        length=len(body),
        metadata={"sha256": sha256(body).hexdigest()},
    )
    with pytest.raises(OrchestratorError, match="inventory_invalid"):
        download_archive(api, prepared, "faults", files.output("received"))
