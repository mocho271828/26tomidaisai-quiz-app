"""配置済みhostから開催の識別子と現在版だけを読み取り、SSMへ返す。"""

import argparse
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID

import httpx
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from quiz_app.aws_demo_paths import (
    RUNTIME_ENVIRONMENT_FILE_POLICY,
    AwsDemoDeploymentValidationError,
    read_validated_external_file,
    validate_external_regular_file,
)
from quiz_app.config import Settings
from quiz_app.dependency_hosts import (
    DependencyHostValidationError,
    PostgresConnectArgs,
    create_pinned_postgres_engine,
    validate_remote_dependency_host,
)
from quiz_app.models import Event
from quiz_app.models.enums import EventState
from quiz_app.rehearsal.preparation import RehearsalPreparationRefusedError
from validation.aws_review import host
from validation.aws_review.diagnostics import configure_logging, log_failure
from validation.aws_review.host_contracts import HostInput, HostRefusedError, require_ownership
from validation.aws_review.host_data import CONFIG_DIRECTORY, POSTGRES_CA_FILE
from validation.aws_review.host_update import validate_existing_host
from validation.rehearsal import _require_release_source

# runnerがこのsourceを直接渡すため、配置先に新規moduleや__file__を要求しない。
REPOSITORY_ROOT = Path("/opt/gs-app-tomidaisai")
HOST_INPUT_FILE = CONFIG_DIRECTORY / "review-input.json"
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
)


class EventStatusRequest(BaseModel):
    """runnerが所有検証したEC2と、host自身の入力を照合する。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    account_id: str = Field(pattern=r"^[0-9]{12}$")
    demo_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", min_length=1, max_length=21)
    region: Literal["ap-northeast-1"] = "ap-northeast-1"
    instance_id: str = Field(pattern=r"^i-[a-f0-9]{17}$")


class EventStatusResult(BaseModel):
    """公開可能な開催情報だけに限定したSSM応答。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    event_id: UUID
    event_version: int = Field(ge=1, le=2_147_483_647)
    event_state: EventState
    observed_at: AwareDatetime
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")

    @field_validator("observed_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("review_event_status_utc_required")
        return value


class _DatabaseIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    database_name: Literal["quiz"]
    current_role: Literal["rehearsal_app"]
    session_role: Literal["rehearsal_app"]
    read_only: Literal["on"]


class _EventRow(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    event_id: UUID
    event_version: int = Field(ge=1, le=2_147_483_647)
    event_state: EventState


def _read_host_input() -> HostInput:
    validated = validate_external_regular_file(
        HOST_INPUT_FILE, REPOSITORY_ROOT, RUNTIME_ENVIRONMENT_FILE_POLICY
    )
    content = read_validated_external_file(
        validated, maximum_bytes=16_384, unavailable_code="review_event_status_input_unavailable"
    )
    return HostInput.model_validate_json(content)


def _create_readonly_engine(database_url: str) -> Engine:
    validated_host = validate_remote_dependency_host("database", database_url)

    def host_engine(
        url: str, *, pool_pre_ping: bool, pool_recycle: int, connect_args: PostgresConnectArgs
    ) -> Engine:
        return create_engine(
            url,
            pool_pre_ping=pool_pre_ping,
            pool_recycle=pool_recycle,
            connect_args={
                **connect_args,
                "sslmode": "verify-full",
                "sslrootcert": str(POSTGRES_CA_FILE),
                "options": "-c default_transaction_read_only=on "
                "-c statement_timeout=30000 -c lock_timeout=5000",
            },
        )

    return create_pinned_postgres_engine(database_url, validated_host, engine_factory=host_engine)


def _read_event(settings: Settings, event_id: UUID, source_commit: str) -> EventStatusResult:
    engine = _create_readonly_engine(settings.database_url)
    try:
        with Session(engine) as session:
            session.execute(text("SET TRANSACTION READ ONLY"))
            session.execute(text("SET LOCAL statement_timeout = '30s'"))
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
            identity = (
                session.execute(
                    text(
                        "SELECT current_database() AS database_name, current_user AS current_role, "
                        "session_user AS session_role, "
                        "current_setting('transaction_read_only') AS read_only"
                    )
                )
                .mappings()
                .one()
            )
            _DatabaseIdentity.model_validate(dict(identity))
            # 単一開催の確認に必要な最大2行だけ読み、参加者や回答などは取得しない。
            records = session.execute(
                select(
                    Event.id.label("event_id"),
                    Event.version.label("event_version"),
                    Event.state.label("event_state"),
                ).limit(2)
            ).mappings()
            events = tuple(_EventRow.model_validate(dict(row)) for row in records)
            if len(events) != 1 or events[0].event_id != event_id:
                raise HostRefusedError("review_event_status_single_event_mismatch")
            return EventStatusResult(
                **events[0].model_dump(),
                observed_at=datetime.now(UTC),
                source_commit=source_commit,
            )
    finally:
        engine.dispose()


def read_event_status(request: EventStatusRequest) -> EventStatusResult:
    """既存hostとの一致を確認し、停止や更新をせず現在の開催を読む。"""
    if Path.cwd().resolve() != REPOSITORY_ROOT or (os.geteuid(), os.getegid()) != (1000, 1000):
        raise HostRefusedError("review_event_status_execution_context_invalid")
    inputs = _read_host_input()
    if any(
        getattr(inputs, field) != getattr(request, field)
        for field in ("account_id", "demo_id", "region", "instance_id")
    ):
        raise HostRefusedError("review_event_status_host_input_mismatch")
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
    release = existing.release
    if (
        release.source_commit != inputs.source_commit
        or release.api_image != inputs.api_image
        or release.gateway_image != inputs.gateway_image
        or existing.settings.aws_demo_expected_event_id != release.event_id
    ):
        raise HostRefusedError("review_event_status_release_mismatch")
    _require_release_source(REPOSITORY_ROOT, release.source_commit)
    return _read_event(existing.settings, release.event_id, release.source_commit)


def main(argv: list[str] | None = None) -> int:
    """成功時は限定JSONだけ、失敗時は既存の安全な診断だけを出力する。"""
    parser = argparse.ArgumentParser(description="AWS確認環境のEvent IDとversionの参照")
    parser.add_argument("--request-json", required=True)
    arguments = parser.parse_args(argv)
    configure_logging()
    try:
        request = EventStatusRequest.model_validate_json(arguments.request_json)
        result = read_event_status(request)
        print(result.model_dump_json(), flush=True)
    except HOST_ERRORS as error:
        log_failure("review_host_failed", error)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
