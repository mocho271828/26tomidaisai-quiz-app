import subprocess
from itertools import takewhile
from pathlib import Path
from textwrap import dedent

import pytest
from test_aws_review_deploy import CERTIFICATE_ARN, CONFIG

ACCOUNT = "123456789012"
ROLE_PREFIX = f"arn:aws:iam::{ACCOUNT}:role/"
ROLE_ARN = ROLE_PREFIX + "preflight-private-role"
WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


@pytest.fixture(params=("deploy", "destroy"))
def workflow(request: pytest.FixtureRequest) -> tuple[str, str]:
    operation = str(request.param)
    source = (WORKFLOWS / f"aws-review-{operation}.yml").read_text()
    step = source.split("      - name: 必須Environment secretsを確認\n", 1)[1]
    block = step.split("        run: |\n", 1)[1]
    lines = takewhile(lambda line: line.startswith("          "), block.splitlines())
    script = dedent("\n".join(lines))
    assert script
    return script, f"AWS_{operation.upper()}_ROLE_ARN"


def run_preflight(
    workflow: tuple[str, str],
    account_id: str,
    role_arn: str,
    overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    script, role_variable = workflow
    environment = {
        "AWS_ACCOUNT_ID": account_id,
        "AWS_REVIEW_ID": "preflight-private-review",
        "AWS_APP_AZ": "ap-northeast-1a",
        "AWS_SECONDARY_AZ": "ap-northeast-1c",
        "AWS_UBUNTU_AMI_ID": "ami-0123456789abcdef0",
        "AWS_DATABASE_ENGINE_VERSION": "16.6",
        "AWS_REVIEW_PUBLIC_ENTRYPOINT": "custom-domain",
        "AWS_REVIEW_DOMAIN_NAME": CONFIG.review_domain_name,
        "AWS_REVIEW_CERTIFICATE_ARN": CERTIFICATE_ARN,
        role_variable: role_arn,
        **(overrides or {}),
    }
    # 実際のworkflowだけを固定入力で実行し、ローカル資格情報を引き継がない。
    result = subprocess.run(  # noqa: S603 -- リポジトリ内の検証stepを固定bashで実行。
        ["/bin/bash", "--noprofile", "--norc", "-e", "-o", "pipefail"],
        input=script,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    output = result.stdout + result.stderr
    for value in environment.values():
        if value.strip():
            assert value not in output
    return result


@pytest.mark.parametrize(
    "variable,value",
    [
        ("AWS_REVIEW_DOMAIN_NAME", ""),
        ("AWS_REVIEW_DOMAIN_NAME", "private..example.org"),
        ("AWS_REVIEW_CERTIFICATE_ARN", ""),
        ("AWS_REVIEW_CERTIFICATE_ARN", CERTIFICATE_ARN.replace("us-east-1", "ap-northeast-1")),
        ("AWS_REVIEW_CERTIFICATE_ARN", CERTIFICATE_ARN.replace(ACCOUNT, "999999999999")),
        ("AWS_REVIEW_CERTIFICATE_ARN", CERTIFICATE_ARN + "\n"),
    ],
)
def test_domain_settings_only_gate_deploy_before_oidc(
    workflow: tuple[str, str], variable: str, value: str
) -> None:
    result = run_preflight(workflow, ACCOUNT, ROLE_ARN, {variable: value})
    if workflow[1] == "AWS_DEPLOY_ROLE_ARN":
        assert result.returncode == 1
        assert variable in result.stdout
    else:
        assert result.returncode == 0


@pytest.mark.parametrize("settings", ["", "private-unused-setting"])
def test_cloudfront_entrypoint_skips_domain_and_certificate_gate(
    workflow: tuple[str, str], settings: str
) -> None:
    result = run_preflight(
        workflow,
        ACCOUNT,
        ROLE_ARN,
        {
            "AWS_REVIEW_PUBLIC_ENTRYPOINT": "cloudfront",
            "AWS_REVIEW_DOMAIN_NAME": settings,
            "AWS_REVIEW_CERTIFICATE_ARN": settings,
        },
    )
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("entrypoint", ["", "private-entrypoint", "cloudfront\n"])
def test_invalid_public_entrypoint_is_rejected_before_oidc(
    workflow: tuple[str, str], entrypoint: str
) -> None:
    result = run_preflight(
        workflow, ACCOUNT, ROLE_ARN, {"AWS_REVIEW_PUBLIC_ENTRYPOINT": entrypoint}
    )
    if workflow[1] == "AWS_DEPLOY_ROLE_ARN":
        assert result.returncode == 1
        assert "AWS_REVIEW_PUBLIC_ENTRYPOINT" in result.stdout
    else:
        assert result.returncode == 0


def test_dispatch_entrypoint_has_explicit_choices_and_existing_default() -> None:
    source = (WORKFLOWS / "aws-review-deploy.yml").read_text()
    dispatch = source.split("  workflow_dispatch:\n", 1)[1].split("\npermissions:", 1)[0]
    assert "      public_entrypoint:\n" in dispatch
    assert "        type: choice\n" in dispatch
    assert "        default: custom-domain\n" in dispatch
    assert "          - custom-domain\n          - cloudfront\n" in dispatch
    assert (
        "AWS_REVIEW_PUBLIC_ENTRYPOINT: ${{ inputs.public_entrypoint || 'custom-domain' }}" in source
    )


def test_deploy_does_not_request_or_forward_monitoring_expiry() -> None:
    source = (WORKFLOWS / "aws-review-deploy.yml").read_text()
    assert "monitoring_expires_at" not in source
    assert "AWS_MONITORING_EXPIRES_AT" not in source


@pytest.mark.parametrize(
    "resource",
    [
        "preflight-private-role",
        "team/project/private_role+=,.@-",
        "!#$%&'()*+,-./:;<=>?@[\\]^_`{|}~/private-role",
        "r" * 64,
        "p" * 510 + "/" + "r" * 64,
    ],
)
def test_valid_role_arn_passes_without_output(workflow: tuple[str, str], resource: str) -> None:
    result = run_preflight(workflow, ACCOUNT, ROLE_PREFIX + resource)
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize(
    "account_id",
    ["", "12345678901", "1234567890123", "12345678901x", f" {ACCOUNT}", f"{ACCOUNT}\n"],
)
def test_invalid_account_fails_without_secret_values(
    workflow: tuple[str, str], account_id: str
) -> None:
    result = run_preflight(workflow, account_id, ROLE_ARN)
    assert result.returncode == 1
    assert "::error::" in result.stdout and "AWS_ACCOUNT_ID" in result.stdout


@pytest.mark.parametrize(
    "role_arn",
    [
        "",
        "preflight-private-role",
        ROLE_PREFIX,
        ROLE_PREFIX + "r" * 65,
        ROLE_PREFIX + "p" * 511 + "/private-role",
        ROLE_ARN.replace(ACCOUNT, "999999999999"),
        ROLE_ARN.replace(":role/", ":user/"),
        f"arn:aws:sts::{ACCOUNT}:assumed-role/private-role/session",
        f"arn:aws:iam::{ACCOUNT}:oidc-provider/token.actions.githubusercontent.com",
        ROLE_ARN.replace(":role/", ":instance-profile/"),
        ROLE_ARN.replace("iam::", "iam:ap-northeast-1:"),
        ROLE_ARN.replace("arn:aws:", "arn:aws-cn:"),
        f'"{ROLE_ARN}"',
        f"'{ROLE_ARN}'",
        f" {ROLE_ARN}",
        f"{ROLE_ARN} ",
        f"{ROLE_ARN}\n",
        f"{ROLE_ARN}\r\n",
        ROLE_PREFIX + "private role",
        ROLE_PREFIX + "private\trole",
        ROLE_PREFIX + "private\nrole",
        ROLE_PREFIX + "private/path/",
        ROLE_PREFIX + "private\u00a0role",
    ],
)
def test_invalid_role_fails_without_secret_values(workflow: tuple[str, str], role_arn: str) -> None:
    result = run_preflight(workflow, ACCOUNT, role_arn)
    assert result.returncode == 1
    assert "::error::" in result.stdout and workflow[1] in result.stdout
