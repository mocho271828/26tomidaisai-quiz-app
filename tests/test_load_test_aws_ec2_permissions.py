from __future__ import annotations

import json
from fnmatch import fnmatchcase

import pytest
from test_load_test_aws import plan

from quiz_app.load_test_aws.bootstrap import PolicyDocument, Statement, execution_policy_documents
from quiz_app.load_test_aws.templates import template_body

CHILD_RESOURCES = (
    ("CreateRouteTable", "route-table"),
    ("CreateSecurityGroup", "security-group"),
    ("CreateSubnet", "subnet"),
)


def _arn(resource: str) -> str:
    configuration = plan()
    return f"arn:aws:ec2:{configuration.region}:{configuration.account_id}:{resource}"


def _tags(prefix: str, *, run_id: str | None = None) -> dict[str, str]:
    return {
        f"{prefix}/Project": "gs-app-tomidaisai",
        f"{prefix}/RunId": str(plan().run_id) if run_id is None else run_id,
    }


def _statements(action: str, resource: str) -> tuple[Statement, ...]:
    return tuple(
        statement
        for body in execution_policy_documents(plan())
        for statement in PolicyDocument.model_validate_json(body).statements
        if f"ec2:{action}" in statement.action
        and any(
            fnmatchcase(resource, pattern)
            for pattern in (
                (statement.resource,) if isinstance(statement.resource, str) else statement.resource
            )
        )
    )


def _allows(action: str, resource: str, context: dict[str, str]) -> bool:
    # 今回のStringEquals/Allow文だけを評価し、IAM全体の模擬実装にはしない。
    for statement in _statements(action, resource):
        assert statement.effect == "Allow"
        if statement.condition is None:
            return True
        assert set(statement.condition) == {"StringEquals"}
        if all(
            context.get(key) in ((value,) if isinstance(value, str) else value)
            for key, value in statement.condition["StringEquals"].items()
        ):
            return True
    return False


@pytest.mark.parametrize(("action", "kind"), CHILD_RESOURCES)
def test_network_creation_checks_parent_ownership_separately_from_new_tags(
    action: str, kind: str
) -> None:
    parent = _arn("vpc/vpc-parent")
    child = _arn(f"{kind}/new-resource")
    request = _tags("aws:RequestTag")
    owned = _tags("ec2:ResourceTag")
    assert _allows(action, parent, owned)
    assert not _allows(action, parent, request)
    assert not _allows(action, parent, {**request, **_tags("ec2:ResourceTag", run_id="other")})
    assert not _allows(action, parent, {**owned, "ec2:ResourceTag/Project": "other"})
    assert _allows(action, child, request)
    assert not _allows(action, child, owned)
    assert not _allows(action, child, _tags("aws:RequestTag", run_id="other"))
    assert not _allows(action, child, {})
    assert not _allows(action, parent.replace(plan().account_id, "999999999999"), owned)


@pytest.mark.parametrize(
    ("action", "kind"), [("CreateVpc", "vpc"), ("CreateInternetGateway", "internet-gateway")]
)
def test_root_network_creation_requires_run_request_tags(action: str, kind: str) -> None:
    resource = _arn(f"{kind}/new-resource")
    assert _allows(action, resource, _tags("aws:RequestTag"))
    assert not _allows(action, resource, _tags("ec2:ResourceTag"))
    assert not _allows(action, resource, {})


@pytest.mark.parametrize("kind", ["vpc", "subnet", "security-group", "route-table", "instance"])
def test_create_tags_cannot_convert_other_run_or_untagged_resources_to_owned(kind: str) -> None:
    resource = _arn(f"{kind}/existing-resource")
    request = _tags("aws:RequestTag")
    assert not _allows("CreateTags", resource, request)
    assert not _allows(
        "CreateTags", resource, {**request, **_tags("ec2:ResourceTag", run_id="other")}
    )
    assert _allows("CreateTags", resource, {**request, **_tags("ec2:ResourceTag")})
    assert not _allows(
        "CreateTags",
        resource,
        {**_tags("aws:RequestTag", run_id="other"), **_tags("ec2:ResourceTag")},
    )


@pytest.mark.parametrize(
    ("action", "kind"),
    [
        *CHILD_RESOURCES,
        ("CreateVpc", "vpc"),
        ("CreateInternetGateway", "internet-gateway"),
        ("RunInstances", "instance"),
        ("RunInstances", "volume"),
    ],
)
def test_atomic_creation_tags_require_the_supported_create_action(action: str, kind: str) -> None:
    context = {**_tags("aws:RequestTag"), "ec2:CreateAction": action}
    assert _allows("CreateTags", _arn(f"{kind}/new-resource"), context)
    assert not _allows(
        "CreateTags", _arn(f"{kind}/new-resource"), {**context, "ec2:CreateAction": "CreateFleet"}
    )


@pytest.mark.parametrize("kind", ["instance", "volume", "subnet", "security-group"])
def test_run_instances_uses_request_tags_only_for_new_compute(kind: str) -> None:
    tag_kind = "aws:RequestTag" if kind in {"instance", "volume"} else "ec2:ResourceTag"
    other_kind = "ec2:ResourceTag" if tag_kind == "aws:RequestTag" else "aws:RequestTag"
    assert _allows("RunInstances", _arn(f"{kind}/resource"), _tags(tag_kind))
    assert not _allows("RunInstances", _arn(f"{kind}/resource"), _tags(other_kind))
    assert not _allows("RunInstances", _arn(f"{kind}/resource"), _tags(tag_kind, run_id="other"))


def test_run_instances_keeps_pinned_image_and_new_interface_separate_from_existing_tags() -> None:
    configuration = plan()
    image = f"arn:aws:ec2:{configuration.region}::image/{configuration.ami_id}"
    assert _allows("RunInstances", image, {})
    assert not _allows("RunInstances", image.replace(configuration.ami_id, "ami-unapproved"), {})
    assert _allows("RunInstances", _arn("network-interface/new-interface"), {})
    resources = json.loads(template_body("workload"))["Resources"]
    for logical_id in ("AppInstance", "RunnerInstance"):
        properties = resources[logical_id]["Properties"]
        assert properties["PropagateTagsToVolumeOnCreation"] is True
        assert properties["SubnetId"] == {"Ref": "PublicSubnet"}
        assert properties["SecurityGroupIds"]
        assert "NetworkInterfaces" not in properties
        assert "KeyName" not in properties
        assert "LaunchTemplate" not in properties


@pytest.mark.parametrize("direction", ["Ingress", "Egress"])
def test_inline_security_group_rules_require_owned_parent_without_creation_tags(
    direction: str,
) -> None:
    action = f"AuthorizeSecurityGroup{direction}"
    assert _allows(action, _arn("security-group/sg-owned"), _tags("ec2:ResourceTag"))
    assert not _allows(action, _arn("security-group/sg-other"), _tags("aws:RequestTag"))
    assert not _allows(
        action, _arn("security-group/sg-other"), _tags("ec2:ResourceTag", run_id="other")
    )
    # inline ruleにTagSpecificationsは無く、作成rule ARNの権限評価を追加しない。
    resources = json.loads(template_body("workload"))["Resources"]
    groups = [item for item in resources.values() if item["Type"] == "AWS::EC2::SecurityGroup"]
    assert groups
    for group in groups:
        for rule in group["Properties"].get(f"SecurityGroup{direction}", []):
            assert "Tags" not in rule
            assert "TagSpecifications" not in rule
    assert not any(
        item["Type"] == f"AWS::EC2::SecurityGroup{direction}" for item in resources.values()
    )
