from __future__ import annotations

import json
from uuid import UUID

import pytest
from test_load_test_aws import RUN_ID, plan

from quiz_app.load_test_aws.bootstrap import execution_policy_documents
from quiz_app.load_test_aws.models import AwsRunPlan
from quiz_app.load_test_aws.templates import template_body


def test_schedule_physical_name_is_unique_even_across_groups() -> None:
    template = json.loads(template_body("control"))
    expected = "${Prefix}-deadline"
    assert template["Resources"]["Deadline"]["Properties"]["Name"] == {"Fn::Sub": expected}
    machine = next(
        item
        for item in template["Resources"].values()
        if item["Type"] == "AWS::StepFunctions::StateMachine"
    )
    states = json.loads(machine["Properties"]["DefinitionString"]["Fn::Sub"][0])["States"]
    for operation in ("DeleteDeadline", "VerifyDeadline"):
        assert states[operation]["Parameters"] == {"Name": expected, "GroupName": "${Prefix}"}


@pytest.mark.parametrize("run_id", [RUN_ID, UUID("a1234567-1234-4234-8234-123456789012")])
def test_cfn_name_conflict_probe_can_only_read_exact_default_group_name(run_id: UUID) -> None:
    configuration = AwsRunPlan.model_validate({**plan().model_dump(), "run_id": run_id})
    statements = [
        item
        for body in execution_policy_documents(configuration)
        for item in json.loads(body)["Statement"]
        if "schedule/default/" in json.dumps(item["Resource"])
    ]
    assert statements == [
        {
            "Sid": "ReadRunScheduleNameConflictProbe",
            "Effect": "Allow",
            "Action": ["scheduler:GetSchedule"],
            "Resource": (
                f"arn:aws:scheduler:{configuration.region}:{configuration.account_id}:"
                f"schedule/default/{configuration.prefix}-deadline"
            ),
        }
    ]


def test_cfn_can_validate_aws_specific_image_and_zone_parameters() -> None:
    statements = [
        item
        for body in execution_policy_documents(plan())
        for item in json.loads(body)["Statement"]
        if item["Sid"] == "ReadEc2"
    ]
    assert len(statements) == 1
    statement = statements[0]
    assert {"ec2:DescribeImages", "ec2:DescribeAvailabilityZones"} <= set(statement["Action"])
    assert all(action.startswith("ec2:Describe") for action in statement["Action"])
    assert statement["Resource"] == "*"
    assert statement["Condition"] == {"StringEquals": {"aws:RequestedRegion": plan().region}}
