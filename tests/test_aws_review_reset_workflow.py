"""初期化の対象制限、明示入力、SSM排他と失敗時の停止をAWSなしで検証する。"""

import base64
import json
import re
import shlex
import subprocess
from itertools import takewhile
from pathlib import Path
from textwrap import dedent
from unittest.mock import Mock
from uuid import UUID

import pytest
from pydantic import ValidationError
from test_aws_review_deploy import ACCOUNT, COMMIT, Clock, owned_stack
from test_aws_review_deploy import owner_environment as owner_environment
from test_load_test_aws import FakeAws

from quiz_app.load_test_aws.aws import AwsCallError
from validation.aws_review import __main__ as cli
from validation.aws_review import remote, reset, reset_remote, stacks
from validation.aws_review.models import ResetInput

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/aws-review-reset-event.yml"
INSTANCE_ID = "i-0123456789abcdef0"
CONFIG = ResetInput(
    account_id=ACCOUNT,
    demo_id="review",
    source_commit=COMMIT,
    expected_event_id=UUID("10000000-0000-4000-8000-000000000001"),
    expected_event_version=7,
    confirmation="RESET EVENT",
)


def reset_aws() -> FakeAws:
    api = FakeAws()
    api.responses[("sts", "get_caller_identity")] = {
        "Account": ACCOUNT,
        "Arn": f"arn:aws:iam::{ACCOUNT}:role/review",
    }
    existing = [owned_stack(kind) for kind in ("control", "workload", "edge")]
    existing[1] = existing[1].model_copy(
        update={"outputs": (stacks.Output(OutputKey="AppInstanceId", OutputValue=INSTANCE_ID),)}
    )
    api.page_responses[("cloudformation", "describe_stacks")] = [
        {"Stacks": [item.model_dump(by_alias=True) for item in existing]}
    ]
    api.responses[("ssm", "describe_instance_information")] = {
        "InstanceInformationList": [{"InstanceId": INSTANCE_ID, "PingStatus": "Online"}]
    }
    api.responses[("ssm", "send_command")] = {
        "Command": {"CommandId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}
    }
    api.responses[("ssm", "get_command_invocation")] = {"Status": "Success", "ResponseCode": 0}
    return api


def test_reset_uses_only_read_operations_and_one_exact_host_command() -> None:
    api = reset_aws()
    reset.reset_event(api, CONFIG)
    assert [(service, operation) for service, operation, _ in api.calls] == [
        ("sts", "get_caller_identity"),
        *(("cloudformation", "describe_stacks"),) * 3,
        ("ssm", "describe_instance_information"),
        ("ssm", "send_command"),
        ("ssm", "get_command_invocation"),
    ]
    sent = next(arguments for _, operation, arguments in api.calls if operation == "send_command")
    assert sent["InstanceIds"] == [INSTANCE_ID]
    assert sent["DocumentName"] == "AWS-RunShellScript"
    assert sent["Parameters"] == {
        "commands": ["bash -c " + shlex.quote(reset_remote.reset_host_commands(CONFIG))],
        "executionTimeout": ["600"],
    }


@pytest.mark.parametrize("kind", ["control", "workload", "edge"])
@pytest.mark.parametrize("state", [None, "UPDATE_IN_PROGRESS", "UPDATE_ROLLBACK_COMPLETE"])
def test_missing_or_unready_stack_refuses_before_ssm(
    kind: str, state: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = reset_aws()
    original = reset.get_stack

    def observed_stack(api: FakeAws, config: ResetInput, observed: str) -> stacks.Stack | None:
        if observed == kind:
            return None if state is None else owned_stack(kind, state)
        return original(api, config, observed)

    monkeypatch.setattr(reset, "get_stack", observed_stack)
    with pytest.raises(ValueError, match="requires_ready_existing_stacks"):
        reset.reset_event(api, CONFIG)
    assert not any(service == "ssm" for service, _, _ in api.calls)


@pytest.mark.parametrize("change", ["account", "ownership", "mode", "instance"])
def test_wrong_target_refuses_before_ssm(change: str, monkeypatch: pytest.MonkeyPatch) -> None:
    api = reset_aws()
    if change == "account":
        api.responses[("sts", "get_caller_identity")] = {
            "Account": "999999999999",
            "Arn": "arn:aws:iam::999999999999:role/other",
        }
    elif change == "ownership":
        api.page_responses[("cloudformation", "describe_stacks")] = [
            {
                "Stacks": [
                    owned_stack("control").model_copy(update={"tags": ()}).model_dump(by_alias=True)
                ]
            }
        ]
    else:
        original = reset.get_stack

        def observed_stack(api: FakeAws, config: ResetInput, kind: str) -> stacks.Stack | None:
            existing = original(api, config, kind)
            assert existing is not None
            if kind != "workload":
                return existing
            if change == "mode":
                return existing.model_copy(update={"parameters": ()})
            return existing.model_copy(
                update={
                    "outputs": (stacks.Output(OutputKey="AppInstanceId", OutputValue="invalid"),)
                }
            )

        monkeypatch.setattr(reset, "get_stack", observed_stack)
    with pytest.raises((ValueError, ValidationError)):
        reset.reset_event(api, CONFIG)
    assert not any(service == "ssm" for service, _, _ in api.calls)


def test_reset_remote_timeout_cancels_without_resending(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    monkeypatch.setattr(remote.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(remote.time, "sleep", clock.sleep)
    api = reset_aws()
    api.responses[("ssm", "get_command_invocation")] = {"Status": "InProgress", "ResponseCode": -1}
    with pytest.raises(TimeoutError, match="review_host_event_reset_timeout"):
        reset.reset_event(api, CONFIG)
    assert clock.now == 780
    assert sum(operation == "send_command" for _, operation, _ in api.calls) == 1
    assert api.calls[-1] == (
        "ssm",
        "cancel_command",
        {"CommandId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "InstanceIds": [INSTANCE_ID]},
    )


@pytest.mark.parametrize("status", ["Failed", "Cancelled", "TimedOut", "Success"])
def test_failed_host_never_reports_success_or_forwards_raw_stderr(
    status: str, capsys: pytest.CaptureFixture[str]
) -> None:
    api = reset_aws()
    api.responses[("ssm", "get_command_invocation")] = {
        "Status": status,
        "ResponseCode": 2,
        "StandardErrorContent": "private-secret-input",
    }
    with pytest.raises(ValueError, match="review_host_event_reset_failed"):
        reset.reset_event(api, CONFIG)
    output = capsys.readouterr()
    assert "private-secret-input" not in output.out + output.err


def test_read_permission_failure_is_not_treated_as_missing_environment() -> None:
    api = reset_aws()
    api.responses[("sts", "get_caller_identity")] = AwsCallError(
        "sts", "get_caller_identity", "AccessDeniedException"
    )
    with pytest.raises(AwsCallError):
        reset.reset_event(api, CONFIG)
    assert len(api.calls) == 1


def test_host_script_preserves_source_and_lock_and_uses_private_writer() -> None:
    script = reset_remote.reset_host_commands(CONFIG)
    checked = subprocess.run(  # noqa: S603 -- 生成済み固定scriptの構文検証だけを行う。
        ["/bin/bash", "-n"], input=script, text=True, capture_output=True, check=False, timeout=5
    )
    assert checked.returncode == 0
    assert "exec 9>/run/lock/gs-review-deploy.lock\nflock --nonblock 9" in script
    assert "rev-parse HEAD" in script and "status --porcelain" in script
    assert script.index("review_reset_source_mismatch") < script.index("write_private_file")
    assert 'sudo -H -u ubuntu "$python" -B -m validation.aws_review.host_reset' in script
    assert "--reset-input /etc/gs-app-tomidaisai/review-reset-input.json" in script
    for forbidden in (
        "git reset",
        "reset --hard",
        " fetch ",
        "s3 cp",
        "docker pull",
        "systemctl",
        "uv sync",
        "chmod",
        "chown",
    ):
        assert forbidden not in script
    write_line = next(line for line in script.splitlines() if '"$python" -B -c ' in line)
    write_code = shlex.split(write_line)[-1]
    encoded = re.search(r"base64\.b64decode\('([A-Za-z0-9+/=]+)'", write_code)
    assert encoded is not None
    assert ResetInput.model_validate_json(base64.b64decode(encoded.group(1))) == CONFIG


@pytest.fixture
def reset_environment(owner_environment: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REVIEW_EXPECTED_EVENT_ID", str(CONFIG.expected_event_id))
    monkeypatch.setenv("AWS_REVIEW_EXPECTED_EVENT_VERSION", str(CONFIG.expected_event_version))
    monkeypatch.setenv("AWS_REVIEW_RESET_CONFIRMATION", CONFIG.confirmation)


def test_configuration_requires_only_target_and_reset_inputs(
    reset_environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    for variable in (
        "AWS_APP_AZ",
        "AWS_SECONDARY_AZ",
        "AWS_UBUNTU_AMI_ID",
        "AWS_DATABASE_ENGINE_VERSION",
        "AWS_REVIEW_DOMAIN_NAME",
        "AWS_REVIEW_CERTIFICATE_ARN",
    ):
        monkeypatch.delenv(variable)
    monkeypatch.setattr(cli.subprocess, "check_output", Mock(side_effect=[COMMIT, ""]))
    assert cli.configuration("reset-event") == CONFIG


@pytest.mark.parametrize(
    "variable,value",
    [
        ("AWS_REVIEW_EXPECTED_EVENT_ID", "private-invalid-uuid"),
        ("AWS_REVIEW_EXPECTED_EVENT_VERSION", "private-invalid-version"),
        ("AWS_REVIEW_EXPECTED_EVENT_VERSION", "1.0"),
        ("AWS_REVIEW_EXPECTED_EVENT_VERSION", "0"),
        ("AWS_REVIEW_RESET_CONFIRMATION", "private-invalid-confirmation"),
    ],
)
def test_invalid_reset_input_fails_before_git_or_aws_without_echoing_input(
    reset_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    variable: str,
    value: str,
) -> None:
    monkeypatch.setenv(variable, value)
    aws, git = Mock(), Mock()
    monkeypatch.setattr(cli, "configure_logging", Mock())
    monkeypatch.setattr(cli, "BotoAwsApi", aws)
    monkeypatch.setattr(cli.subprocess, "check_output", git)
    assert cli.main(["reset-event"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["configuration_errors"][0]["variables"] == [variable]
    assert "private-invalid-" not in output.err
    aws.assert_not_called()
    git.assert_not_called()


@pytest.mark.parametrize("version", [True, 1.0, -1, 0, 2_147_483_647, " 1", "1\n"])
def test_expected_version_is_a_bounded_integer(version: object) -> None:
    with pytest.raises(ValidationError):
        ResetInput.model_validate(CONFIG.model_dump() | {"expected_event_version": version})


def workflow_step(name: str) -> str:
    step = WORKFLOW.read_text().split(f"      - name: {name}\n", 1)[1]
    block = step.split("        run: |\n", 1)[1]
    return dedent(
        "\n".join(takewhile(lambda line: line.startswith("          "), block.splitlines()))
    )


@pytest.mark.parametrize("actor", ["GITHUB_ACTOR", "GITHUB_TRIGGERING_ACTOR"])
def test_workflow_blocks_non_owner_before_credential_job(actor: str) -> None:
    environment = {
        "AWS_REVIEW_REPOSITORY": "committee/quiz",
        "AWS_REVIEW_OPERATOR": "operator",
        "GITHUB_REPOSITORY": "committee/quiz",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_ACTOR": "operator",
        "GITHUB_TRIGGERING_ACTOR": "operator",
        actor: "private-other-actor",
    }
    result = subprocess.run(  # noqa: S603 -- workflowの固定authorize stepを資格情報なしで検証。
        ["/bin/bash", "-e"],
        input=workflow_step("所有者によるmainからの実行だけ許可"),
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 1
    assert "private-other-actor" not in result.stdout + result.stderr
    source = WORKFLOW.read_text()
    assert "    needs: authorize\n" in source
    assert "    environment: aws-review\n" in source
    assert "      group: aws-review\n      cancel-in-progress: false\n" in source
    assert "  push:" not in source and "  schedule:" not in source


@pytest.mark.parametrize(
    "confirmation", ["RESET EVENT", "", "reset event", "RESET EVENT\n", "$(exit 99)"]
)
def test_workflow_confirmation_is_exact_and_not_evaluated(confirmation: str) -> None:
    result = subprocess.run(  # noqa: S603 -- workflowの固定preflightを資格情報なしで検証。
        ["/bin/bash", "-e"],
        input=workflow_step("必須Environment secretsを確認"),
        env={
            "AWS_ACCOUNT_ID": ACCOUNT,
            "AWS_REVIEW_ID": "review",
            "AWS_DEPLOY_ROLE_ARN": f"arn:aws:iam::{ACCOUNT}:role/review",
            "AWS_REVIEW_RESET_CONFIRMATION": confirmation,
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == (0 if confirmation == "RESET EVENT" else 1)
