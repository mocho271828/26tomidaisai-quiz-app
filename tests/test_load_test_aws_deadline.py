from __future__ import annotations

import json
from typing import cast

import pytest
from test_load_test_aws import FakeAws, ResponseT, plan

from quiz_app.load_test_aws.lifecycle import require_deadline
from quiz_app.load_test_aws.models import deadline_target
from quiz_app.load_test_aws.responses import Stack


def _schedule() -> dict[str, object]:
    configuration = plan()
    return {
        "State": "ENABLED",
        "ScheduleExpression": configuration.expires_at.strftime("at(%Y-%m-%dT%H:%M:%S)"),
        "ScheduleExpressionTimezone": "UTC",
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "Target": {
            "Arn": "arn:aws:scheduler:::aws-sdk:sfn:startExecution",
            "Input": json.dumps(deadline_target(configuration.cleanup_input())),
            "RoleArn": "arn:scheduler-role",
            "DeadLetterConfig": {"Arn": "arn:deadline-queue"},
            "RetryPolicy": {"MaximumEventAgeInSeconds": 3600, "MaximumRetryAttempts": 20},
        },
        "ActionAfterCompletion": "DELETE",
    }


def _control() -> Stack:
    return Stack(
        StackId="arn:stack",
        StackName=plan().control_stack,
        StackStatus="CREATE_COMPLETE",
        Outputs=[
            {"OutputKey": "SchedulerRoleArn", "OutputValue": "arn:scheduler-role"},
            {"OutputKey": "DeadlineQueueArn", "OutputValue": "arn:deadline-queue"},
        ],
    )


@pytest.mark.parametrize(
    ("path", "unexpected"),
    [
        (("Target", "RoleArn"), "arn:other-role"),
        (("Target", "DeadLetterConfig", "Arn"), "arn:other-queue"),
        (("Target", "RetryPolicy", "MaximumRetryAttempts"), 0),
        (("Target", "RetryPolicy", "MaximumEventAgeInSeconds"), 60),
        (("ScheduleExpressionTimezone",), "Asia/Tokyo"),
        (("FlexibleTimeWindow", "Mode"), "FLEXIBLE"),
    ],
)
def test_deadline_refuses_target_or_timing_drift_without_modification(
    path: tuple[str, ...], unexpected: object
) -> None:
    api = FakeAws()
    schedule = _schedule()
    item = schedule
    for key in path[:-1]:
        item = cast(dict[str, object], item[key])
    item[path[-1]] = unexpected
    api.responses[("scheduler", "get_schedule")] = schedule
    with pytest.raises(ValueError, match="deadline_not_armed"):
        require_deadline(api, plan(), _control(), arm_auto_delete=True)
    assert [operation for _, operation, _ in api.calls] == ["get_schedule"]


@pytest.mark.parametrize("missing", ["RoleArn", "DeadLetterConfig", "RetryPolicy"])
def test_deadline_cannot_accept_missing_guard_configuration(missing: str) -> None:
    api = FakeAws()
    schedule = _schedule()
    cast(dict[str, object], schedule["Target"]).pop(missing)
    api.responses[("scheduler", "get_schedule")] = schedule
    with pytest.raises(ValueError):
        require_deadline(api, plan(), _control())


def test_verified_deadline_requires_no_write() -> None:
    api = FakeAws()
    api.responses[("scheduler", "get_schedule")] = _schedule()
    require_deadline(api, plan(), _control())
    assert [operation for _, operation, _ in api.calls] == ["get_schedule"]


class ArmingAws(FakeAws):
    def __init__(self, *, alter_role: bool) -> None:
        super().__init__()
        self.alter_role = alter_role
        self.responses[("scheduler", "get_schedule")] = {
            **_schedule(),
            "ActionAfterCompletion": "NONE",
        }

    def call(
        self, service: str, operation: str, response: type[ResponseT], **kwargs: object
    ) -> ResponseT:
        if operation == "update_schedule":
            schedule = _schedule()
            if self.alter_role:
                cast(dict[str, object], schedule["Target"])["RoleArn"] = "arn:drifted-role"
            self.responses[("scheduler", "get_schedule")] = schedule
        return super().call(service, operation, response, **kwargs)


@pytest.mark.parametrize("alter_role", [False, True])
def test_arming_self_deletion_reads_back_full_target(alter_role: bool) -> None:
    api = ArmingAws(alter_role=alter_role)
    if alter_role:
        with pytest.raises(ValueError, match="deadline_not_armed"):
            require_deadline(api, plan(), _control(), arm_auto_delete=True)
    else:
        require_deadline(api, plan(), _control(), arm_auto_delete=True)
    assert [operation for _, operation, _ in api.calls] == [
        "get_schedule",
        "update_schedule",
        "get_schedule",
    ]
