"""独自domainの入口と外部証明書の権限・撤去境界をAWS非接続で確認する。"""

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
EDGE = ROOT / "infra/aws-rehearsal/public-edge.yaml"
OIDC = ROOT / "infra/aws-review/github-oidc.yaml"
DOMAIN = "quiz.example.org"
CERTIFICATE = "arn:aws:acm:us-east-1:123456789012:certificate/12345678-1234-1234-1234-123456789abc"


class CloudFormationLoader(yaml.SafeLoader):
    """CFN関数を実行せず、構造のままSafeLoaderへ渡す。"""


def _intrinsic(loader: yaml.SafeLoader, tag: str, node: yaml.Node) -> dict[str, object]:
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        raise AssertionError(f"Unexpected intrinsic node: {type(node).__name__}")
    return {tag if tag == "Ref" else f"Fn::{tag}": value}


CloudFormationLoader.add_multi_constructor("!", _intrinsic)


def _template(path: Path) -> dict[str, object]:
    # SafeLoaderの継承であり、追加tagはデータ化するだけで任意objectを生成しない。
    document = yaml.load(
        path.read_text(encoding="utf-8"),
        Loader=CloudFormationLoader,  # noqa: S506 -- SafeLoader継承でCFN関数をデータ化するだけ。
    )
    assert isinstance(document, dict)
    return document


def _rule_value(value: object, parameters: dict[str, str]) -> object:
    """利用しているrule演算だけで、片側欠測の4通りを評価する。"""
    if not isinstance(value, dict):
        return value
    if "Ref" in value:
        return parameters[value["Ref"]]
    if "Fn::Equals" in value:
        left, right = value["Fn::Equals"]
        return _rule_value(left, parameters) == _rule_value(right, parameters)
    if "Fn::Not" in value:
        return not _rule_value(value["Fn::Not"][0], parameters)
    raise AssertionError(f"Unexpected rule operator: {tuple(value)}")


@pytest.mark.parametrize(
    "domain,certificate,accepted",
    [(DOMAIN, CERTIFICATE, True), ("", "", True), (DOMAIN, "", False), ("", CERTIFICATE, False)],
)
def test_domain_and_certificate_must_be_provided_together(
    domain: str, certificate: str, accepted: bool
) -> None:
    parameters = {
        "ReviewDomainName": domain,
        "ReviewCertificateArn": certificate,
        "MonitoringEnabled": "false",
    }
    rules = _template(EDGE)["Rules"]
    actual = all(
        _rule_value(assertion["Assert"], parameters)
        for rule in rules.values()
        if _rule_value(rule["RuleCondition"], parameters)
        for assertion in rule["Assertions"]
    )
    assert actual is accepted


def test_review_domain_accepts_only_hostnames_or_an_empty_default() -> None:
    parameters = _template(EDGE)["Parameters"]
    pattern = parameters["ReviewDomainName"]["AllowedPattern"]
    assert all(re.fullmatch(pattern, value) for value in ("", DOMAIN, "quiz.festival.example"))
    assert not any(re.fullmatch(pattern, value) for value in ("*.example.org", "bad", DOMAIN + "/"))
    assert parameters["ReviewDomainName"]["Default"] == ""
    assert parameters["ReviewCertificateArn"]["Default"] == ""
    assert _template(OIDC)["Parameters"]["ReviewCertificateArn"]["Default"] == ""


@pytest.mark.parametrize("path", [EDGE, OIDC])
@pytest.mark.parametrize(
    "certificate,accepted",
    [
        ("", True),
        (CERTIFICATE, True),
        (CERTIFICATE.replace("us-east-1", "ap-northeast-1"), False),
        (CERTIFICATE.replace("arn:aws:", "arn:aws-cn:"), False),
        (CERTIFICATE.replace("123456789012", "12345678901"), False),
        (CERTIFICATE.replace("certificate/", "certificate/*"), False),
        (f"{CERTIFICATE}\n", False),
        (f" {CERTIFICATE}", False),
    ],
)
def test_certificate_parameter_rejects_other_regions_and_malformed_arns(
    path: Path, certificate: str, accepted: bool
) -> None:
    pattern = _template(path)["Parameters"]["ReviewCertificateArn"]["AllowedPattern"]
    assert (re.fullmatch(pattern, certificate) is not None) is accepted


def test_alias_tls_and_public_origin_switch_together_without_changing_origin() -> None:
    template = _template(EDGE)
    configuration = template["Resources"]["Distribution"]["Properties"]["DistributionConfig"]
    assert template["Conditions"]["HasReviewDomain"] == {
        "Fn::Not": [{"Fn::Equals": [{"Ref": "ReviewDomainName"}, ""]}]
    }
    assert configuration["Aliases"] == {
        "Fn::If": ["HasReviewDomain", [{"Ref": "ReviewDomainName"}], {"Ref": "AWS::NoValue"}]
    }
    assert configuration["ViewerCertificate"] == {
        "Fn::If": [
            "HasReviewDomain",
            {
                "AcmCertificateArn": {"Ref": "ReviewCertificateArn"},
                "SslSupportMethod": "sni-only",
                "MinimumProtocolVersion": "TLSv1.2_2021",
            },
            {"CloudFrontDefaultCertificate": True},
        ]
    }
    assert template["Outputs"]["PublicUrl"]["Value"] == {
        "Fn::If": [
            "HasReviewDomain",
            {"Fn::Sub": "https://${ReviewDomainName}"},
            {"Fn::Sub": "https://${Distribution.DomainName}"},
        ]
    }
    assert template["Outputs"]["CloudFrontDomainName"]["Value"] == {
        "Fn::GetAtt": "Distribution.DomainName"
    }
    assert configuration["Origins"][0]["VpcOriginConfig"]["VpcOriginId"] == {
        "Fn::GetAtt": "VpcOrigin.Id"
    }


def test_oidc_subject_is_one_literal_repository_and_environment() -> None:
    template = _template(OIDC)
    pattern = template["Parameters"]["GitHubSubject"]["AllowedPattern"]
    for repository in ("committee/quiz", "committee@123/quiz@456"):
        assert re.fullmatch(pattern, f"repo:{repository}:environment:aws-review")
    for subject in (
        "repo:*/quiz:environment:aws-review",
        "repo:committee/*:environment:aws-review",
        "repo:committee/quiz:environment:other",
    ):
        assert not re.fullmatch(pattern, subject)
    for name in ("DeployRole", "DestroyRole"):
        role = template["Resources"][name]["Properties"]
        conditions = role["AssumeRolePolicyDocument"]["Statement"][0]["Condition"]
        assert conditions["StringEquals"]["token.actions.githubusercontent.com:sub"] == {
            "Ref": "GitHubSubject"
        }


def test_only_deploy_role_can_describe_the_explicit_external_certificate() -> None:
    template = _template(OIDC)
    resources = template["Resources"]
    statements = resources["DeployRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    certificate_statements = [item for item in statements if "acm:" in json.dumps(item)]
    assert certificate_statements == [
        {
            "Fn::If": [
                "HasReviewCertificate",
                {
                    "Effect": "Allow",
                    "Action": "acm:DescribeCertificate",
                    "Resource": {"Ref": "ReviewCertificateArn"},
                },
                {"Ref": "AWS::NoValue"},
            ]
        }
    ]
    assert template["Conditions"]["HasReviewCertificate"] == {
        "Fn::Not": [{"Fn::Equals": [{"Ref": "ReviewCertificateArn"}, ""]}]
    }
    assert "acm:" not in json.dumps(resources["DestroyRole"])
    assert "acm:" not in json.dumps(resources["ReadAndCleanupPolicy"])
    for path in (EDGE, OIDC):
        assert all(
            resource["Type"] != "AWS::CertificateManager::Certificate"
            for resource in _template(path)["Resources"].values()
        )
