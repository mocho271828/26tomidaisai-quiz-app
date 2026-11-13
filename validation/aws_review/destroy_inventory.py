from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from quiz_app.load_test_aws.aws import ApiModel, AwsApi
from quiz_app.load_test_aws.ownership import ResourceIdentity
from quiz_app.load_test_aws.responses import (
    Instances,
    Stack,
    StackResources,
    Stacks,
    StackSummaries,
    Tag,
    Volumes,
)
from validation.aws_review.models import ReviewTarget


class DestroyLedger(BaseModel):
    """削除済みの試行も含むCFNの所有台帳。秘密値は保持しない。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    stacks: tuple[Stack, ...] = ()
    resources: tuple[ResourceIdentity, ...] = ()

    def identifiers(self, kind: str) -> frozenset[str]:
        return frozenset(item.identifier for item in self.resources if item.kind == kind)


class MasterSecret(ApiModel):
    arn: str = Field(alias="SecretArn")


class DatabaseIdentity(ApiModel):
    identifier: str = Field(alias="DBInstanceIdentifier")
    resource_id: str = Field(alias="DbiResourceId")
    master_secret: MasterSecret | None = Field(default=None, alias="MasterUserSecret")


class DatabaseIdentities(ApiModel):
    items: tuple[DatabaseIdentity, ...] = Field(alias="DBInstances")


def owned(tags: tuple[Tag, ...], target: ReviewTarget) -> bool:
    values = {tag.key: tag.value for tag in tags}
    return values.get("DemoId") == target.demo_id and values.get("Purpose") == "quiz-rehearsal"


def require_stack(stack: Stack, target: ReviewTarget) -> None:
    allowed = {f"{target.prefix}-{kind}" for kind in ("edge", "workload", "control")}
    arn_prefix = f"arn:aws:cloudformation:ap-northeast-1:{target.account_id}:stack/"
    if (
        stack.name not in allowed
        or not stack.stack_id.startswith(f"{arn_prefix}{stack.name}/")
        or not owned(stack.tags, target)
    ):
        raise ValueError("aws_review_destroy_stack_ownership_mismatch")


def read_ledger(api: AwsApi, target: ReviewTarget) -> DestroyLedger:
    """名前は発見にだけ使い、ARN・所有タグ・CFN台帳を確認してから削除対象にする。"""
    stacks: list[Stack] = []
    resources: list[ResourceIdentity] = []
    names = {f"{target.prefix}-{kind}" for kind in ("edge", "workload", "control")}
    for page in api.pages("cloudformation", "list_stacks", StackSummaries):
        for summary in page.stacks:
            if summary.name not in names:
                continue
            observed = api.call(
                "cloudformation", "describe_stacks", Stacks, StackName=summary.stack_id
            )
            if len(observed.stacks) != 1 or observed.stacks[0].stack_id != summary.stack_id:
                raise ValueError("aws_review_destroy_stack_identity_mismatch")
            stack = observed.stacks[0]
            require_stack(stack, target)
            stacks.append(stack)
            for resource_page in api.pages(
                "cloudformation", "list_stack_resources", StackResources, StackName=stack.stack_id
            ):
                resources.extend(
                    ResourceIdentity(kind=item.kind, identifier=item.physical_id)
                    for item in resource_page.resources
                    if item.physical_id
                )
            resources.extend(
                ResourceIdentity(kind="AWS::SecretsManager::Secret", identifier=output.value)
                for output in stack.outputs
                if output.key == "DatabaseMasterSecretArn"
            )
    ledger = DestroyLedger(stacks=tuple(stacks), resources=tuple(resources))
    instance_ids = ledger.identifiers("AWS::EC2::Instance")
    if instance_ids:
        for page in api.pages(
            "ec2",
            "describe_instances",
            Instances,
            Filters=[{"Name": "instance-id", "Values": sorted(instance_ids)}],
        ):
            for reservation in page.reservations:
                for instance in reservation.instances:
                    if instance.identifier not in instance_ids:
                        raise ValueError("aws_review_destroy_instance_identity_mismatch")
                    if instance.state.name != "terminated" and not owned(instance.tags, target):
                        raise ValueError("aws_review_destroy_instance_ownership_mismatch")
                    resources.extend(
                        ResourceIdentity(kind="AWS::EC2::Volume", identifier=device.ebs.volume_id)
                        for device in instance.block_devices
                        if device.ebs is not None
                    )
    # EC2が既に消えた再実行でも、AWS予約tagから間接作成されたEBSを回収する。
    if stacks:
        for page in api.pages(
            "ec2",
            "describe_volumes",
            Volumes,
            Filters=[
                {
                    "Name": "tag:aws:cloudformation:stack-id",
                    "Values": [stack.stack_id for stack in stacks],
                }
            ],
        ):
            resources.extend(
                ResourceIdentity(kind="AWS::EC2::Volume", identifier=item.identifier)
                for item in page.volumes
            )
    database_ids = ledger.identifiers("AWS::RDS::DBInstance")
    if database_ids:
        for page in api.pages("rds", "describe_db_instances", DatabaseIdentities):
            for database in page.items:
                if database.identifier not in database_ids:
                    continue
                resources.append(
                    ResourceIdentity(kind="RdsResourceId", identifier=database.resource_id)
                )
                if database.master_secret is not None:
                    resources.append(
                        ResourceIdentity(
                            kind="AWS::SecretsManager::Secret",
                            identifier=database.master_secret.arn,
                        )
                    )
    return DestroyLedger(stacks=tuple(stacks), resources=tuple(resources))
