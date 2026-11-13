from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from test_aws_review_deploy import ACCOUNT, CERTIFICATE_ARN, CONFIG, TARGET, owned_stack
from test_aws_review_deploy import owner_environment as owner_environment
from test_load_test_aws import FakeAws

from quiz_app.load_test_aws import aws
from validation.aws_review import __main__ as cli
from validation.aws_review import deploy, domain
from validation.aws_review.models import DeploymentInput


def certificate() -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "CertificateArn": CERTIFICATE_ARN,
        "Status": "ISSUED",
        "Type": "AMAZON_ISSUED",
        "SubjectAlternativeNames": [CONFIG.review_domain_name],
        "NotBefore": now - timedelta(days=1),
        "NotAfter": now + timedelta(days=30),
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("review_domain_name", ""),
        ("review_domain_name", "https://quiz.example.org"),
        ("review_domain_name", "quiz..example.org"),
        ("review_certificate_arn", ""),
        ("review_certificate_arn", CERTIFICATE_ARN.replace("us-east-1", "ap-northeast-1")),
        ("review_certificate_arn", CERTIFICATE_ARN.replace(ACCOUNT, "999999999999")),
        ("review_certificate_arn", CERTIFICATE_ARN + "\n"),
    ],
)
def test_domain_inputs_fail_closed(field: str, value: str) -> None:
    with pytest.raises(ValidationError) as failure:
        DeploymentInput.model_validate({**CONFIG.model_dump(), field: value})
    assert failure.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize("variable", ["AWS_REVIEW_DOMAIN_NAME", "AWS_REVIEW_CERTIFICATE_ARN"])
def test_destroy_does_not_require_domain_settings(
    owner_environment: None, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    monkeypatch.delenv(variable)
    assert cli.configuration("destroy") == TARGET
    with pytest.raises(ValidationError):
        cli.configuration("deploy")


@pytest.mark.parametrize(
    "field,value",
    [
        ("Status", "PENDING_VALIDATION"),
        ("Status", "EXPIRED"),
        ("Status", "REVOKED"),
        ("Type", "PRIVATE"),
        ("SubjectAlternativeNames", []),
        ("SubjectAlternativeNames", ["different.example.org"]),
        ("SubjectAlternativeNames", ["*.example.org"]),
        ("CertificateArn", CERTIFICATE_ARN.replace(ACCOUNT, "999999999999")),
        ("NotBefore", datetime.now(UTC) + timedelta(days=1)),
        ("NotAfter", datetime.now(UTC) - timedelta(seconds=1)),
        ("NotAfter", None),
    ],
)
def test_invalid_certificate_stops_deploy_before_stack_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    api = FakeAws()
    api.responses[("sts", "get_caller_identity")] = {
        "Account": ACCOUNT,
        "Arn": f"arn:aws:iam::{ACCOUNT}:role/review",
    }
    api.responses[("acm", "describe_certificate")] = {
        "Certificate": {**certificate(), field: value}
    }
    mutation = Mock()
    monkeypatch.setattr(deploy, "apply_stack", mutation)
    monkeypatch.setattr(deploy, "get_stack", Mock(return_value=owned_stack()))
    with pytest.raises((ValueError, ValidationError)):
        deploy.deploy(api, CONFIG, tmp_path)
    mutation.assert_not_called()
    assert [(service, operation) for service, operation, _ in api.calls] == [
        ("sts", "get_caller_identity"),
        ("acm", "describe_certificate"),
    ]


def test_issued_certificate_uses_only_exact_arn() -> None:
    api = FakeAws()
    api.responses[("acm", "describe_certificate")] = {"Certificate": certificate()}
    domain.validate_certificate(api, CONFIG)
    assert api.calls == [("acm", "describe_certificate", {"CertificateArn": CERTIFICATE_ARN})]


@pytest.mark.parametrize(
    "service,region", [("acm", "us-east-1"), ("pricing", "us-east-1"), ("ec2", "ap-northeast-1")]
)
def test_certificate_client_region_without_changing_workload_region(
    monkeypatch: pytest.MonkeyPatch, service: str, region: str
) -> None:
    session = Mock()
    monkeypatch.setattr(aws.boto3, "Session", Mock(return_value=session))
    api = aws.BotoAwsApi()
    session.client.return_value.describe.return_value = {}
    api.call(service, "describe", aws.EmptyResponse)
    assert session.client.call_args.kwargs["region_name"] == region


@pytest.mark.parametrize("public_entrypoint", ["custom-domain", "cloudfront"])
def test_cli_outputs_selected_entrypoint_without_claiming_dns_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    public_entrypoint: str,
) -> None:
    from validation import github_json_check

    config = DeploymentInput.model_validate(
        {**CONFIG.model_dump(), "public_entrypoint": public_entrypoint}
    )
    result = domain.DeploymentResult.model_validate(
        {
            "public_entrypoint": public_entrypoint,
            "review_domain_name": config.review_domain_name,
            "url": (
                "https://d123example.cloudfront.net"
                if public_entrypoint == "cloudfront"
                else CONFIG.public_url
            ),
            "cloudfront_domain_name": "d123example.cloudfront.net",
        }
    )
    monkeypatch.setattr(cli, "configuration", Mock(return_value=config))
    monkeypatch.setattr(cli, "BotoAwsApi", Mock())
    monkeypatch.setattr(cli, "configure_logging", Mock())
    monkeypatch.setattr(github_json_check, "main", Mock(return_value=0))
    monkeypatch.setattr(deploy, "deploy", Mock(return_value=result))
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert cli.main(["deploy"]) == 0
    assert '"dns_verified": false' in capsys.readouterr().out
    summary_text = summary.read_text()
    if public_entrypoint == "cloudfront":
        assert "CloudFront標準URL" in summary_text
        assert "https://d123example.cloudfront.net" in summary_text
        assert all(value not in summary_text for value in ("Xserver", "CNAME", "ACM"))
    else:
        assert (
            f"| {CONFIG.review_domain_name} | CNAME | d123example.cloudfront.net |" in summary_text
        )
    assert "未確認" in summary_text
    assert CERTIFICATE_ARN not in summary_text


@pytest.mark.parametrize(
    "target",
    [
        "",
        "evil.example.org",
        "example.cloudfront.net",
        "d.cloudfront.net",
        "d" + "a" * 63 + ".cloudfront.net",
        "d123.cloudfront.net\n",
        "https://d123.cloudfront.net",
    ],
)
def test_untrusted_dns_target_is_not_rendered(target: str) -> None:
    with pytest.raises(ValidationError):
        domain.DeploymentResult(
            url=CONFIG.public_url,
            review_domain_name=CONFIG.review_domain_name,
            cloudfront_domain_name=target,
        )


@pytest.mark.parametrize(
    "public_entrypoint,url",
    [
        ("custom-domain", "https://d123example.cloudfront.net"),
        ("cloudfront", "https://quiz.example.org"),
        ("cloudfront", "https://other.cloudfront.net"),
        ("cloudfront", "https://d123example.cloudfront.net/"),
        ("cloudfront", "https://d123example.cloudfront.net.evil.example"),
    ],
)
def test_public_url_must_match_selected_entrypoint(public_entrypoint: str, url: str) -> None:
    with pytest.raises(ValidationError, match="review_public_url_mismatch"):
        domain.DeploymentResult.model_validate(
            {
                "public_entrypoint": public_entrypoint,
                "review_domain_name": CONFIG.review_domain_name,
                "url": url,
                "cloudfront_domain_name": "d123example.cloudfront.net",
            }
        )
