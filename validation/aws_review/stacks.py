"""同じ専用stackだけをCloudFormationで作成・更新する。"""

import time
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field

from quiz_app.load_test_aws.aws import ApiModel, AwsApi, EmptyResponse
from quiz_app.load_test_aws.responses import Tag
from validation.aws_review.models import ReviewTarget
from validation.aws_review.resource_progress import StackProgress


class Output(ApiModel):
    key: str = Field(alias="OutputKey")
    value: str = Field(alias="OutputValue")


class StackParameter(ApiModel):
    key: str = Field(alias="ParameterKey")
    value: str = Field(alias="ParameterValue")


class Stack(ApiModel):
    arn: str = Field(alias="StackId")
    name: str = Field(alias="StackName")
    status: str = Field(alias="StackStatus")
    tags: tuple[Tag, ...] = Field(default=(), alias="Tags")
    outputs: tuple[Output, ...] = Field(default=(), alias="Outputs")
    parameters: tuple[StackParameter, ...] = Field(default=(), alias="Parameters")

    def output(self, key: str) -> str:
        matches = [item.value for item in self.outputs if item.key == key]
        if len(matches) != 1:
            raise ValueError("review_stack_output_missing")
        return matches[0]


class Stacks(ApiModel):
    stacks: tuple[Stack, ...] = Field(alias="Stacks")


class ChangeSet(ApiModel):
    arn: str = Field(alias="Id")


class ChangeTarget(ApiModel):
    attribute: str | None = Field(default=None, alias="Attribute")
    name: str | None = Field(default=None, alias="Name")
    recreation: str | None = Field(default=None, alias="RequiresRecreation")


class ChangeDetail(ApiModel):
    target: ChangeTarget | None = Field(default=None, alias="Target")


class ResourceChange(ApiModel):
    action: str | None = Field(default=None, alias="Action")
    logical_id: str | None = Field(default=None, alias="LogicalResourceId")
    resource_type: str | None = Field(default=None, alias="ResourceType")
    replacement: str | None = Field(default=None, alias="Replacement")
    policy_action: str | None = Field(default=None, alias="PolicyAction")
    scope: tuple[str, ...] = Field(default=(), alias="Scope")
    details: tuple[ChangeDetail, ...] = Field(default=(), alias="Details")


class Change(ApiModel):
    kind: str | None = Field(default=None, alias="Type")
    resource: ResourceChange | None = Field(default=None, alias="ResourceChange")


class ChangeState(ApiModel):
    status: str = Field(alias="Status")
    reason: str = Field(default="", alias="StatusReason")
    execution: str = Field(default="UNAVAILABLE", alias="ExecutionStatus")
    changes: tuple[Change, ...] = Field(default=(), alias="Changes")
    next_token: str | None = Field(default=None, alias="NextToken")


_WORKLOAD_UPDATES = {
    ("AppInstance", "AWS::EC2::Instance"): {"InstanceType", "Monitoring", "CreditSpecification"},
    ("AppRole", "AWS::IAM::Role"): {"Policies"},
}


def _is_safe_workload_update(state: ChangeState, *, allow_instance_type_conditional: bool) -> bool:
    # 今回は2資源の既存property更新だけ。続きのpageや不明な差分を省略して実行しない。
    if state.next_token or not 1 <= len(state.changes) <= len(_WORKLOAD_UPDATES):
        return False
    seen: set[str] = set()
    for change in state.changes:
        resource = change.resource
        if change.kind != "Resource" or resource is None or resource.logical_id in seen:
            return False
        allowed = _WORKLOAD_UPDATES.get((resource.logical_id, resource.resource_type))
        if (
            allowed is None
            or resource.action != "Modify"
            or resource.replacement not in {"False", "Conditional"}
            or resource.policy_action is not None
            or resource.scope != ("Properties",)
            or not resource.details
        ):
            return False
        has_conditional_instance_type = False
        for detail in resource.details:
            target = detail.target
            if target is None or target.attribute != "Properties" or target.name not in allowed:
                return False
            if target.recreation == "Conditionally":
                if resource.logical_id != "AppInstance" or target.name != "InstanceType":
                    return False
                has_conditional_instance_type = True
            elif target.recreation != "Never":
                return False
        if resource.replacement == "Conditional" and not (
            allow_instance_type_conditional and has_conditional_instance_type
        ):
            return False
        if resource.logical_id is not None:
            seen.add(resource.logical_id)
    return True


def get_stack(api: AwsApi, target: ReviewTarget, kind: str) -> Stack | None:
    """不存在は一覧で確認し、権限拒否を不存在へ変換しない。"""
    name = f"{target.prefix}-{kind}"
    found = [
        stack
        for page in api.pages("cloudformation", "describe_stacks", Stacks)
        for stack in page.stacks
        if stack.name == name and stack.status != "DELETE_COMPLETE"
    ]
    if not found:
        return None
    if len(found) != 1:
        raise ValueError("review_stack_ambiguous")
    stack = found[0]
    expected = {"DemoId": target.demo_id, "Purpose": "quiz-rehearsal"}
    tags = {tag.key: tag.value for tag in stack.tags}
    prefix = f"arn:aws:cloudformation:{target.region}:{target.account_id}:stack/{name}/"
    if not stack.arn.startswith(prefix) or any(tags.get(k) != v for k, v in expected.items()):
        raise ValueError("review_stack_ownership_mismatch")
    return stack


def apply_stack(
    api: AwsApi,
    target: ReviewTarget,
    kind: Literal["control", "workload", "edge"],
    template: Path,
    parameters: dict[str, str],
    *,
    role_arn: str | None = None,
    allow_instance_type_conditional: bool = False,
    timeout_seconds: int = 2700,
) -> Stack:
    current = get_stack(api, target, kind)
    if current is not None and current.status not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
        raise ValueError("review_stack_not_ready_for_update")
    kwargs: dict[str, object] = {}
    if role_arn is not None:
        expected_role = f"arn:aws:iam::{target.account_id}:role/gs-review-{target.demo_id}-cfn"
        if role_arn != expected_role:
            raise ValueError("review_cloudformation_role_mismatch")
        kwargs["RoleARN"] = role_arn
    change = api.call(
        "cloudformation",
        "create_change_set",
        ChangeSet,
        StackName=current.arn if current else f"{target.prefix}-{kind}",
        ChangeSetName=f"review-{uuid4().hex}",
        ChangeSetType="UPDATE" if current else "CREATE",
        TemplateBody=template.read_text(),
        Parameters=[
            {"ParameterKey": key, "ParameterValue": value} for key, value in parameters.items()
        ],
        Tags=[
            {"Key": "DemoId", "Value": target.demo_id},
            {"Key": "Purpose", "Value": "quiz-rehearsal"},
        ],
        Capabilities=["CAPABILITY_NAMED_IAM"],
        **kwargs,
    )
    deadline = time.monotonic() + timeout_seconds
    reporter = StackProgress(kind)
    reporter.report("WAITING", step="change_set")
    while time.monotonic() < deadline:
        state = api.call(
            "cloudformation", "describe_change_set", ChangeState, ChangeSetName=change.arn
        )
        if state.status == "FAILED":
            api.call("cloudformation", "delete_change_set", EmptyResponse, ChangeSetName=change.arn)
            if current and (
                "didn't contain changes" in state.reason
                or "No updates are to be performed" in state.reason
            ):
                reporter.report("NO_CHANGES", step="change_set")
                return current
            reporter.report(state.status, step="change_set")
            raise ValueError("review_change_set_failed")
        reporter.report(state.status, step="change_set")
        if state.status == "CREATE_COMPLETE":
            break
        time.sleep(10)
    else:
        raise TimeoutError("review_change_set_timeout")
    if (
        current is not None
        and kind == "workload"
        and not _is_safe_workload_update(
            state, allow_instance_type_conditional=allow_instance_type_conditional
        )
    ):
        api.call("cloudformation", "delete_change_set", EmptyResponse, ChangeSetName=change.arn)
        raise ValueError("review_workload_update_change_not_allowed")
    api.call("cloudformation", "execute_change_set", EmptyResponse, ChangeSetName=change.arn)
    while time.monotonic() < deadline:
        state = api.call(
            "cloudformation", "describe_change_set", ChangeState, ChangeSetName=change.arn
        )
        observed = get_stack(api, target, kind)
        if observed is not None:
            reporter.report(observed.status)
        if (
            observed
            and state.execution == "EXECUTE_COMPLETE"
            and observed.status in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
        ):
            return observed
        if (
            state.execution == "EXECUTE_FAILED"
            or observed is None
            or "FAILED" in observed.status
            or "ROLLBACK" in observed.status
        ):
            raise ValueError("review_stack_deployment_failed")
        reporter.resources(api, observed.arn)
        time.sleep(15)
    raise TimeoutError("review_stack_deployment_timeout")
