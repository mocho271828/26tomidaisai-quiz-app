from pathlib import Path
from typing import Literal
from unittest.mock import Mock

import pytest
from test_aws_review_deploy import TARGET, owned_stack
from test_aws_review_deploy import template as template
from test_load_test_aws import FakeAws

from validation.aws_review import stacks


def target_detail(name: str, *, recreation: str = "Never") -> dict[str, object]:
    return {
        "Target": {
            "Attribute": "Properties",
            "Name": name,
            "RequiresRecreation": recreation,
        }
    }


def resource_change(**overrides: object) -> dict[str, object]:
    resource = {
        "Action": "Modify",
        "LogicalResourceId": "AppInstance",
        "ResourceType": "AWS::EC2::Instance",
        "Replacement": "False",
        "Scope": ["Properties"],
        "Details": [target_detail("Monitoring")],
        **overrides,
    }
    return {"Type": "Resource", "ResourceChange": resource}


def configure_api(
    monkeypatch: pytest.MonkeyPatch,
    changes: object,
    *,
    kind: Literal["control", "workload", "edge"] = "workload",
    is_create: bool = False,
    **state_fields: object,
) -> FakeAws:
    api = FakeAws()
    final = owned_stack(kind, "CREATE_COMPLETE" if is_create else "UPDATE_COMPLETE")
    monkeypatch.setattr(
        stacks, "get_stack", Mock(side_effect=[None if is_create else owned_stack(kind), final])
    )
    api.responses[("cloudformation", "create_change_set")] = {"Id": "change-id"}
    api.responses[("cloudformation", "describe_change_set")] = {
        "Status": "CREATE_COMPLETE",
        "ExecutionStatus": "EXECUTE_COMPLETE",
        "Changes": changes,
        **state_fields,
    }
    return api


@pytest.mark.parametrize(
    "changes",
    [
        [resource_change(Details=[target_detail("InstanceType")])],
        [resource_change()],
        [resource_change(Details=[target_detail("CreditSpecification")])],
        [
            resource_change(),
            resource_change(
                LogicalResourceId="AppRole",
                ResourceType="AWS::IAM::Role",
                Details=[target_detail("Policies")],
            ),
        ],
    ],
)
def test_workload_update_allows_only_reviewed_capacity_and_monitoring_properties(
    template: Path, monkeypatch: pytest.MonkeyPatch, changes: object
) -> None:
    api = configure_api(monkeypatch, changes)
    assert stacks.apply_stack(api, TARGET, "workload", template, {}).status == "UPDATE_COMPLETE"
    assert sum(op == "execute_change_set" for _, op, _ in api.calls) == 1
    assert not any(op == "delete_change_set" for _, op, _ in api.calls)


@pytest.mark.parametrize(
    "logical_id,resource_type",
    [
        ("Database", "AWS::RDS::DBInstance"),
        ("Redis", "AWS::ElastiCache::ReplicationGroup"),
        ("RedisAuthSecret", "AWS::SecretsManager::Secret"),
        ("AppSecurityGroup", "AWS::EC2::SecurityGroup"),
    ],
)
@pytest.mark.parametrize(
    "action,replacement",
    [("Remove", None), ("Modify", "False"), ("Modify", "True"), ("Modify", "Conditional")],
)
def test_workload_update_never_changes_or_removes_data_and_unrelated_resources(
    template: Path,
    monkeypatch: pytest.MonkeyPatch,
    logical_id: str,
    resource_type: str,
    action: str,
    replacement: str | None,
) -> None:
    changes = [
        resource_change(),
        resource_change(
            LogicalResourceId=logical_id,
            ResourceType=resource_type,
            Action=action,
            Replacement=replacement,
        ),
    ]
    api = configure_api(monkeypatch, changes)
    with pytest.raises(ValueError, match="^review_workload_update_change_not_allowed$"):
        stacks.apply_stack(
            api, TARGET, "workload", template, {}, allow_instance_type_conditional=True
        )
    assert not any(op == "execute_change_set" for _, op, _ in api.calls)
    assert api.calls[-1][1:] == ("delete_change_set", {"ChangeSetName": "change-id"})


@pytest.mark.parametrize(
    "changes,state_fields",
    [
        ([], {}),
        ([{}], {}),
        ([{"Type": "Resource"}], {}),
        ([{"Type": "unknown", "ResourceChange": {}}], {}),
        ([resource_change(), resource_change()], {}),
        ([resource_change()], {"NextToken": "unread-private-page"}),
        ([resource_change(Action="Add")], {}),
        ([resource_change(Action="Dynamic")], {}),
        ([resource_change(Replacement="True")], {}),
        ([resource_change(Replacement=None)], {}),
        ([resource_change(PolicyAction="ReplaceAndDelete")], {}),
        ([resource_change(ResourceType="AWS::RDS::DBInstance")], {}),
        ([resource_change(Scope=[])], {}),
        ([resource_change(Scope=["Properties", "UpdateReplacePolicy"])], {}),
        ([resource_change(Details=[])], {}),
        ([resource_change(Details=[{}])], {}),
        ([resource_change(Details=[{"Target": {"Name": "Monitoring"}}])], {}),
        ([resource_change(Details=[target_detail("ImageId")])], {}),
        ([resource_change(Details=[target_detail("Monitoring", recreation="Always")])], {}),
        ([resource_change(Details=[target_detail("Monitoring", recreation="Conditionally")])], {}),
        ([resource_change(Replacement="Conditional")], {}),
    ],
)
def test_incomplete_or_out_of_scope_changes_fail_closed_before_execute(
    template: Path,
    monkeypatch: pytest.MonkeyPatch,
    changes: object,
    state_fields: dict[str, object],
) -> None:
    api = configure_api(monkeypatch, changes, **state_fields)
    with pytest.raises(ValueError, match="^review_workload_update_change_not_allowed$"):
        stacks.apply_stack(
            api, TARGET, "workload", template, {}, allow_instance_type_conditional=True
        )
    assert not any(op == "execute_change_set" for _, op, _ in api.calls)


@pytest.mark.parametrize("allow", [False, True])
def test_conditional_instance_type_requires_separate_existing_instance_validation(
    template: Path, monkeypatch: pytest.MonkeyPatch, allow: bool
) -> None:
    api = configure_api(
        monkeypatch,
        [
            resource_change(
                Replacement="Conditional",
                Details=[
                    target_detail("InstanceType", recreation="Conditionally"),
                    target_detail("Monitoring"),
                    target_detail("CreditSpecification"),
                ],
            )
        ],
    )
    if allow:
        stacks.apply_stack(
            api, TARGET, "workload", template, {}, allow_instance_type_conditional=True
        )
    else:
        with pytest.raises(ValueError, match="^review_workload_update_change_not_allowed$"):
            stacks.apply_stack(api, TARGET, "workload", template, {})
    assert sum(op == "execute_change_set" for _, op, _ in api.calls) == int(allow)


@pytest.mark.parametrize("kind", ["control", "edge", "workload"])
def test_create_and_other_stacks_keep_existing_change_set_behavior(
    template: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: Literal["control", "workload", "edge"],
) -> None:
    is_create = kind == "workload"
    api = configure_api(monkeypatch, [], kind=kind, is_create=is_create)
    assert stacks.apply_stack(api, TARGET, kind, template, {}).status.endswith("COMPLETE")
    assert sum(op == "execute_change_set" for _, op, _ in api.calls) == 1
