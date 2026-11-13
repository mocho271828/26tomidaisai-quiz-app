"""配置済みaws-reviewのデータだけを、明示確認した版で初期化する。"""

import argparse
import fcntl
import json
import os
import stat
import subprocess
from pathlib import Path
from uuid import uuid4

import httpx
import logfire
from botocore.exceptions import BotoCoreError, ClientError
from cryptography.fernet import InvalidToken
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from quiz_app.aws_demo_paths import (
    RUNTIME_ENVIRONMENT_FILE_POLICY,
    AwsDemoDeploymentValidationError,
    read_validated_external_file,
    validate_external_regular_file,
)
from quiz_app.dependency_hosts import DependencyHostValidationError
from quiz_app.rehearsal.preparation import RehearsalPreparationRefusedError
from validation.aws_review import host
from validation.aws_review.diagnostics import configure_logging, log_failure, progress
from validation.aws_review.host_contracts import HostInput, HostRefusedError, require_ownership
from validation.aws_review.host_data import (
    BACKUP_DIRECTORY,
    CONFIG_DIRECTORY,
    POSTGRES_CA_FILE,
    RELEASE_FILE,
    RUNTIME_FILE,
    load_secrets,
)
from validation.aws_review.host_update import validate_existing_host
from validation.aws_review.models import ResetInput
from validation.aws_review.reset_archive import capture_reset_archive, write_reset_archive
from validation.aws_review.reset_data import inspect_reset_event, reset_event_data
from validation.rehearsal import _require_release_source

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
HOST_INPUT_FILE = CONFIG_DIRECTORY / "review-input.json"
SERVICES = ["gateway", "api", "api-secondary"]
HOST_ERRORS = (
    OSError,
    UnicodeError,
    ValidationError,
    ValueError,
    HostRefusedError,
    AwsDemoDeploymentValidationError,
    RehearsalPreparationRefusedError,
    DependencyHostValidationError,
    SQLAlchemyError,
    BotoCoreError,
    ClientError,
    httpx.HTTPError,
    subprocess.SubprocessError,
    InvalidToken,
)


def _read_input(path: Path) -> bytes:
    validated = validate_external_regular_file(
        path, REPOSITORY_ROOT, RUNTIME_ENVIRONMENT_FILE_POLICY
    )
    return read_validated_external_file(
        validated, maximum_bytes=16_384, unavailable_code="review_reset_input_unavailable"
    )


def reset_host(request: ResetInput) -> None:
    """対象一致を停止前と削除transaction内で検証し、成功した場合だけ公開を再開する。"""
    if REPOSITORY_ROOT != Path("/opt/gs-app-tomidaisai") or (os.geteuid(), os.getegid()) != (
        1000,
        1000,
    ):
        raise HostRefusedError("review_reset_host_execution_context_invalid")
    _require_release_source(REPOSITORY_ROOT, request.source_commit)
    inputs = HostInput.model_validate_json(_read_input(HOST_INPUT_FILE))
    if any(
        getattr(inputs, field) != getattr(request, field)
        for field in ("account_id", "region", "demo_id", "source_commit")
    ):
        raise HostRefusedError("review_reset_host_input_mismatch")
    progress("host_ownership")
    require_ownership(inputs)
    context = host._command(["/usr/bin/docker", "context", "show"], timeout=10)
    existing = validate_existing_host(
        inputs,
        REPOSITORY_ROOT,
        execution_host=host._command(["/usr/bin/hostname", "-f"], timeout=10),
        docker_context=context,
        docker_endpoint=host._command(
            [
                "/usr/bin/docker",
                "context",
                "inspect",
                context,
                "--format",
                "{{.Endpoints.docker.Host}}",
            ],
            timeout=10,
        ),
    )
    release, settings = existing.release, existing.settings
    if (
        release.source_commit != request.source_commit
        or release.event_id != request.expected_event_id
        or release.api_image != inputs.api_image
        or release.gateway_image != inputs.gateway_image
    ):
        raise HostRefusedError("review_reset_release_mismatch")
    inspect_reset_event(
        settings,
        request.expected_event_id,
        request.expected_event_version,
        postgres_ca=POSTGRES_CA_FILE,
    )
    secrets = load_secrets(inputs)
    archive_key = settings.round_backup_encryption_key
    if (
        archive_key is None
        or secrets.initialization.app_database_url.get_secret_value() != settings.database_url
        or secrets.redis_url.get_secret_value() != settings.redis_url
        or secrets.session_secret != settings.session_secret
        or secrets.backup_key != settings.round_backup_encryption_key
    ):
        raise HostRefusedError("review_reset_credentials_mismatch")
    host._compose(inputs, ["config", "--quiet"])
    reset_id = uuid4()
    archive_directory = BACKUP_DIRECTORY.parent / f"event-reset-{reset_id.hex}"

    def preserve_data(session: Session) -> None:
        archive = capture_reset_archive(
            session,
            reset_id=reset_id,
            source_commit=request.source_commit,
            event_id=request.expected_event_id,
            event_version=request.expected_event_version,
        )
        digest = write_reset_archive(
            archive,
            directory=archive_directory,
            repository_root=REPOSITORY_ROOT,
            protected_paths=(RELEASE_FILE, RUNTIME_FILE, POSTGRES_CA_FILE),
            encryption_key=archive_key,
        )
        logfire.info("Event reset archive verified", reset_id=reset_id, checksum=digest)

    completed = False
    progress("host_stopping")
    try:
        host._compose(inputs, ["stop", "--timeout", "30", *SERVICES])
        progress("database_reset")
        result = reset_event_data(
            settings,
            secrets,
            request.expected_event_id,
            request.expected_event_version,
            reset_id=reset_id,
            before_delete=preserve_data,
            postgres_ca=POSTGRES_CA_FILE,
        )
        progress("host_preflight")
        host._command(
            [
                "/bin/bash",
                str(REPOSITORY_ROOT / "scripts/aws-demo-preflight.sh"),
                "--release-file",
                str(RELEASE_FILE),
            ],
            timeout=300,
        )
        progress("host_starting")
        host._compose(
            inputs,
            [
                "up",
                "-d",
                "--no-build",
                "--pull",
                "never",
                "--wait",
                "--wait-timeout",
                "180",
                "api",
                "api-secondary",
                "gateway",
            ],
        )
        host._verify_api_readiness(inputs)
        completed = True
        progress("host_ready")
        print(
            json.dumps(
                {
                    "status": "reset",
                    "reset_id": str(reset_id),
                    "archive_directory": str(archive_directory),
                    **result.model_dump(mode="json"),
                }
            )
        )
    finally:
        if not completed:
            # DB確定後の起動失敗も再初期化せず停止を維持し、保全記録から人が判断する。
            try:
                host._compose(inputs, ["stop", "--timeout", "30", *SERVICES])
            except HOST_ERRORS as stop_error:
                log_failure("review_host_failed", stop_error)


def main(argv: list[str] | None = None) -> int:
    """既存deployと同じhost lock下で実行し、秘密を含む例外本文を出さない。"""
    parser = argparse.ArgumentParser(description="AWS確認環境のEventデータ初期化")
    parser.add_argument("--reset-input", type=Path, required=True)
    arguments = parser.parse_args(argv)
    configure_logging()
    descriptor: int | None = None
    try:
        request = ResetInput.model_validate_json(_read_input(arguments.reset_input))
        descriptor = os.open(
            CONFIG_DIRECTORY / "review-host.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_nlink != 1
            or status.st_uid != os.geteuid()
            or stat.S_IMODE(status.st_mode) != 0o600
        ):
            raise HostRefusedError("review_reset_lock_unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        reset_host(request)
    except HOST_ERRORS as error:
        log_failure("review_host_failed", error)
        return 2
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
