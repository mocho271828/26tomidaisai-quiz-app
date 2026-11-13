"""待機ログの対象識別、機密値除外、追加AWS照会の上限を検証する。"""

import json
from itertools import chain, repeat
from pathlib import Path
from unittest.mock import Mock

import pytest
from botocore.exceptions import (
    CredentialRetrievalError,
    EndpointConnectionError,
    NoCredentialsError,
    OperationNotPageableError,
    PaginationError,
    ReadTimeoutError,
)
from pydantic import ValidationError
from test_aws_review_deploy import Clock, owned_stack
from test_aws_review_destroy import TARGET, stack
from test_load_test_aws import FakeAws

from quiz_app.load_test_aws.aws import AwsCallError
from validation.aws_review import destroy, diagnostics, resource_progress, stacks
from validation.aws_review.diagnostics import ProgressContext
from validation.aws_review.resource_progress import StackProgress


@pytest.fixture(autouse=True)
def configure_logging() -> None:
    diagnostics.configure_logging()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    value = Clock()
    monkeypatch.setattr(resource_progress.time, "monotonic", value.monotonic)
    monkeypatch.setattr(resource_progress.time, "sleep", value.sleep)
    return value


def resource(
    name: str = "AppInstance", kind: str = "AWS::EC2::Instance", state: str = "CREATE_IN_PROGRESS"
) -> dict[str, str]:
    return {
        "LogicalResourceId": name,
        "ResourceType": kind,
        "ResourceStatus": state,
        "PhysicalResourceId": "private-instance-identity",
        "ResourceStatusReason": "private-password-in-reason",
    }


def test_resource_progress_uses_owned_stack_and_only_changed_safe_statuses(
    clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeAws()
    identity = owned_stack().arn
    records = [resource(), resource("Database", "AWS::RDS::DBInstance")]
    api.page_responses[("cloudformation", "list_stack_resources")] = [
        {"StackResourceSummaries": records}
    ]
    reporter = StackProgress("workload")
    reporter.resources(api, identity)
    initial = capsys.readouterr().err
    emitted = [json.loads(line) for line in initial.splitlines()]
    assert [item["resource"] for item in emitted] == ["AppInstance", "Database"]
    assert "アプリ実行用EC2" in initial and "PostgreSQLデータベース" in initial
    assert "private-" not in initial and identity not in initial
    assert api.calls == [("cloudformation", "list_stack_resources", {"StackName": identity})]
    clock.sleep(59)
    reporter.resources(api, identity)
    assert len(api.calls) == 1
    clock.sleep(1)
    reporter.resources(api, identity)
    assert len(api.calls) == 2
    assert capsys.readouterr().err == ""
    clock.sleep(60)
    records[0]["ResourceStatus"] = "CREATE_COMPLETE"
    reporter.resources(api, identity)
    changed = json.loads(capsys.readouterr().err)
    assert changed["resource"] == "AppInstance" and changed["state"] == "CREATE_COMPLETE"


def test_resource_progress_reads_all_pages_and_ignores_unapproved_names_types_and_stacks(
    clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeAws()
    api.page_responses[("cloudformation", "list_stack_resources")] = [
        {
            "StackResourceSummaries": [
                resource(name="private-secret"),
                resource(kind="private-type"),
                resource("Distribution", "AWS::CloudFront::Distribution"),
            ]
        },
        {"StackResourceSummaries": [resource(state="private-status")]},
    ]
    StackProgress("workload").resources(api, owned_stack().arn)
    output = capsys.readouterr().err
    assert json.loads(output)["state"] == "UNKNOWN"
    assert "private-" not in output and "Distribution" not in output


@pytest.mark.parametrize("field", ["stack", "resource", "state", "step"])
def test_progress_revalidates_context_before_emitting(
    field: str, capsys: pytest.CaptureFixture[str]
) -> None:
    context = ProgressContext(
        stack="workload", step="stack", resource="AppInstance", state="WAITING"
    )
    invalid = context.model_copy(update={field: "private-secret"})
    with pytest.raises(ValidationError):
        diagnostics.progress("stack_wait", context=invalid)
    assert capsys.readouterr().err == ""


def test_progress_rejects_resource_from_another_stack(capsys: pytest.CaptureFixture[str]) -> None:
    context = ProgressContext(stack="edge", step="stack", resource="AppInstance", state="WAITING")
    with pytest.raises(ValueError, match="resource_stack_mismatch"):
        diagnostics.progress("stack_wait", context=context)
    assert capsys.readouterr().err == ""


def test_stack_timeout_has_at_most_45_additional_resource_polls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    template = tmp_path / "template.yaml"
    template.write_text("Resources: {}\n")
    api = FakeAws()
    api.responses[("cloudformation", "create_change_set")] = {"Id": "change-id"}
    api.responses[("cloudformation", "describe_change_set")] = {
        "Status": "CREATE_COMPLETE",
        "ExecutionStatus": "EXECUTE_IN_PROGRESS",
    }
    monkeypatch.setattr(
        stacks,
        "get_stack",
        Mock(side_effect=chain([None], repeat(owned_stack(status="CREATE_IN_PROGRESS")))),
    )
    with pytest.raises(TimeoutError, match="stack_deployment_timeout"):
        stacks.apply_stack(api, TARGET, "workload", template, {})
    polls = [
        arguments for _, operation, arguments in api.calls if operation == "list_stack_resources"
    ]
    assert len(polls) == 45
    assert all(arguments == {"StackName": owned_stack().arn} for arguments in polls)
    assert clock.now == 2700


def test_destroy_wait_reports_target_and_polls_at_most_once_per_minute(
    clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeAws()
    current = stack("workload", "DELETE_IN_PROGRESS")
    api.responses[("cloudformation", "describe_stacks")] = {
        "Stacks": [current.model_dump(by_alias=True)]
    }
    api.page_responses[("cloudformation", "list_stack_resources")] = [
        {"StackResourceSummaries": [resource(state="DELETE_IN_PROGRESS")]}
    ]
    with pytest.raises(TimeoutError, match="destroy_incomplete"):
        destroy._delete_stack(api, TARGET, current, deadline=119)
    assert sum(operation == "list_stack_resources" for _, operation, _ in api.calls) == 2
    assert clock.now == 119
    output = capsys.readouterr().err
    assert "AWSリソースの削除" in output and "アプリ実行用EC2" in output
    assert "private-" not in output


def test_ssm_progress_deduplicates_states_and_keeps_unknown_values_private(
    capsys: pytest.CaptureFixture[str],
) -> None:
    reporter = StackProgress("workload", "ssm_wait")
    for state in ("WAITING", "Online", "Online", "private-secret"):
        reporter.report(state, step="ssm_connection", resource="AppInstance")
    reporter.report("InProgress", step="ssm_command", resource="AppInstance")
    output = capsys.readouterr().err
    emitted = [json.loads(line) for line in output.splitlines()]
    assert [item["state"] for item in emitted] == ["WAITING", "Online", "UNKNOWN", "InProgress"]
    assert "SSM接続の準備" in output and "アプリ配置・DB初期化の実行" in output
    assert "private-" not in output


@pytest.mark.parametrize(
    "error",
    [
        AwsCallError("cloudformation", "list_stack_resources", "AccessDenied"),
        AwsCallError("cloudformation", "list_stack_resources", "Throttling"),
        AwsCallError("private-service", "private-operation", "private-code"),
        EndpointConnectionError(endpoint_url="https://private-endpoint"),
        ReadTimeoutError(endpoint_url="https://private-endpoint"),
        PaginationError(message="private-repeated-next-token"),
        OperationNotPageableError(operation_name="private-operation"),
        NoCredentialsError(),
        CredentialRetrievalError(provider="private-provider", error_msg="private-credential"),
        ValidationError.from_exception_data("private-model", []),
    ],
)
def test_optional_progress_failure_disables_only_further_detail_polls(
    error: BaseException,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    api = FakeAws()
    pages = Mock(side_effect=error)
    monkeypatch.setattr(api, "pages", pages)
    reporter = StackProgress("workload")
    reporter.resources(api, owned_stack().arn)
    clock.sleep(60)
    reporter.resources(api, owned_stack().arn)
    pages.assert_called_once()
    reporter.report("CREATE_COMPLETE")
    output = capsys.readouterr().err
    emitted = [json.loads(line) for line in output.splitlines()]
    assert [item["state"] for item in emitted] == ["UNAVAILABLE", "CREATE_COMPLETE"]
    assert all(item["status"] == "running" for item in emitted)
    assert "詳細進捗取得不可（通常の完了判定を継続）" in output
    assert "private-" not in output


@pytest.mark.parametrize("status", ["CREATE_COMPLETE", "CREATE_FAILED"])
def test_optional_progress_denial_does_not_override_mandatory_stack_result(
    status: str, tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = tmp_path / "template.yaml"
    template.write_text("Resources: {}\n")
    api = FakeAws()
    api.responses[("cloudformation", "create_change_set")] = {"Id": "change-id"}
    api.responses[("cloudformation", "describe_change_set")] = {
        "Status": "CREATE_COMPLETE",
        "ExecutionStatus": "EXECUTE_COMPLETE",
    }
    final = owned_stack(status=status)
    monkeypatch.setattr(
        stacks,
        "get_stack",
        Mock(side_effect=[None, owned_stack(status="CREATE_IN_PROGRESS"), final]),
    )
    monkeypatch.setattr(
        api,
        "pages",
        Mock(side_effect=AwsCallError("cloudformation", "list_stack_resources", "AccessDenied")),
    )
    if status == "CREATE_COMPLETE":
        assert stacks.apply_stack(api, TARGET, "workload", template, {}) == final
    else:
        with pytest.raises(ValueError, match="stack_deployment_failed"):
            stacks.apply_stack(api, TARGET, "workload", template, {})
    assert clock.now == 15


@pytest.mark.parametrize("status", ["DELETE_COMPLETE", "DELETE_FAILED"])
def test_optional_progress_denial_does_not_override_mandatory_destroy_result(
    status: str, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeAws()
    current = stack("workload", "DELETE_IN_PROGRESS")
    responses = [
        destroy.Stacks(stacks=(current,)),
        destroy.Stacks(stacks=(stack("workload", status),)),
    ]
    calls = Mock(side_effect=responses)
    monkeypatch.setattr(api, "call", calls)
    pages = Mock(side_effect=AwsCallError("cloudformation", "list_stack_resources", "AccessDenied"))
    monkeypatch.setattr(api, "pages", pages)
    if status == "DELETE_COMPLETE":
        destroy._delete_stack(api, TARGET, current, deadline=120)
    else:
        with pytest.raises(ValueError, match="destroy_stack_failed"):
            destroy._delete_stack(api, TARGET, current, deadline=120)
    assert clock.now == 30
    assert calls.call_count == 2
    pages.assert_called_once()
