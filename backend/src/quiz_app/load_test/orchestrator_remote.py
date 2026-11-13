from __future__ import annotations

import gzip
import io
import os
import tarfile
import threading
import zlib
from concurrent.futures import Future
from hashlib import sha256
from pathlib import Path
from typing import Literal

from botocore.exceptions import BotoCoreError
from botocore.response import StreamingBody
from pydantic import ConfigDict, Field

from quiz_app.load_test.db_verifier_artifacts import read_secure_external_file
from quiz_app.load_test.orchestrator_contracts import MAX_METRICS_ARTIFACT_BYTES, OrchestratorError
from quiz_app.load_test_aws.aws import ApiModel, AwsApi, AwsCallError, EmptyResponse
from quiz_app.load_test_aws.models import AwsPreparationEvidence
from quiz_app.load_test_aws.responses import CommandResponse

MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
AppAction = Literal["faults", "export"]


class CommandInvocation(ApiModel):
    status: Literal[
        "Pending",
        "InProgress",
        "Delayed",
        "Success",
        "Cancelled",
        "TimedOut",
        "Failed",
        "Cancelling",
    ] = Field(alias="Status")
    response_code: int = Field(alias="ResponseCode")


class ArchiveObject(ApiModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, arbitrary_types_allowed=True)
    body: StreamingBody = Field(alias="Body")
    length: int = Field(alias="ContentLength", ge=1)
    metadata: dict[str, str] = Field(alias="Metadata")


def evidence_bucket(prepared: AwsPreparationEvidence) -> str:
    return f"gs-lt-{prepared.run_id.hex}-{prepared.account_id}"


def start_app_worker(api: AwsApi, prepared: AwsPreparationEvidence, action: AppAction) -> str:
    result = api.call(
        "ssm",
        "send_command",
        CommandResponse,
        InstanceIds=[prepared.app_instance_id],
        DocumentName=f"gs-lt-{prepared.run_id.hex}-app-worker",
        Parameters={"Action": [action]},
        TimeoutSeconds=120,
        CloudWatchOutputConfig={"CloudWatchOutputEnabled": False},
    )
    return result.command.identifier


def app_worker_status(api: AwsApi, prepared: AwsPreparationEvidence, command_id: str) -> bool:
    """eventual consistencyだけを待機へ変換し、それ以外の失敗は停止する。"""
    try:
        result = api.call(
            "ssm",
            "get_command_invocation",
            CommandInvocation,
            CommandId=command_id,
            InstanceId=prepared.app_instance_id,
        )
    except AwsCallError as error:
        if error.code == "InvocationDoesNotExist":
            return False
        raise
    if result.status in {"Pending", "InProgress", "Delayed"}:
        return False
    if result.status != "Success" or result.response_code != 0:
        raise OrchestratorError("orchestrator_app_worker_failed")
    return True


def monitor_app_worker(
    api: AwsApi, prepared: AwsPreparationEvidence, command_id: str, stop: threading.Event
) -> Future[bool]:
    """SSM待機をscheduleの狭い時刻windowから分離する。"""
    result: Future[bool] = Future()

    def observe() -> None:
        try:
            while not stop.is_set():
                if app_worker_status(api, prepared, command_id):
                    result.set_result(True)
                    return
                stop.wait(5)
            result.cancel()
        except (ValueError, RuntimeError, OSError, BotoCoreError) as error:
            result.set_exception(error)

    threading.Thread(target=observe, name="load-test-app-monitor", daemon=True).start()
    return result


def upload_archive(
    api: AwsApi, prepared: AwsPreparationEvidence, action: AppAction, paths: tuple[Path, ...]
) -> None:
    """秘密入力を含まない明示artifactだけを、run専用S3へ転送する。"""
    output = io.BytesIO()
    total = 0
    names: set[str] = set()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for path in paths:
            if path.name in names:
                raise OrchestratorError("orchestrator_duplicate_artifact")
            names.add(path.name)
            body, _ = read_secure_external_file(path, maximum_bytes=MAX_METRICS_ARTIFACT_BYTES)
            total += len(body)
            if total > MAX_ARCHIVE_BYTES:
                raise OrchestratorError("orchestrator_archive_too_large")
            info = tarfile.TarInfo(path.name)
            info.size = len(body)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(body))
    body = output.getvalue()
    if not names or len(body) > MAX_ARCHIVE_BYTES:
        raise OrchestratorError("orchestrator_archive_invalid")
    api.call(
        "s3",
        "put_object",
        EmptyResponse,
        Bucket=evidence_bucket(prepared),
        Key=f"evidence/app-worker/{action}.tar.gz",
        Body=body,
        ContentType="application/gzip",
        ServerSideEncryption="AES256",
        ExpectedBucketOwner=prepared.account_id,
        IfNoneMatch="*",
        Metadata={"sha256": sha256(body).hexdigest()},
    )


def download_archive(
    api: AwsApi, prepared: AwsPreparationEvidence, action: AppAction, directory: Path
) -> tuple[Path, ...]:
    result = api.call(
        "s3",
        "get_object",
        ArchiveObject,
        Bucket=evidence_bucket(prepared),
        Key=f"evidence/app-worker/{action}.tar.gz",
        ExpectedBucketOwner=prepared.account_id,
    )
    try:
        if result.length > MAX_ARCHIVE_BYTES:
            raise OrchestratorError("orchestrator_archive_too_large")
        body = result.body.read(MAX_ARCHIVE_BYTES + 1)
    finally:
        result.body.close()
    if len(body) != result.length or sha256(body).hexdigest() != result.metadata.get("sha256"):
        raise OrchestratorError("orchestrator_archive_checksum_invalid")
    try:
        paths = _unpack_archive(body, directory)
    except (tarfile.TarError, gzip.BadGzipFile, EOFError, zlib.error):
        raise OrchestratorError("orchestrator_archive_invalid") from None
    expected = (
        {"fault.jsonl", "fault-status.json"}
        if action == "faults"
        else {f"metrics-{prepared.run_id}.{suffix}" for suffix in ("json", "csv", "sha256")}
    )
    if {path.name for path in paths} != expected:
        raise OrchestratorError("orchestrator_archive_inventory_invalid")
    return paths


def _unpack_archive(body: bytes, directory: Path) -> tuple[Path, ...]:
    with gzip.GzipFile(fileobj=io.BytesIO(body), mode="rb") as compressed:
        expanded = compressed.read(MAX_ARCHIVE_BYTES + 1)
    if len(expanded) > MAX_ARCHIVE_BYTES:
        raise OrchestratorError("orchestrator_archive_too_large")
    paths: list[Path] = []
    total = 0
    # extractallを使わず、flat regular fileだけを排他的に保存する。
    with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
        for member in archive:
            if (
                len(paths) >= 20
                or not member.isfile()
                or Path(member.name).name != member.name
                or member.name in {"", ".", ".."}
                or member.size < 1
            ):
                raise OrchestratorError("orchestrator_archive_member_invalid")
            total += member.size
            if total > MAX_ARCHIVE_BYTES:
                raise OrchestratorError("orchestrator_archive_too_large")
            source = archive.extractfile(member)
            if source is None:
                raise OrchestratorError("orchestrator_archive_member_invalid")
            with source:
                content = source.read(member.size + 1)
            if len(content) != member.size:
                raise OrchestratorError("orchestrator_archive_member_invalid")
            path = directory / member.name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as destination:
                destination.write(content)
                destination.flush()
                os.fsync(destination.fileno())
            paths.append(path)
    if not paths:
        raise OrchestratorError("orchestrator_archive_empty")
    return tuple(paths)
