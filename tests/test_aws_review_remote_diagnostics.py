"""SSMからの失敗診断伝播と、生出力・未知値を公開しない境界を検証する。"""

import json

import pytest
from test_aws_review_deploy import host_input as host_input
from test_aws_review_deploy import online_host
from test_load_test_aws import FakeAws

from validation.aws_review import diagnostics, remote, remote_diagnostics
from validation.aws_review.host_contracts import HostInput


@pytest.fixture(autouse=True)
def configure_logging() -> None:
    diagnostics.configure_logging()


def failure_payload() -> dict[str, object]:
    return {
        "status": "failed",
        "code": "review_host_failed",
        "error_type": "CalledProcessError",
        "locations": [{"module": "validation/aws_review/host.py", "line": 241}],
    }


def test_only_last_valid_host_failure_is_reconstructed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = failure_payload() | {
        "aws_operation": "GetSecretValue",
        "aws_code": "AccessDeniedException",
        "message": "private-password",
        "stdout": "private-answer",
        "configuration_errors": [{"value": "private-token"}],
    }
    raw_output = "\n".join(
        [
            "postgresql://private-user:private-password@private-host/quiz",
            json.dumps(failure_payload() | {"error_type": "OSError"}),
            json.dumps(payload),
            '{"status":"failed","private-token":',
            "failed to run commands: exit status 2",
        ]
    )
    remote_diagnostics.report_host_failure(raw_output)
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == failure_payload() | {
        "aws_operation": "GetSecretValue",
        "aws_code": "AccessDeniedException",
    }
    assert "private-" not in output.err


@pytest.mark.parametrize(
    "module",
    [
        "validation/aws_review/private-password.py",
        "validation/aws_review/../private-password.py",
        "/opt/private-password.py",
        "tests/private-password.py",
    ],
)
def test_unknown_diagnostic_values_and_location_fields_are_discarded(
    module: str, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = failure_payload() | {
        "error_type": "private-password",
        "aws_operation": "private-operation",
        "aws_code": "private-code",
        "locations": [
            {"module": module, "line": 1},
            {
                "module": "validation/aws_review/host.py",
                "line": 241,
                "locals": {"password": "private-password"},
                "source": "private-answer",
            },
        ],
    }
    remote_diagnostics.report_host_failure(json.dumps(payload))
    output = capsys.readouterr().err
    assert json.loads(output) == failure_payload() | {"error_type": "Error"}
    assert "private-" not in output


@pytest.mark.parametrize(
    "replacement",
    [
        {"status": "running"},
        {"code": "review_private-answer"},
        {"error_type": {"secret": "private-password"}},
        {"locations": [{"module": "validation/aws_review/host.py", "line": True}]},
        {"locations": [{"module": "validation/aws_review/host.py", "line": "241"}]},
        {"locations": [{"module": "validation/aws_review/host.py", "line": -1}]},
        {"locations": [{"module": "validation/aws_review/host.py", "line": 100_001}]},
    ],
)
def test_invalid_host_failure_is_ignored_without_exposing_validation_error(
    replacement: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    remote_diagnostics.report_host_failure(json.dumps(failure_payload() | replacement))
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("standard_error", ["", "private-password", '{"status":"failed"'])
def test_missing_or_truncated_diagnostics_preserve_remote_failure(
    host_input: HostInput, standard_error: str, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeAws()
    online_host(api, host_input)
    api.responses[("ssm", "get_command_invocation")] = {
        "Status": "Failed",
        "ResponseCode": 2,
        "StandardErrorContent": standard_error,
        "StandardOutputContent": json.dumps(failure_payload()),
    }
    with pytest.raises(ValueError, match="review_host_deployment_failed"):
        remote.deploy_host(api, host_input, "script")
    output = capsys.readouterr().err
    assert "review_host_failed" not in output
    assert "private-password" not in output
    assert [operation for _, operation, _ in api.calls] == [
        "describe_instance_information",
        "send_command",
        "get_command_invocation",
    ]


@pytest.mark.parametrize("status,response_code", [("Failed", 2), ("Success", 0)])
def test_remote_forwards_valid_diagnostic_only_on_terminal_failure(
    host_input: HostInput,
    status: str,
    response_code: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    api = FakeAws()
    online_host(api, host_input)
    api.responses[("ssm", "get_command_invocation")] = {
        "Status": status,
        "ResponseCode": response_code,
        "StandardErrorContent": "private-password\n" + json.dumps(failure_payload()),
        "StandardOutputContent": "private-answer",
    }
    if status == "Failed":
        with pytest.raises(ValueError, match="review_host_deployment_failed"):
            remote.deploy_host(api, host_input, "script")
    else:
        remote.deploy_host(api, host_input, "script")
    output = capsys.readouterr().err
    records = [json.loads(line) for line in output.splitlines()]
    assert [record for record in records if record["status"] == "failed"] == (
        [failure_payload()] if status == "Failed" else []
    )
    assert "private-" not in output


def test_invocation_representation_and_serialization_exclude_raw_standard_error() -> None:
    invocation = remote.Invocation.model_validate(
        {"Status": "Failed", "ResponseCode": 2, "StandardErrorContent": "private-password"}
    )
    assert "private-password" not in repr(invocation)
    assert "private-password" not in invocation.model_dump_json()
