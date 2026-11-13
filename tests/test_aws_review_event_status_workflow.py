"""Event確認workflowの手動認可とSecrets検証をAWSなしで検証する。"""

import re
import subprocess
from itertools import takewhile
from pathlib import Path
from textwrap import dedent

import pytest
from test_aws_review_workflow import ACCOUNT, ROLE_ARN, ROLE_PREFIX

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/aws-review-event-status.yml"
OWNER_ENVIRONMENT = {
    "AWS_REVIEW_REPOSITORY": "committee/quiz",
    "AWS_REVIEW_OPERATOR": "operator",
    "GITHUB_REPOSITORY": "committee/quiz",
    "GITHUB_REF": "refs/heads/main",
    "GITHUB_ACTOR": "operator",
    "GITHUB_TRIGGERING_ACTOR": "operator",
}
SECRETS = {
    "AWS_ACCOUNT_ID": ACCOUNT,
    "AWS_REVIEW_ID": "private-review",
    "AWS_DEPLOY_ROLE_ARN": ROLE_ARN,
}


def run_step(name: str, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    source = WORKFLOW.read_text()
    step = source.split(f"      - name: {name}\n", 1)[1]
    block = step.split("        run: |\n", 1)[1]
    script = dedent(
        "\n".join(takewhile(lambda line: line.startswith("          "), block.splitlines()))
    )
    # 固定workflowのshellだけを実行し、ローカルの資格情報を引き継がない。
    return subprocess.run(  # noqa: S603 -- Secretsを持たない固定bashで認可stepを検証する。
        ["/bin/bash", "--noprofile", "--norc", "-e", "-o", "pipefail"],
        input=script,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )


def test_workflow_requires_manual_dispatch_without_reset_inputs() -> None:
    source = WORKFLOW.read_text()
    assert source.split("on:\n", 1)[1].split("\npermissions:", 1)[0] == "  workflow_dispatch:\n"
    assert "inputs." not in source
    assert "expected_event_id" not in source
    assert "expected_event_version" not in source
    assert "RESET EVENT" not in source
    assert "    timeout-minutes: 20\n" in source


def test_authorization_completes_before_the_only_oidc_job() -> None:
    source = WORKFLOW.read_text()
    authorization, observation = source.split("  event-status:\n", 1)
    assert "secrets." not in authorization
    assert "id-token: write" not in authorization
    assert "    needs: authorize\n" in observation
    assert "    environment: aws-review\n" in observation
    assert "      group: aws-review\n      cancel-in-progress: false\n" in observation
    assert source.count("      id-token: write\n") == 1


@pytest.mark.parametrize("variable", OWNER_ENVIRONMENT)
@pytest.mark.parametrize("value", ["private-invalid-target", ""])
def test_wrong_owner_repository_or_ref_refuses_without_echoing_input(
    variable: str, value: str
) -> None:
    result = run_step("所有者によるmainからの実行だけ許可", OWNER_ENVIRONMENT | {variable: value})
    assert result.returncode == 1
    assert "private-invalid-target" not in result.stdout + result.stderr


def test_owner_main_dispatch_passes_without_output() -> None:
    result = run_step("所有者によるmainからの実行だけ許可", OWNER_ENVIRONMENT)
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""


def test_only_existing_target_and_role_secrets_are_needed() -> None:
    source = WORKFLOW.read_text()
    assert set(re.findall(r"secrets\.([A-Z_]+)", source)) == set(SECRETS)
    assert source.index("必須Environment secretsを確認") < source.index(
        "aws-actions/configure-aws-credentials@"
    )
    assert "role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}" in source
    result = run_step("必須Environment secretsを確認", SECRETS)
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("variable", SECRETS)
def test_missing_secret_refuses_before_oidc_without_disclosing_values(variable: str) -> None:
    environment = {name: value for name, value in SECRETS.items() if name != variable}
    result = run_step("必須Environment secretsを確認", environment)
    assert result.returncode == 1
    output = result.stdout + result.stderr
    assert variable in output
    assert all(value not in output for value in environment.values())


@pytest.mark.parametrize(
    "variable,value",
    [
        ("AWS_ACCOUNT_ID", "12345678901"),
        ("AWS_ACCOUNT_ID", "1234567890123"),
        ("AWS_ACCOUNT_ID", ACCOUNT + "\n"),
        ("AWS_ACCOUNT_ID", "１２３４５６７８９０１２"),
        ("AWS_DEPLOY_ROLE_ARN", ROLE_ARN.replace(ACCOUNT, "999999999999")),
        ("AWS_DEPLOY_ROLE_ARN", ROLE_ARN.replace(":role/", ":user/")),
        ("AWS_DEPLOY_ROLE_ARN", ROLE_ARN + "\n"),
        ("AWS_DEPLOY_ROLE_ARN", ROLE_PREFIX + "r" * 65),
        ("AWS_DEPLOY_ROLE_ARN", ROLE_PREFIX + "p" * 511 + "/private-role"),
        ("AWS_DEPLOY_ROLE_ARN", "$(exit 99)"),
    ],
)
def test_invalid_secret_refuses_without_echo_or_shell_evaluation(variable: str, value: str) -> None:
    environment = SECRETS | {variable: value}
    result = run_step("必須Environment secretsを確認", environment)
    assert result.returncode == 1
    output = result.stdout + result.stderr
    assert variable in output
    assert all(secret not in output for secret in environment.values())


@pytest.mark.parametrize("resource", ["review", "team/project/review", "p" * 510 + "/" + "r" * 64])
def test_existing_role_paths_and_limits_remain_accepted(resource: str) -> None:
    result = run_step(
        "必須Environment secretsを確認",
        SECRETS | {"AWS_DEPLOY_ROLE_ARN": ROLE_PREFIX + resource},
    )
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""


def test_workflow_runs_only_status_cli_with_existing_pinned_setup() -> None:
    source = WORKFLOW.read_text()
    cli_commands = re.findall(r"^        run: (uv run .+)$", source, re.MULTILINE)
    assert cli_commands == [
        "uv run --no-sync --project backend python -m validation.aws_review event-status"
    ]
    assert "run: uv sync --frozen --no-dev --project backend" in source
    assert '          python-version: "3.12"\n' in source
    assert "          persist-credentials: false\n" in source
    assert "          enable-cache: false\n" in source
    assert "          unset-current-credentials: true\n" in source
    assert re.findall(r"^\s+(?:- )?uses: (\S+)", source, re.MULTILINE) == [
        "actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09",
        "astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7",
        "aws-actions/configure-aws-credentials@e1253824e5c10ff9df46874f81ed3ec929e19cfd",
    ]
    for forbidden in (" reset-event", " destroy", " deploy", "docker", "systemctl", "psql"):
        assert forbidden not in source
