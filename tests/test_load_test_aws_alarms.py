from __future__ import annotations

import json
from typing import cast

import pytest
from test_load_test_aws import NOW, FakeAws, ResponseT, plan
from test_load_test_aws_deadline import _schedule

from quiz_app.load_test_aws import lifecycle
from quiz_app.load_test_aws.alarms import expected_alarm, require_deadline_alarm
from quiz_app.load_test_aws.aws import AwsCallError
from quiz_app.load_test_aws.responses import Stack
from quiz_app.load_test_aws.templates import tags, template_body


def _topic() -> str:
    configuration = plan()
    return (
        f"arn:aws:sns:{configuration.region}:{configuration.account_id}:"
        f"{configuration.prefix}-notifications"
    )


def _alarm() -> dict[str, object]:
    configuration = plan()
    expected = expected_alarm(configuration, _topic())
    return {
        **expected.model_dump(by_alias=True, exclude_none=True),
        "AlarmArn": (
            f"arn:aws:cloudwatch:{configuration.region}:{configuration.account_id}:"
            f"alarm:{expected.name}"
        ),
    }


class CreatingAlarmAws(FakeAws):
    def __init__(self, *, visible_after_create: bool = True) -> None:
        super().__init__()
        self.visible_after_create = visible_after_create
        self.responses[("cloudwatch", "list_tags_for_resource")] = {"Tags": tags(plan())}

    def call(
        self, service: str, operation: str, response: type[ResponseT], **kwargs: object
    ) -> ResponseT:
        result = super().call(service, operation, response, **kwargs)
        if operation == "put_metric_alarm" and self.visible_after_create:
            self.responses[("cloudwatch", "describe_alarms")] = {"MetricAlarms": [_alarm()]}
        return result


def test_creating_alarm_verifies_exact_configuration_and_ownership() -> None:
    api = CreatingAlarmAws()
    require_deadline_alarm(api, plan(), _topic(), create_missing=True)
    assert [operation for _, operation, _ in api.calls] == [
        "describe_alarms",
        "put_metric_alarm",
        "describe_alarms",
        "list_tags_for_resource",
    ]
    assert all(
        arguments["AlarmTypes"] == ["MetricAlarm", "CompositeAlarm", "LogAlarm"]
        for _, operation, arguments in api.calls
        if operation == "describe_alarms"
    )
    arguments = api.calls[1][2]
    assert arguments["Tags"] == tags(plan())
    assert arguments["AlarmActions"] == (_topic(),)
    assert arguments["DatapointsToAlarm"] == 1
    assert "Unit" not in arguments


def test_existing_owned_alarm_is_read_only() -> None:
    api = CreatingAlarmAws()
    api.responses[("cloudwatch", "describe_alarms")] = {"MetricAlarms": [_alarm()]}
    require_deadline_alarm(api, plan(), _topic(), create_missing=True)
    assert [operation for _, operation, _ in api.calls] == [
        "describe_alarms",
        "list_tags_for_resource",
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("AlarmArn", "arn:foreign"),
        ("AlarmName", "foreign"),
        ("ActionsEnabled", False),
        ("AlarmActions", ["arn:foreign"]),
        ("Dimensions", [{"Name": "QueueName", "Value": "another-queue"}]),
        ("Threshold", 1),
        ("Period", 300),
        ("Unit", "Count"),
    ],
)
def test_existing_alarm_configuration_drift_cannot_be_overwritten(
    field: str, value: object
) -> None:
    api = CreatingAlarmAws()
    api.responses[("cloudwatch", "describe_alarms")] = {
        "MetricAlarms": [{**_alarm(), field: value}]
    }
    with pytest.raises(ValueError, match="configuration_mismatch"):
        require_deadline_alarm(api, plan(), _topic(), create_missing=True)
    assert [operation for _, operation, _ in api.calls] == ["describe_alarms"]


@pytest.mark.parametrize("missing", ["Project", "Purpose", "RunId", "ExpiresAt"])
def test_existing_alarm_without_full_ownership_cannot_be_overwritten(missing: str) -> None:
    api = CreatingAlarmAws()
    api.responses[("cloudwatch", "describe_alarms")] = {"MetricAlarms": [_alarm()]}
    api.responses[("cloudwatch", "list_tags_for_resource")] = {
        "Tags": [item for item in tags(plan()) if item["Key"] != missing]
    }
    with pytest.raises(ValueError, match="ownership_mismatch"):
        require_deadline_alarm(api, plan(), _topic(), create_missing=True)
    assert all(operation != "put_metric_alarm" for _, operation, _ in api.calls)


@pytest.mark.parametrize(
    "response",
    [
        {"CompositeAlarms": [{"AlarmName": "same-name"}]},
        {"LogAlarms": [{"AlarmName": "same-name"}]},
        {"MetricAlarms": [_alarm()], "LogAlarms": [{"AlarmName": "same-name"}]},
        {"NextToken": "unexpected-page"},
        {"MetricAlarms": [_alarm(), _alarm()]},
    ],
)
def test_ambiguous_alarm_identity_refuses_creation(response: dict[str, object]) -> None:
    api = CreatingAlarmAws()
    api.responses[("cloudwatch", "describe_alarms")] = response
    with pytest.raises(ValueError, match="identity_mismatch"):
        require_deadline_alarm(api, plan(), _topic(), create_missing=True)
    assert [operation for _, operation, _ in api.calls] == ["describe_alarms"]


def test_failed_readback_is_bounded_and_never_repeats_put() -> None:
    api = CreatingAlarmAws(visible_after_create=False)
    with pytest.raises(ValueError, match="alarm_missing"):
        require_deadline_alarm(api, plan(), _topic(), create_missing=True)
    assert [operation for _, operation, _ in api.calls] == [
        "describe_alarms",
        "put_metric_alarm",
        "describe_alarms",
    ]


def test_read_only_alarm_check_does_not_create_missing_alarm() -> None:
    api = CreatingAlarmAws()
    with pytest.raises(ValueError, match="alarm_missing"):
        require_deadline_alarm(api, plan(), _topic())
    assert [operation for _, operation, _ in api.calls] == ["describe_alarms"]


def test_alarm_permission_denial_cannot_be_treated_as_missing() -> None:
    api = CreatingAlarmAws()
    api.responses[("cloudwatch", "describe_alarms")] = AwsCallError(
        "cloudwatch", "describe_alarms", "AccessDenied"
    )
    with pytest.raises(AwsCallError, match="AccessDenied"):
        require_deadline_alarm(api, plan(), _topic(), create_missing=True)
    assert [operation for _, operation, _ in api.calls] == ["describe_alarms"]


def _prepare_api(monkeypatch: pytest.MonkeyPatch) -> CreatingAlarmAws:
    api = CreatingAlarmAws()
    control = Stack(
        StackId="arn:stack",
        StackName=plan().control_stack,
        StackStatus="CREATE_COMPLETE",
        Tags=tags(plan()),
        Outputs=[
            {"OutputKey": "NotificationTopicArn", "OutputValue": _topic()},
            {"OutputKey": "SchedulerRoleArn", "OutputValue": "arn:scheduler-role"},
            {"OutputKey": "DeadlineQueueArn", "OutputValue": "arn:deadline-queue"},
            {"OutputKey": "WorkerRoleArn", "OutputValue": "arn:worker-role"},
        ],
    )
    monkeypatch.setattr(lifecycle, "preflight", lambda *args, **kwargs: None)
    monkeypatch.setattr(lifecycle, "validate_worker_package", lambda *args: None)
    monkeypatch.setattr(lifecycle, "ensure_execution_role", lambda *args: False)
    monkeypatch.setattr(lifecycle, "find_stack", lambda *args: control)
    api.responses[("scheduler", "get_schedule")] = _schedule()
    api.responses[("lambda", "create_function")] = {"FunctionArn": "arn:worker"}
    api.page_responses[("sns", "list_subscriptions_by_topic")] = [
        {
            "Subscriptions": [
                {
                    "Protocol": "email",
                    "Endpoint": plan().notification_email.get_secret_value(),
                    "SubscriptionArn": _topic() + ":subscription-id",
                }
            ]
        }
    ]
    return api


@pytest.mark.parametrize("blocked_guard", ["notification", "deadline"])
def test_prepare_cannot_create_alarm_before_notification_and_deadline_readback(
    monkeypatch: pytest.MonkeyPatch, blocked_guard: str
) -> None:
    api = _prepare_api(monkeypatch)
    if blocked_guard == "notification":
        api.page_responses[("sns", "list_subscriptions_by_topic")] = []
    else:
        schedule = cast(dict[str, object], api.responses[("scheduler", "get_schedule")])
        schedule["State"] = "DISABLED"
    with pytest.raises(ValueError, match=blocked_guard):
        lifecycle.prepare(api, plan(), b"worker", now=NOW, manifest_bytes=b"manifest")
    assert all(service in {"sns", "scheduler"} for service, _, _ in api.calls)


def test_prepare_orders_both_guards_before_alarm_and_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _prepare_api(monkeypatch)
    result = lifecycle.prepare(api, plan(), b"worker", now=NOW, manifest_bytes=b"manifest")
    assert result.status == "worker_creating"
    assert [operation for _, operation, _ in api.calls] == [
        "list_subscriptions_by_topic",
        "get_schedule",
        "describe_alarms",
        "put_metric_alarm",
        "describe_alarms",
        "list_tags_for_resource",
        "get_function_configuration",
        "create_function",
    ]


def test_control_template_cannot_create_billable_alarm_before_guards() -> None:
    control = json.loads(template_body("control"))
    assert all(
        resource["Type"] not in {"AWS::CloudWatch::Alarm", "AWS::CloudWatch::CompositeAlarm"}
        for resource in control["Resources"].values()
    )
