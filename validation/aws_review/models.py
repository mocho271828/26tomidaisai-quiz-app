"""GitHubの設定値をAWS操作前に検証する。"""

from datetime import datetime, timedelta
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    TypeAdapter,
    ValidationInfo,
    field_validator,
    model_validator,
)

REVIEW_DOMAIN_PATTERN = r"^$|^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
ReviewDomainName = Annotated[str, Field(pattern=REVIEW_DOMAIN_PATTERN, max_length=253)]


class ReviewTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    account_id: str = Field(pattern=r"^[0-9]{12}$")
    demo_id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=21)
    region: Literal["ap-northeast-1"] = "ap-northeast-1"

    @property
    def prefix(self) -> str:
        return f"gs-rehearsal-{self.demo_id}"


class ResetInput(ReviewTarget):
    """対象開催・確認時点の版を指定し、古い要求による再初期化を拒否する。"""

    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    expected_event_id: UUID
    expected_event_version: int = Field(ge=1, le=2_147_483_646)
    confirmation: Literal["RESET EVENT"]

    @field_validator("expected_event_version", mode="before")
    @classmethod
    def require_integer_version(cls, value: object) -> object:
        if isinstance(value, bool) or isinstance(value, float):
            raise ValueError("review_reset_version_integer_required")
        if isinstance(value, str) and (not value.isascii() or not value.isdecimal()):
            raise ValueError("review_reset_version_integer_required")
        return value


class EventStatusInput(ReviewTarget):
    """照会コードのcheckoutだけを固定し、配置済みsourceの更新は要求しない。"""

    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")


class DeploymentInput(ReviewTarget):
    deployment_mode: Literal["create", "update"] = "update"
    monitoring_expires_at: AwareDatetime | None = Field(default=None, validate_default=True)
    app_instance_type: Literal["c7i.xlarge", "t3.medium"] = "c7i.xlarge"
    public_entrypoint: Literal["custom-domain", "cloudfront"] = "custom-domain"
    review_domain_name: ReviewDomainName = Field(default="", validate_default=True)
    review_certificate_arn: str = Field(
        default="",
        validate_default=True,
        pattern=r"^$|^arn:aws:acm:us-east-1:[0-9]{12}:certificate/"
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    )
    app_az: str = Field(pattern=r"^ap-northeast-1[a-z]$")
    secondary_az: str = Field(pattern=r"^ap-northeast-1[a-z]$")
    ubuntu_ami_id: str = Field(pattern=r"^ami-[0-9a-f]{17}$")
    database_engine_version: str = Field(pattern=r"^16\.[0-9]+$")
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    vite_event_start_at: str = Field(
        default="",
        pattern=r"^$|^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}"
        r"(:[0-9]{2}(\.[0-9]+)?)?(Z|[+-][0-9]{2}:[0-9]{2})$",
    )
    vite_survey_url: str = Field(default="", max_length=2_048, pattern=r"^[^\x00-\x20\x7f]*$")
    vite_home_url: str = Field(default="", max_length=2_048, pattern=r"^[^\x00-\x20\x7f]*$")

    @field_validator("monitoring_expires_at")
    @classmethod
    def require_create_expiry(
        cls, value: AwareDatetime | None, info: ValidationInfo
    ) -> AwareDatetime | None:
        if info.data.get("deployment_mode") == "create" and value is None:
            raise ValueError("review_create_monitoring_expiry_required")
        return value

    @property
    def public_url(self) -> str | None:
        if self.public_entrypoint == "cloudfront":
            return None
        return f"https://{self.review_domain_name}"

    @field_validator("review_domain_name", "review_certificate_arn", mode="before")
    @classmethod
    def ignore_unused_custom_domain_settings(cls, value: object, info: ValidationInfo) -> object:
        """標準URLの配置では、Environmentに残る独自ドメイン設定へ依存しない。"""
        return "" if info.data.get("public_entrypoint") == "cloudfront" else value

    @field_validator("review_domain_name")
    @classmethod
    def require_custom_domain(cls, value: str, info: ValidationInfo) -> str:
        if info.data.get("public_entrypoint") != "cloudfront" and not value:
            raise ValueError("review_custom_domain_required")
        return value

    @field_validator("review_certificate_arn")
    @classmethod
    def validate_certificate_account(cls, value: str, info: ValidationInfo) -> str:
        if not value:
            if info.data.get("public_entrypoint") != "cloudfront":
                raise ValueError("review_custom_certificate_required")
            return value
        if "account_id" in info.data and value.split(":")[4] != info.data["account_id"]:
            raise ValueError("review_certificate_account_mismatch")
        return value

    @field_validator("vite_event_start_at")
    @classmethod
    def validate_event_start_at(cls, value: str) -> str:
        if value != "":
            TypeAdapter(AwareDatetime).validate_python(value)
        return value

    @field_validator("vite_survey_url", "vite_home_url")
    @classmethod
    def validate_exit_url(cls, value: str, info: ValidationInfo) -> str:
        if value == "":
            return value
        url = HttpUrl(value)
        if (
            url.scheme != "https"
            or url.username is not None
            or url.password is not None
            or url.host is None
            or url.host.rstrip(".") == info.data.get("review_domain_name")
        ):
            raise ValueError("review_external_https_url_required")
        return value

    @model_validator(mode="after")
    def distinct_zones(self) -> Self:
        if self.app_az == self.secondary_az:
            raise ValueError("review_distinct_zones_required")
        return self


def validate_monitoring_deadline(config: DeploymentInput, *, now: datetime) -> None:
    """明示した期限だけを検証し、update の空欄は既存期限の維持として扱う。"""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("review_timezone_required")
    expiry = config.monitoring_expires_at
    if expiry is not None and not now < expiry <= now + timedelta(hours=12):
        raise ValueError("review_monitoring_expiry_must_be_within_12_hours")
