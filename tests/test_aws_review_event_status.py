"""Event照会の対象・SSM出力・初期化入力のSummaryを外部接続なしで検証する。"""

import base64
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from test_aws_review_deploy import ACCOUNT, COMMIT, Clock, owned_stack
from test_aws_review_deploy import owner_environment as owner_environment
from test_aws_review_reset_workflow import INSTANCE_ID, reset_aws
from test_load_test_aws import FakeAws

from validation.aws_review import __main__ as cli
from validation.aws_review import event_status, remote, stacks
from validation.aws_review.event_status_host import EventStatusRequest, EventStatusResult
from validation.aws_review.models import EventStatusInput

ROOT = Path(__file__).resolve().parents[1]
CONFIG = EventStatusInput(account_id=ACCOUNT, demo_id="review", source_commit=COMMIT)
OBSERVED = EventStatusResult.model_validate_json(
    json.dumps(
        {
            "event_id": "10000000-0000-4000-8000-000000000001",
            "event_version": 7,
            "event_state": "LobbyOpen",
            "observed_at": "2026-10-21T19:00:00Z",
            "source_commit": "b" * 40,
        }
    )
)


@pytest.fixture
def status_environment(owner_environment: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "configure_logging", Mock())
    for name in list(os.environ):
        if name.startswith("AWS_") and name not in {
            "AWS_ACCOUNT_ID",
            "AWS_REVIEW_ID",
            "AWS_REVIEW_REPOSITORY",
            "AWS_REVIEW_OPERATOR",
        }:
            monkeypatch.delenv(name)


def status_aws(output: str | None = None) -> FakeAws:
    api = reset_aws()
    api.responses[("ssm", "get_command_invocation")] = {
        "Status": "Success",
        "ResponseCode": 0,
        "StandardOutputContent": OBSERVED.model_dump_json() if output is None else output,
        "StandardErrorContent": "private-host-stderr",
    }
    return api


def test_status_uses_exact_host_and_read_operations(capsys: pytest.CaptureFixture[str]) -> None:
    api = status_aws()
    assert event_status.get_event_status(api, CONFIG) == OBSERVED
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
    assert sent["Parameters"]["executionTimeout"] == ["120"]
    output = capsys.readouterr()
    assert output.out == ""
    assert "private-host-stderr" not in output.err
    assert "Event ID・versionの読み取り" in output.err
    assert "DB初期化" not in output.err


@pytest.mark.parametrize("kind", ["control", "workload", "edge"])
@pytest.mark.parametrize("state", [None, "UPDATE_IN_PROGRESS", "UPDATE_ROLLBACK_COMPLETE"])
def test_missing_or_unready_stack_refuses_before_ssm(
    kind: str, state: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = status_aws()
    original = event_status.get_stack

    def observed_stack(
        api: FakeAws, config: EventStatusInput, observed: str
    ) -> stacks.Stack | None:
        if observed == kind:
            return None if state is None else owned_stack(kind, state)
        return original(api, config, observed)

    monkeypatch.setattr(event_status, "get_stack", observed_stack)
    with pytest.raises(ValueError, match="requires_ready_existing_stacks"):
        event_status.get_event_status(api, CONFIG)
    assert not any(service == "ssm" for service, _, _ in api.calls)


@pytest.mark.parametrize("change", ["account", "ownership", "mode", "instance"])
def test_wrong_target_refuses_before_ssm(change: str, monkeypatch: pytest.MonkeyPatch) -> None:
    api = status_aws()
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
        original = event_status.get_stack

        def observed_stack(
            api: FakeAws, config: EventStatusInput, kind: str
        ) -> stacks.Stack | None:
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

        monkeypatch.setattr(event_status, "get_stack", observed_stack)
    with pytest.raises((ValueError, ValidationError)):
        event_status.get_event_status(api, CONFIG)
    assert not any(service == "ssm" for service, _, _ in api.calls)


@pytest.mark.parametrize(
    "output",
    [
        "",
        "private-output",
        "x" * 4097,
        "{}",
        OBSERVED.model_dump_json() + "\nprivate-output",
        OBSERVED.model_dump_json()[:-2],
    ],
)
def test_unstructured_or_truncated_stdout_is_not_forwarded(
    output: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(ValueError, match="^review_event_status_response_invalid$"):
        event_status.get_event_status(status_aws(output), CONFIG)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "private-output" not in captured.err


@pytest.mark.parametrize(
    "change",
    [
        {"event_id": "private-uuid"},
        {"event_version": True},
        {"event_version": 0},
        {"event_version": "7"},
        {"event_state": "private-state"},
        {"observed_at": "2026-10-21T19:00:00"},
        {"source_commit": "private-commit"},
        {"password": "private-secret"},
    ],
)
def test_only_strict_allowlisted_fields_leave_the_host(
    change: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    payload = json.dumps(OBSERVED.model_dump(mode="json") | change)
    with pytest.raises(ValueError, match="review_event_status_response_invalid"):
        event_status.get_event_status(status_aws(payload), CONFIG)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "private-" not in captured.err


def test_timeout_is_bounded_and_does_not_resend(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    monkeypatch.setattr(remote.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(remote.time, "sleep", clock.sleep)
    api = status_aws()
    api.responses[("ssm", "get_command_invocation")] = {"Status": "InProgress", "ResponseCode": -1}
    with pytest.raises(TimeoutError, match="review_host_event_status_timeout"):
        event_status.get_event_status(api, CONFIG)
    assert clock.now == 300
    assert sum(operation == "send_command" for _, operation, _ in api.calls) == 1
    assert api.calls[-1][1] == "cancel_command"


def test_failure_does_not_use_stdout_as_success(capsys: pytest.CaptureFixture[str]) -> None:
    api = status_aws()
    api.responses[("ssm", "get_command_invocation")] = {
        "Status": "Failed",
        "ResponseCode": 2,
        "StandardOutputContent": OBSERVED.model_dump_json(),
        "StandardErrorContent": "private-stderr",
    }
    with pytest.raises(ValueError, match="review_host_event_status_failed"):
        event_status.get_event_status(api, CONFIG)
    output = capsys.readouterr()
    assert output.out == ""
    assert "private-stderr" not in output.err


def test_script_embeds_exact_source_without_host_updates() -> None:
    request = EventStatusRequest(account_id=ACCOUNT, demo_id="review", instance_id=INSTANCE_ID)
    script = event_status.event_status_commands(request)
    checked = subprocess.run(  # noqa: S603 -- 固定SSM scriptの構文だけを検証する。
        ["/bin/bash", "-n"], input=script, text=True, capture_output=True, timeout=5, check=False
    )
    assert checked.returncode == 0
    assert "exec 9>/run/lock/gs-review-deploy.lock\nflock --nonblock 9" in script
    tokens = shlex.split(script.split('sudo -H -u ubuntu "$python" -B -c ', 1)[1])
    program, flag, argument = tokens
    assert flag == "--request-json"
    assert EventStatusRequest.model_validate_json(argument) == request
    encoded = re.search(r"base64.b64decode\('([A-Za-z0-9+/=]+)'", program)
    assert encoded is not None
    assert (
        base64.b64decode(encoded.group(1), validate=True)
        == (ROOT / "validation/aws_review/event_status_host.py").read_bytes()
    )
    assert len(script.encode()) < 64_000
    for forbidden in (
        "systemctl",
        "git reset",
        "git fetch",
        "docker stop",
        "docker compose",
        "write_private_file",
        "uv sync",
    ):
        assert forbidden not in script


def test_configuration_does_not_require_event_values_or_deployment_inputs(
    status_environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.subprocess, "check_output", Mock(side_effect=[COMMIT, ""]))
    assert cli.configuration("event-status") == CONFIG


@pytest.mark.parametrize("dirty", [" M private-path\n", "?? private-path\n"])
def test_uncommitted_query_code_is_rejected_before_aws(
    status_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    dirty: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli.subprocess, "check_output", Mock(side_effect=[COMMIT, dirty]))
    aws = Mock()
    monkeypatch.setattr(cli, "BotoAwsApi", aws)
    assert cli.main(["event-status"]) == 1
    aws.assert_not_called()
    output = capsys.readouterr()
    assert output.out == "" and "private-path" not in output.err


def test_cli_summary_has_copy_fields_and_source_mismatch_guidance(
    status_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(cli.subprocess, "check_output", Mock(side_effect=[COMMIT, ""]))
    monkeypatch.setattr(cli, "BotoAwsApi", status_aws)
    assert cli.main(["event-status"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {"status": "event_status", **OBSERVED.model_dump(mode="json")}
    content = summary.read_text()
    assert f"| `expected_event_id` | `{OBSERVED.event_id}` |" in content
    assert "| `expected_event_version` | `7` |" in content
    assert "配置commitがこのworkflowのmainと異なります" in content
    assert "update" in content
    assert "DBを全初期化" in content
    assert "private-host-stderr" not in content + output.out + output.err
    assert "配置commitがこのworkflowのmainと異なります" not in event_status.status_summary(
        OBSERVED, OBSERVED.source_commit
    )


def test_invalid_response_does_not_create_a_success_summary(
    status_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(cli.subprocess, "check_output", Mock(side_effect=[COMMIT, ""]))
    monkeypatch.setattr(cli, "BotoAwsApi", lambda: status_aws("private-invalid-json"))
    assert cli.main(["event-status"]) == 1
    output = capsys.readouterr()
    assert output.out == "" and "private-invalid-json" not in output.err
    assert not summary.exists()
