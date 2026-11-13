from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from ipaddress import IPv4Address, IPv4Network
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

REGION = "ap-northeast-1"
PROJECT = "gs-app-tomidaisai"
PURPOSE = "load-test-aws-only"
RUN_SECONDS = 4_800
EXPIRY_SECONDS = 10_800
BUDGET_SECONDS = 14_400
BUDGET_JPY = Decimal("1000")
HOURLY_USD = Decimal("0.9484333333333333333333333333")
VARIABLE_RESERVE_USD = Decimal("0.50")
EXCHANGE_RATE = Decimal("200")
SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SOURCE_COMMIT = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
INSTANCE_ID = Annotated[str, Field(pattern=r"^i-[0-9a-f]{8,17}$")]
ACCOUNT_ID = Annotated[str, Field(pattern=r"^[0-9]{12}$")]
_PRIVATE_NETWORKS = tuple(
    IPv4Network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("aws_load_test_utc_required")
    return value.astimezone(UTC)


class AwsRunPlan(StrictModel):
    """作成前に確定し、期限後に変更しない実行契約。秘密値は保持しない。"""

    schema_version: Literal["aws-load-test-plan-v1"] = "aws-load-test-plan-v1"
    run_id: UUID
    account_id: ACCOUNT_ID
    region: Literal["ap-northeast-1"] = REGION
    source_commit: SOURCE_COMMIT
    manifest_sha256: SHA256
    created_at: datetime
    notification_email: SecretStr
    ami_id: Annotated[str, Field(pattern=r"^ami-[0-9a-f]{8,17}$")]
    availability_zone: Annotated[str, Field(pattern=r"^ap-northeast-1[a-d]$")]
    secondary_availability_zone: Annotated[str, Field(pattern=r"^ap-northeast-1[a-d]$")]
    worker_package_sha256: SHA256
    schema_revision: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_]{1,64}$")]
    cloudformation_role_arn: (
        Annotated[str, Field(pattern=r"^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$")] | None
    ) = None

    _validate_time = field_validator("created_at")(utc_datetime)

    @field_validator("notification_email")
    @classmethod
    def validate_email(cls, value: SecretStr) -> SecretStr:
        email = value.get_secret_value()
        if not 3 <= len(email) <= 254 or email.count("@") != 1 or any(c.isspace() for c in email):
            raise ValueError("aws_load_test_notification_email_invalid")
        if "." not in email.rsplit("@", 1)[1]:
            raise ValueError("aws_load_test_notification_email_invalid")
        return value

    @model_validator(mode="after")
    def different_zones(self) -> AwsRunPlan:
        if self.availability_zone == self.secondary_availability_zone:
            raise ValueError("aws_load_test_subnets_require_two_zones")
        if (
            self.cloudformation_role_arn is not None
            and self.cloudformation_role_arn.split(":")[4] != self.account_id
        ):
            raise ValueError("aws_load_test_cloudformation_role_account_mismatch")
        return self

    @property
    def prefix(self) -> str:
        return f"gs-lt-{self.run_id.hex}"

    @property
    def execution_role_arn(self) -> str:
        return (
            self.cloudformation_role_arn
            or f"arn:aws:iam::{self.account_id}:role/{self.prefix}-cloudformation"
        )

    @property
    def workload_stack(self) -> str:
        return f"{self.prefix}-workload"

    @property
    def control_stack(self) -> str:
        return f"{self.prefix}-control"

    @property
    def expires_at(self) -> datetime:
        return self.created_at + timedelta(seconds=EXPIRY_SECONDS)

    @property
    def cleanup_budget_at(self) -> datetime:
        return self.created_at + timedelta(seconds=BUDGET_SECONDS)

    @property
    def estimated_total_jpy(self) -> Decimal:
        return ((HOURLY_USD * 4 + VARIABLE_RESERVE_USD) * EXCHANGE_RATE * Decimal("1.1")).quantize(
            Decimal("0.01"), rounding=ROUND_CEILING
        )

    def require_creation_window(self, now: datetime) -> None:
        if (
            not self.created_at
            <= utc_datetime(now)
            < self.expires_at - timedelta(seconds=RUN_SECONDS + 600)
        ):
            raise ValueError("aws_load_test_insufficient_remaining_time")
        if self.estimated_total_jpy > BUDGET_JPY:
            raise ValueError("aws_load_test_budget_exceeded")

    def cleanup_input(self) -> CleanupInput:
        return CleanupInput(
            run_id=self.run_id, account_id=self.account_id, expires_at=self.expires_at
        )


class CleanupInput(StrictModel):
    run_id: UUID
    account_id: ACCOUNT_ID
    region: Literal["ap-northeast-1"] = REGION
    expires_at: datetime

    _validate_time = field_validator("expires_at")(utc_datetime)

    @property
    def prefix(self) -> str:
        return f"gs-lt-{self.run_id.hex}"

    @property
    def workload_stack(self) -> str:
        return f"{self.prefix}-workload"


def execution_input(run: CleanupInput) -> str:
    """Scheduler・手動・runnerで同一文字列を使用し、STANDARDの冪等性を保つ。"""
    return json.dumps({"run": run.model_dump(mode="json")}, separators=(",", ":"))


def deadline_target(run: CleanupInput) -> dict[str, str]:
    return {
        "StateMachineArn": (
            f"arn:aws:states:{run.region}:{run.account_id}:stateMachine:{run.prefix}-cleanup"
        ),
        "Name": "cleanup",
        "Input": execution_input(run),
    }


class AwsPreparationEvidence(StrictModel):
    schema_version: Literal["aws-load-test-preparation-v1"] = "aws-load-test-preparation-v1"
    run_id: UUID
    account_id: ACCOUNT_ID
    region: Literal["ap-northeast-1"] = REGION
    source_commit: SOURCE_COMMIT
    manifest_sha256: SHA256
    app_instance_id: INSTANCE_ID
    runner_instance_id: INSTANCE_ID
    app_private_ip: IPv4Address
    runner_private_ip: IPv4Address
    public_origin: str
    ca_sha256: SHA256
    prepared_at: datetime
    expires_at: datetime

    _validate_time = field_validator("prepared_at", "expires_at")(utc_datetime)

    @model_validator(mode="after")
    def validate_binding(self) -> AwsPreparationEvidence:
        for address in (self.app_private_ip, self.runner_private_ip):
            if not any(address in network for network in _PRIVATE_NETWORKS):
                raise ValueError("aws_load_test_private_address_required")
        if (
            self.app_private_ip == self.runner_private_ip
            or self.app_instance_id == self.runner_instance_id
        ):
            raise ValueError("aws_load_test_independent_runner_required")
        if (
            self.public_origin != f"https://{self.app_private_ip}"
            or self.prepared_at >= self.expires_at
        ):
            raise ValueError("aws_load_test_preparation_binding_invalid")
        return self


class SchemaReceipt(StrictModel):
    """人手作業後のread-only DB verifierが出力した準備確認。"""

    schema_version: Literal["aws-load-test-schema-v1"] = "aws-load-test-schema-v1"
    run_id: UUID
    account_id: ACCOUNT_ID
    schema_revision: str
    verified_at: datetime
    schema_applied_by_human: Literal[True]
    application_role_is_nonowner: Literal[True]
    verifier_role_is_read_only: Literal[True]
    single_event_count: Literal[1]

    _validate_time = field_validator("verified_at")(utc_datetime)


class Residual(StrictModel):
    kind: str
    identifier: str
    state: str


class CleanupResult(StrictModel):
    run_id: UUID
    done: bool
    observed_at: datetime
    residuals: tuple[Residual, ...]
    stack_status: str
