import json
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from test_aws_review_deploy import CONFIG
from test_aws_review_deploy import owner_environment as owner_environment

from validation.aws_review import __main__ as cli
from validation.aws_review import diagnostics
from validation.aws_review.configuration_errors import configuration_issues
from validation.aws_review.models import DeploymentInput


@pytest.fixture
def execution_guards(monkeypatch: pytest.MonkeyPatch) -> tuple[Mock, Mock, Mock]:
    aws, git, log = Mock(), Mock(), Mock()
    monkeypatch.setattr(cli, "BotoAwsApi", aws)
    monkeypatch.setattr(cli.subprocess, "check_output", git)
    monkeypatch.setattr(cli, "configure_logging", Mock())
    monkeypatch.setattr(diagnostics.logfire, "error", log)
    return aws, git, log


@pytest.mark.parametrize("deployment_mode", ["create", "update"])
@pytest.mark.parametrize("retired_expiry", [None, "", "invalid-date", "2000-01-01T00:00:00Z"])
def test_deployment_generates_monitoring_expiry_without_user_datetime(
    owner_environment: None,
    execution_guards: tuple[Mock, Mock, Mock],
    monkeypatch: pytest.MonkeyPatch,
    deployment_mode: str,
    retired_expiry: str | None,
) -> None:
    now = datetime(2026, 10, 23, 3, tzinfo=UTC)
    clock = Mock(wraps=datetime)
    clock.now.return_value = now
    monkeypatch.setattr(cli, "datetime", clock)
    monkeypatch.setenv("AWS_REVIEW_DEPLOYMENT_MODE", deployment_mode)
    if retired_expiry is None:
        monkeypatch.delenv("AWS_MONITORING_EXPIRES_AT", raising=False)
    else:
        monkeypatch.setenv("AWS_MONITORING_EXPIRES_AT", retired_expiry)
    aws, git, log = execution_guards
    git.side_effect = [CONFIG.source_commit, ""]

    result = cli.configuration("deploy")

    assert isinstance(result, DeploymentInput)
    assert result.deployment_mode == deployment_mode
    assert result.monitoring_expires_at == now + timedelta(hours=12)
    aws.assert_not_called()
    log.assert_not_called()


@pytest.mark.parametrize(
    "variable,value",
    [
        ("AWS_ACCOUNT_ID", "private-account"),
        ("AWS_REVIEW_PUBLIC_ENTRYPOINT", "private-entrypoint"),
        ("AWS_REVIEW_DOMAIN_NAME", "private..example.org"),
        ("AWS_REVIEW_CERTIFICATE_ARN", "private-certificate"),
        ("AWS_REVIEW_ID", "private-review-with-a-name-too-long"),
        ("AWS_APP_AZ", "private-zone"),
        ("AWS_SECONDARY_AZ", "private-zone"),
        ("AWS_UBUNTU_AMI_ID", "ami-private-image"),
        ("AWS_DATABASE_ENGINE_VERSION", "private-version"),
        ("GITHUB_SHA", "private-commit"),
        ("VITE_EVENT_START_AT", "2031-04-05T06:07:08"),
        ("VITE_EVENT_START_AT", "2026-02-30T18:00:00+09:00"),
        ("VITE_SURVEY_URL", "http://private-survey.example.org/"),
        ("VITE_HOME_URL", "https://private-user:private-password@example.org/"),
        ("VITE_HOME_URL", f"https://{CONFIG.review_domain_name}/"),
        ("AWS_APP_AZ", '"ap-northeast-1a"'),
        ("AWS_UBUNTU_AMI_ID", "ami-0123456789abcdef0\n"),
    ],
)
def test_invalid_configuration_identifies_setting_without_values_or_aws_calls(
    owner_environment: None,
    execution_guards: tuple[Mock, Mock, Mock],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    variable: str,
    value: str,
) -> None:
    monkeypatch.setenv(variable, value)
    assert cli.main(["deploy"]) == 1
    output = capsys.readouterr()
    payload = json.loads(output.err)
    assert output.out == ""
    assert payload["status"] == "failed"
    assert payload["error_type"] == "ValidationError"
    assert payload["configuration_errors"][0]["variables"] == [variable]
    assert payload["configuration_errors"][0]["requirement"]
    aws, git, log = execution_guards
    aws.assert_not_called()
    git.assert_not_called()
    log.assert_called_once()
    assert value not in output.err
    assert "private-" not in repr(log.call_args)


@pytest.mark.parametrize("operation", ["deploy", "destroy"])
@pytest.mark.parametrize("variable", ["AWS_ACCOUNT_ID", "AWS_REVIEW_ID"])
@pytest.mark.parametrize("missing", [False, True])
def test_missing_or_empty_target_setting_is_reported_for_both_operations(
    owner_environment: None,
    execution_guards: tuple[Mock, Mock, Mock],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operation: str,
    variable: str,
    missing: bool,
) -> None:
    if missing:
        monkeypatch.delenv(variable)
    else:
        monkeypatch.setenv(variable, "")
    assert cli.main([operation]) == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["configuration_errors"][0]["variables"] == [variable]
    aws, git, _log = execution_guards
    aws.assert_not_called()
    git.assert_not_called()


def test_equal_zones_identifies_both_settings_without_printing_their_values(
    owner_environment: None,
    execution_guards: tuple[Mock, Mock, Mock],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AWS_SECONDARY_AZ", CONFIG.app_az)
    assert cli.main(["deploy"]) == 1
    output = capsys.readouterr().err
    issues = json.loads(output)["configuration_errors"]
    assert len(issues) == 1
    assert set(issues[0]["variables"]) == {"AWS_APP_AZ", "AWS_SECONDARY_AZ"}
    assert issues[0]["requirement"]
    aws, git, _log = execution_guards
    aws.assert_not_called()
    git.assert_not_called()


def test_multiple_invalid_settings_are_reported_together(
    owner_environment: None,
    execution_guards: tuple[Mock, Mock, Mock],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    variables = {"AWS_UBUNTU_AMI_ID", "VITE_SURVEY_URL", "VITE_HOME_URL"}
    for variable in variables:
        monkeypatch.setenv(variable, "private-input")
    assert cli.main(["deploy"]) == 1
    output = capsys.readouterr().err
    issues = json.loads(output)["configuration_errors"]
    assert {variable for issue in issues for variable in issue["variables"]} == variables
    assert "private-input" not in output
    aws, git, _log = execution_guards
    aws.assert_not_called()
    git.assert_not_called()


@pytest.mark.parametrize("location", [("private-location",), ("account_id", "private-location")])
def test_unknown_or_nested_location_is_not_disclosed(
    execution_guards: tuple[Mock, Mock, Mock],
    capsys: pytest.CaptureFixture[str],
    location: tuple[str, ...],
) -> None:
    error = ValidationError.from_exception_data(
        "private-title",
        [
            {
                "type": "value_error",
                "loc": location,
                "input": "private-input",
                "ctx": {"error": ValueError("private-message")},
            }
        ],
    )
    error.__cause__ = ValueError("private-cause")
    issues = configuration_issues(error)
    assert len(issues) == 1 and issues[0].variables == ()
    assert issues[0].requirement
    diagnostics.log_failure("aws_review_operation_failed", error, configuration_errors=issues)
    output = capsys.readouterr().err
    payload = json.loads(output)
    assert payload["configuration_errors"][0]["variables"] == []
    assert set(payload["configuration_errors"][0]) == {"variables", "requirement"}
    assert "private-" not in output
    assert "private-" not in repr(execution_guards[2].call_args)


def test_validation_failure_after_configuration_does_not_claim_invalid_settings(
    execution_guards: tuple[Mock, Mock, Mock],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = ValidationError.from_exception_data(
        "private-aws-response",
        [{"type": "string_type", "loc": ("account_id",), "input": 123}],
    )
    monkeypatch.setattr(cli, "configuration", Mock(return_value=CONFIG))
    execution_guards[0].side_effect = error
    assert cli.main(["deploy"]) == 1
    output = capsys.readouterr().err
    payload = json.loads(output)
    assert payload["error_type"] == "ValidationError"
    assert "configuration_errors" not in payload
    assert "AWS_ACCOUNT_ID" not in output
    assert "private-" not in output


@pytest.mark.parametrize("settings", [None, "", "private-unused-setting"])
def test_cloudfront_configuration_does_not_require_custom_domain_settings(
    owner_environment: None,
    execution_guards: tuple[Mock, Mock, Mock],
    monkeypatch: pytest.MonkeyPatch,
    settings: str | None,
) -> None:
    monkeypatch.setenv("AWS_REVIEW_PUBLIC_ENTRYPOINT", "cloudfront")
    for variable in ("AWS_REVIEW_DOMAIN_NAME", "AWS_REVIEW_CERTIFICATE_ARN"):
        if settings is None:
            monkeypatch.delenv(variable)
        else:
            monkeypatch.setenv(variable, settings)
    aws, git, log = execution_guards
    git.side_effect = [CONFIG.source_commit, ""]
    result = cli.configuration("deploy")
    assert isinstance(result, DeploymentInput)
    assert result.public_entrypoint == "cloudfront"
    assert result.public_url is None
    assert result.review_domain_name == result.review_certificate_arn == ""
    aws.assert_not_called()
    log.assert_not_called()


@pytest.mark.parametrize("field", ["review_domain_name", "review_certificate_arn"])
def test_default_custom_domain_requires_explicit_domain_and_certificate(field: str) -> None:
    values = CONFIG.model_dump(exclude={"public_entrypoint", field})
    with pytest.raises(ValidationError) as failure:
        DeploymentInput.model_validate(values)
    assert failure.value.errors()[0]["loc"] == (field,)


def test_public_entrypoint_defaults_to_existing_custom_domain() -> None:
    result = DeploymentInput.model_validate(CONFIG.model_dump(exclude={"public_entrypoint"}))
    assert result.public_entrypoint == "custom-domain"
    assert result.public_url == CONFIG.public_url
