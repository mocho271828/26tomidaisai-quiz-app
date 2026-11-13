"""公開ドメインの証明書と、配置後に案内するDNS情報を検証する。"""

from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from quiz_app.load_test_aws.aws import ApiModel, AwsApi
from validation.aws_review.models import DeploymentInput, ReviewDomainName


class Certificate(ApiModel):
    arn: str = Field(alias="CertificateArn")
    status: Literal["ISSUED"] = Field(alias="Status")
    type: Literal["AMAZON_ISSUED"] = Field(alias="Type")
    names: tuple[str, ...] = Field(alias="SubjectAlternativeNames")
    not_before: AwareDatetime = Field(alias="NotBefore")
    not_after: AwareDatetime = Field(alias="NotAfter")


class CertificateResponse(ApiModel):
    certificate: Certificate = Field(alias="Certificate")


def validate_certificate(api: AwsApi, config: DeploymentInput) -> None:
    """有効な専用証明書を確認し、未検証の証明書でリソース作成へ進ませない。"""
    certificate = api.call(
        "acm",
        "describe_certificate",
        CertificateResponse,
        CertificateArn=config.review_certificate_arn,
    ).certificate
    if certificate.arn != config.review_certificate_arn:
        raise ValueError("review_certificate_identity_mismatch")
    if config.review_domain_name not in certificate.names:
        raise ValueError("review_certificate_domain_mismatch")
    if not certificate.not_before <= datetime.now(UTC) < certificate.not_after:
        raise ValueError("review_certificate_not_valid_now")


class DeploymentResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    public_entrypoint: Literal["custom-domain", "cloudfront"] = "custom-domain"
    review_domain_name: ReviewDomainName = Field(default="", exclude=True)
    url: str
    cloudfront_domain_name: str = Field(pattern=r"^d[a-z0-9]{1,62}\.cloudfront\.net$")

    @model_validator(mode="after")
    def require_selected_public_url(self) -> Self:
        expected_url = (
            f"https://{self.cloudfront_domain_name}"
            if self.public_entrypoint == "cloudfront"
            else f"https://{self.review_domain_name}"
        )
        if self.url != expected_url or (
            self.public_entrypoint == "custom-domain" and not self.review_domain_name
        ):
            raise ValueError("review_public_url_mismatch")
        return self

    def dns_summary(self) -> str:
        """選択した公開入口に必要な案内だけを検証済みの公開情報から生成する。"""
        if self.public_entrypoint == "cloudfront":
            return (
                f"\nCloudFront標準URL: [{self.url}]({self.url})\n\n"
                "参加者・運営・両PCのRunnerはこのURLを使用してください。\n"
                "AWS配置は完了しましたが、公開URLの疎通は未確認です。\n"
            )
        return (
            "\nDNS管理画面に次のアクセス用レコードを設定してください。\n\n"
            "| ホスト名 | 種別 | 内容 |\n| --- | --- | --- |\n"
            f"| {self.review_domain_name} | CNAME | {self.cloudfront_domain_name} |\n\n"
            "同名のAレコードをこのCNAMEに置き換えます。ACM検証用CNAMEは残してください。\n"
            "AWS配置は完了しましたが、DNS反映・公開URLの疎通は未確認です。\n"
        )
