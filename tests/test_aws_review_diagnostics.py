import json
from typing import cast
from unittest.mock import Mock

import logfire
import pytest
from botocore.exceptions import ClientError

from quiz_app.load_test_aws.aws import AwsCallError
from validation.aws_review import __main__ as review_cli
from validation.aws_review import diagnostics


def test_logging_disables_console_export_and_source_argument_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure = Mock()
    monkeypatch.setattr(diagnostics.logfire, "configure", configure)
    diagnostics.configure_logging()
    configure.assert_called_once_with(send_to_logfire=False, inspect_arguments=False, console=False)


def test_span_exception_body_cause_and_local_values_never_reach_console(
    capsys: pytest.CaptureFixture[str],
) -> None:
    diagnostics.configure_logging()
    try:
        with logfire.span("test operation"):
            try:
                raise OSError("postgresql://private-user:private-password@private-host")
            except OSError as error:
                raise ValueError("private-answer") from error
    except ValueError as error:
        diagnostics.log_failure("review_host_failed", error)
    output = capsys.readouterr()
    assert output.out == ""
    result = json.loads(output.err)
    assert result["status"] == "failed" and result["code"] == "review_host_failed"
    assert result["locations"]
    assert "private-" not in output.err
    assert str(diagnostics.ROOT) not in output.err
    assert all(set(location) == {"module", "line"} for location in result["locations"])


def test_validation_failure_keeps_validation_module_location(
    capsys: pytest.CaptureFixture[str],
) -> None:
    diagnostics.configure_logging()
    try:
        diagnostics.progress(cast(diagnostics.ReviewPhase, "private-answer"))
    except ValueError as error:
        diagnostics.log_failure("review_host_failed", error)
    result = json.loads(capsys.readouterr().err)
    assert result["locations"][-1]["module"] == "validation/aws_review/diagnostics.py"
    assert result["locations"][-1]["line"] > 0


def test_progress_only_accepts_fixed_phases_and_flushes_safe_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    diagnostics.configure_logging()
    diagnostics.progress("database_reset")
    assert json.loads(capsys.readouterr().err) == {"status": "running", "phase": "database_reset"}
    with pytest.raises(ValueError, match="review_phase_invalid"):
        diagnostics.progress(cast(diagnostics.ReviewPhase, "private-password"))
    assert capsys.readouterr().err == ""


def test_cli_does_not_promote_code_shaped_exception_text_to_public_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        review_cli, "configuration", Mock(side_effect=ValueError("review_private_answer"))
    )
    assert review_cli.main(["destroy"]) == 1
    result = json.loads(capsys.readouterr().err)
    assert result["code"] == "aws_review_operation_failed"
    assert result["locations"][-1]["module"] == "validation/aws_review/__main__.py"
    assert "private_answer" not in json.dumps(result)


def test_aws_diagnostics_only_include_known_service_operation_and_error_codes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    diagnostics.configure_logging()
    diagnostics.log_failure(
        "aws_review_operation_failed", AwsCallError("rds", "describe_db_instances", "AccessDenied")
    )
    result = json.loads(capsys.readouterr().err)
    assert result["aws_service"] == "rds"
    assert result["aws_operation"] == "describe_db_instances"
    assert result["aws_code"] == "AccessDenied"
    assert result["error_type"] == "AwsCallError"
    diagnostics.log_failure(
        "aws_review_operation_failed",
        AwsCallError("private-answer", "private-operation", "private-password"),
    )
    result = json.loads(capsys.readouterr().err)
    assert not any(key.startswith("aws_") for key in result)
    assert "private-" not in json.dumps(result)


@pytest.mark.parametrize("operation", ["GetSecretValue", "DescribeStackResource"])
def test_sdk_error_keeps_allowed_operation_and_code_but_discards_response_body(
    capsys: pytest.CaptureFixture[str],
    operation: str,
) -> None:
    diagnostics.configure_logging()
    error = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "private-password"}},
        operation,
    )
    diagnostics.log_failure("review_host_failed", error)
    result = json.loads(capsys.readouterr().err)
    assert result["aws_operation"] == operation
    assert result["aws_code"] == "AccessDeniedException"
    assert "private-password" not in json.dumps(result)
