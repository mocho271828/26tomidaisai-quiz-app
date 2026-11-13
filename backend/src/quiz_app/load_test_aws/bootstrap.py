from __future__ import annotations

import json
from string import Template

from pydantic import Field

from quiz_app.load_test_aws.aws import ApiModel, AwsApi, AwsCallError, EmptyResponse
from quiz_app.load_test_aws.models import AwsRunPlan
from quiz_app.load_test_aws.responses import Tag
from quiz_app.load_test_aws.templates import tags, template_root


class Role(ApiModel):
    arn: str = Field(alias="Arn")
    tags: tuple[Tag, ...] = Field(default=(), alias="Tags")


class RoleResponse(ApiModel):
    role: Role = Field(alias="Role")


class Policy(ApiModel):
    arn: str = Field(alias="Arn")
    default_version: str = Field(alias="DefaultVersionId")


class PolicyResponse(ApiModel):
    policy: Policy = Field(alias="Policy")


class Statement(ApiModel):
    sid: str = Field(alias="Sid")
    effect: str = Field(alias="Effect")
    action: tuple[str, ...] = Field(alias="Action")
    resource: str | tuple[str, ...] = Field(alias="Resource")
    condition: dict[str, dict[str, str | tuple[str, ...]]] | None = Field(
        default=None, alias="Condition"
    )


class PolicyDocument(ApiModel):
    version: str = Field(alias="Version")
    statements: tuple[Statement, ...] = Field(alias="Statement")


class PolicyVersion(ApiModel):
    document: PolicyDocument = Field(alias="Document")


class PolicyVersionResponse(ApiModel):
    version: PolicyVersion = Field(alias="PolicyVersion")


def execution_policy_documents(plan: AwsRunPlan) -> tuple[str, ...]:
    """公開CFN resource schemaのhandler権限をrun固有ARNへ束縛する。"""
    source = (template_root() / "cfn-execution-policy.json").read_text(encoding="utf-8")
    document = PolicyDocument.model_validate_json(
        Template(source).substitute(
            Region=plan.region,
            Account=plan.account_id,
            Prefix=plan.prefix,
            RunId=str(plan.run_id),
            AmiId=plan.ami_id,
        )
    )
    result: list[str] = []
    batch: list[Statement] = []
    for statement in document.statements:
        candidate = PolicyDocument(Version=document.version, Statement=(*batch, statement))
        body = candidate.model_dump_json(by_alias=True, exclude_none=True)
        if len(body) > 6000:
            if not batch:
                raise ValueError("aws_load_test_policy_statement_too_large")
            result.append(
                PolicyDocument(Version=document.version, Statement=tuple(batch)).model_dump_json(
                    by_alias=True, exclude_none=True
                )
            )
            batch = [statement]
        else:
            batch.append(statement)
    if batch:
        result.append(
            PolicyDocument(Version=document.version, Statement=tuple(batch)).model_dump_json(
                by_alias=True, exclude_none=True
            )
        )
    return tuple(result)


def ensure_execution_role(api: AwsApi, plan: AwsRunPlan) -> bool:
    """既存roleを上書きせず、初回だけrun専用service roleを作成する。"""
    if plan.cloudformation_role_arn is not None:
        return False
    name = f"{plan.prefix}-cloudformation"
    is_created = False
    try:
        observed = api.call("iam", "get_role", RoleResponse, RoleName=name)
        ownership = {tag.key: tag.value for tag in observed.role.tags}
        if (
            observed.role.arn != plan.execution_role_arn
            or ownership.get("RunId") != str(plan.run_id)
            or ownership.get("Project") != "gs-app-tomidaisai"
        ):
            raise ValueError("aws_load_test_execution_role_ownership_mismatch")
    except AwsCallError as error:
        if error.code != "NoSuchEntity":
            raise
        # Stack用service roleの公式trust契約。extension/StackSets用SourceArn条件は流用しない。
        api.call(
            "iam",
            "create_role",
            RoleResponse,
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "cloudformation.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                }
            ),
            Tags=tags(plan),
            Description="Disposable AWS load test CloudFormation service role",
        )
        is_created = True
    for index, body in enumerate(execution_policy_documents(plan)):
        policy_name = f"{plan.prefix}-cfn-{index}"
        arn = f"arn:aws:iam::{plan.account_id}:policy/{policy_name}"
        try:
            existing = api.call("iam", "get_policy", PolicyResponse, PolicyArn=arn)
            version = api.call(
                "iam",
                "get_policy_version",
                PolicyVersionResponse,
                PolicyArn=arn,
                VersionId=existing.policy.default_version,
            )
            if version.version.document != PolicyDocument.model_validate_json(body):
                raise ValueError("aws_load_test_execution_policy_mismatch")
        except AwsCallError as error:
            if error.code != "NoSuchEntity":
                raise
            api.call(
                "iam",
                "create_policy",
                PolicyResponse,
                PolicyName=policy_name,
                PolicyDocument=body,
                Tags=tags(plan),
            )
            is_created = True
        api.call("iam", "attach_role_policy", EmptyResponse, RoleName=name, PolicyArn=arn)
    return is_created
