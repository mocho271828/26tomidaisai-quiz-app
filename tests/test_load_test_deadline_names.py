from __future__ import annotations

import json
from typing import cast
from uuid import UUID

import pytest
from test_load_test_aws import FakeAws, ResponseT, plan
from test_load_test_aws_deadline import _control, _schedule

from quiz_app.load_test_aws.lifecycle import require_deadline
from quiz_app.load_test_aws.models import AwsRunPlan, deadline_target
from quiz_app.load_test_aws.storage import scan_active_control
from quiz_app.load_test_aws.templates import parameters, template_body

RUN_IDS = (
    UUID("0a758be1-15dd-4e1a-a4c6-d32d1f41a701"),
    UUID("09d35384-8cbd-445d-a8f4-6ab7ce8a7584"),
)


class DeadlineAws(FakeAws):
    def __init__(self, configuration: AwsRunPlan, *, armed: bool) -> None:
        super().__init__()
        self.schedule = _schedule()
        cast(dict[str, object], self.schedule["Target"])["Input"] = json.dumps(
            deadline_target(configuration.cleanup_input())
        )
        self.schedule["ActionAfterCompletion"] = "DELETE" if armed else "NONE"
        self.responses[("scheduler", "get_schedule")] = self.schedule

    def call(
        self, service: str, operation: str, response: type[ResponseT], **kwargs: object
    ) -> ResponseT:
        if service == "scheduler" and operation == "update_schedule":
            self.schedule["ActionAfterCompletion"] = "DELETE"
        return super().call(service, operation, response, **kwargs)


@pytest.mark.parametrize("run_id", RUN_IDS)
@pytest.mark.parametrize("armed", [False, True])
def test_deadline_creation_read_update_and_inventory_share_run_scoped_name(
    run_id: UUID, armed: bool
) -> None:
    configuration = AwsRunPlan.model_validate({**plan().model_dump(), "run_id": run_id})
    template = json.loads(template_body("control"))
    schedules = [
        resource["Properties"]
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::Scheduler::Schedule"
    ]
    assert len(schedules) == 1
    assert schedules[0]["Name"] == {"Fn::Sub": "${Prefix}-deadline"}
    template_parameters = {
        item["ParameterKey"]: item["ParameterValue"]
        for item in parameters(configuration, "control")
    }
    schedule_name = f"{template_parameters['Prefix']}-deadline"
    assert schedule_name == f"gs-lt-{run_id.hex}-deadline"
    assert len(schedule_name) <= 64

    api = DeadlineAws(configuration, armed=armed)
    require_deadline(api, configuration, _control(), arm_auto_delete=True)
    residuals = scan_active_control(api, configuration.cleanup_input())
    assert [(item.kind, item.state) for item in residuals] == [("deadline-schedule", "ENABLED")]
    scheduler_calls = [
        (operation, args) for service, operation, args in api.calls if service == "scheduler"
    ]
    assert [operation for operation, _ in scheduler_calls] == (
        ["get_schedule", "get_schedule"]
        if armed
        else ["get_schedule", "update_schedule", "get_schedule", "get_schedule"]
    )
    assert all(
        arguments["Name"] == schedule_name and arguments["GroupName"] == configuration.prefix
        for _, arguments in scheduler_calls
    )


@pytest.mark.parametrize("run_id", RUN_IDS)
def test_deadline_absence_looks_up_only_current_run_schedule(run_id: UUID) -> None:
    configuration = AwsRunPlan.model_validate({**plan().model_dump(), "run_id": run_id})
    api = FakeAws()
    assert scan_active_control(api, configuration.cleanup_input()) == ()
    assert api.calls[-1] == (
        "scheduler",
        "get_schedule",
        {"GroupName": configuration.prefix, "Name": f"{configuration.prefix}-deadline"},
    )
