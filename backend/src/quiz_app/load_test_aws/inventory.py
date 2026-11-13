from __future__ import annotations

from pydantic import Field

from quiz_app.load_test_aws.aws import ApiModel, AwsApi, AwsCallError, EmptyResponse
from quiz_app.load_test_aws.identity_scan import scan_changed_identity
from quiz_app.load_test_aws.models import PROJECT, CleanupInput, Residual
from quiz_app.load_test_aws.networks import scan_networks
from quiz_app.load_test_aws.ownership import RunLedger, owned
from quiz_app.load_test_aws.responses import (
    Addresses,
    AutomatedBackups,
    Databases,
    DbSnapshots,
    Instances,
    RedisGroups,
    RedisSnapshots,
    ResourceTags,
    Snapshots,
    Stack,
    StackResources,
    Stacks,
    StackSummaries,
    Tag,
    Volumes,
)
from quiz_app.load_test_aws.storage import (
    empty_bucket,
    scan_storage,
)

EMPTY_LEDGER = RunLedger()


class Ami(ApiModel):
    identifier: str = Field(alias="ImageId")
    state: str = Field(alias="State")
    tags: tuple[Tag, ...] = Field(default=(), alias="Tags")


class Amis(ApiModel):
    images: tuple[Ami, ...] = Field(alias="Images")


def find_stack(api: AwsApi, name: str) -> Stack | None:
    """不存在だけを空にし、AccessDeniedや不正な応答を成功へ変換しない。"""
    matches = []
    for page in api.pages("cloudformation", "list_stacks", StackSummaries):
        matches.extend(item for item in page.stacks if item.name == name)
    active = [item for item in matches if item.status != "DELETE_COMPLETE"]
    if len(active) > 1:
        raise ValueError("aws_load_test_stack_identity_ambiguous")
    if not active:
        return None
    result = api.call("cloudformation", "describe_stacks", Stacks, StackName=active[0].stack_id)
    if len(result.stacks) != 1 or result.stacks[0].name != name:
        raise ValueError("aws_load_test_stack_identity_invalid")
    return result.stacks[0]


def prepare_stack_deletion(api: AwsApi, run: CleanupInput, stack: Stack) -> None:
    if not owned(stack.tags, run):
        raise ValueError("aws_load_test_stack_ownership_mismatch")
    for page in api.pages(
        "cloudformation", "list_stack_resources", StackResources, StackName=stack.stack_id
    ):
        for resource in page.resources:
            if (
                resource.kind == "AWS::S3::Bucket"
                and resource.physical_id
                and resource.status != "DELETE_COMPLETE"
            ):
                try:
                    empty_bucket(api, resource.physical_id)
                except AwsCallError as error:
                    if error.code != "NoSuchBucket":
                        raise


def scan_ec2(
    api: AwsApi, run: CleanupInput, *, delete: bool, ledger: RunLedger = EMPTY_LEDGER
) -> list[Residual]:
    result: list[Residual] = []
    filters = [
        {"Name": "tag:RunId", "Values": [str(run.run_id)]},
        {"Name": "tag:Project", "Values": [PROJECT]},
    ]
    for page in api.pages("ec2", "describe_instances", Instances, Filters=filters):
        for reservation in page.reservations:
            for item in reservation.instances:
                if item.state.name == "terminated":
                    continue
                result.append(
                    Residual(kind="ec2-instance", identifier=item.identifier, state=item.state.name)
                )
                if (
                    delete
                    and item.state.name != "shutting-down"
                    and ledger.permits("AWS::EC2::Instance", item.identifier, item.tags, run)
                ):
                    api.call(
                        "ec2", "terminate_instances", EmptyResponse, InstanceIds=[item.identifier]
                    )
    for page in api.pages("ec2", "describe_volumes", Volumes, Filters=filters):
        for item in page.volumes:
            result.append(Residual(kind="ebs-volume", identifier=item.identifier, state=item.state))
            if (
                delete
                and item.state == "available"
                and ledger.permits("AWS::EC2::Volume", item.identifier, item.tags, run)
            ):
                api.call("ec2", "delete_volume", EmptyResponse, VolumeId=item.identifier)
    for page in api.pages("ec2", "describe_images", Amis, Owners=[run.account_id], Filters=filters):
        for item in page.images:
            if item.state == "deregistered":
                continue
            result.append(Residual(kind="ami", identifier=item.identifier, state=item.state))
            if delete and ledger.permits("AWS::EC2::Image", item.identifier, item.tags, run):
                api.call("ec2", "deregister_image", EmptyResponse, ImageId=item.identifier)
    for page in api.pages(
        "ec2", "describe_snapshots", Snapshots, OwnerIds=[run.account_id], Filters=filters
    ):
        for item in page.snapshots:
            result.append(
                Residual(kind="ebs-snapshot", identifier=item.identifier, state=item.state)
            )
            if (
                delete
                and item.state == "completed"
                and ledger.permits("AWS::EC2::Snapshot", item.identifier, item.tags, run)
            ):
                api.call("ec2", "delete_snapshot", EmptyResponse, SnapshotId=item.identifier)
    addresses = api.call("ec2", "describe_addresses", Addresses, Filters=filters)
    for item in addresses.addresses:
        result.append(Residual(kind="elastic-ip", identifier=item.identifier, state="allocated"))
        if delete and ledger.permits("AWS::EC2::EIP", item.identifier, item.tags, run):
            if item.association_id is not None:
                api.call(
                    "ec2", "disassociate_address", EmptyResponse, AssociationId=item.association_id
                )
            api.call("ec2", "release_address", EmptyResponse, AllocationId=item.identifier)
    return result


def scan_databases(
    api: AwsApi, run: CleanupInput, *, delete: bool, ledger: RunLedger = EMPTY_LEDGER
) -> list[Residual]:
    result: list[Residual] = []
    for page in api.pages("rds", "describe_db_instances", Databases):
        for item in page.databases:
            if not owned(item.tags, run):
                if item.identifier in ledger.identifiers("AWS::RDS::DBInstance"):
                    result.append(
                        Residual(
                            kind="rds-instance",
                            identifier=item.identifier,
                            state="ownership-unverified",
                        )
                    )
                continue
            result.append(
                Residual(kind="rds-instance", identifier=item.identifier, state=item.state)
            )
            if (
                delete
                and ledger.permits("AWS::RDS::DBInstance", item.identifier, item.tags, run)
                and item.state not in {"creating", "deleting", "backing-up", "modifying"}
            ):
                api.call(
                    "rds",
                    "delete_db_instance",
                    EmptyResponse,
                    DBInstanceIdentifier=item.identifier,
                    SkipFinalSnapshot=True,
                    DeleteAutomatedBackups=True,
                )
    for page in api.pages("rds", "describe_db_snapshots", DbSnapshots, SnapshotType="manual"):
        for item in page.snapshots:
            tags = api.call("rds", "list_tags_for_resource", ResourceTags, ResourceName=item.arn)
            if not owned(tags.tags, run):
                if item.db_identifier in ledger.identifiers("AWS::RDS::DBInstance"):
                    result.append(
                        Residual(
                            kind="rds-snapshot",
                            identifier=item.identifier,
                            state="ownership-unverified",
                        )
                    )
                continue
            result.append(
                Residual(kind="rds-snapshot", identifier=item.identifier, state=item.state)
            )
            if (
                delete
                and item.state == "available"
                and item.db_identifier in ledger.identifiers("AWS::RDS::DBInstance")
            ):
                api.call(
                    "rds", "delete_db_snapshot", EmptyResponse, DBSnapshotIdentifier=item.identifier
                )
    for page in api.pages("rds", "describe_db_instance_automated_backups", AutomatedBackups):
        for item in page.backups:
            # DB名による親子関係と保持backup自身のtagを併用する。
            if item.db_identifier != f"{run.prefix}-postgres":
                continue
            result.append(
                Residual(kind="rds-automated-backup", identifier=item.identifier, state=item.state)
            )
            if (
                delete
                and item.state == "retained"
                and item.db_identifier in ledger.identifiers("AWS::RDS::DBInstance")
            ):
                if not owned(item.tags, run):
                    raise ValueError("aws_load_test_backup_ownership_mismatch")
                api.call(
                    "rds",
                    "delete_db_instance_automated_backup",
                    EmptyResponse,
                    DbiResourceId=item.identifier,
                )
    for page in api.pages("elasticache", "describe_replication_groups", RedisGroups):
        for item in page.groups:
            tags = api.call(
                "elasticache", "list_tags_for_resource", ResourceTags, ResourceName=item.arn
            )
            if not owned(tags.tags, run):
                if item.identifier in ledger.identifiers("AWS::ElastiCache::ReplicationGroup"):
                    result.append(
                        Residual(
                            kind="redis-group",
                            identifier=item.identifier,
                            state="ownership-unverified",
                        )
                    )
                continue
            result.append(
                Residual(kind="redis-group", identifier=item.identifier, state=item.state)
            )
            if (
                delete
                and item.state == "available"
                and ledger.permits(
                    "AWS::ElastiCache::ReplicationGroup", item.identifier, tags.tags, run
                )
            ):
                api.call(
                    "elasticache",
                    "delete_replication_group",
                    EmptyResponse,
                    ReplicationGroupId=item.identifier,
                    RetainPrimaryCluster=False,
                )
    for page in api.pages(
        "elasticache", "describe_snapshots", RedisSnapshots, SnapshotSource="user"
    ):
        for item in page.snapshots:
            tags = api.call(
                "elasticache", "list_tags_for_resource", ResourceTags, ResourceName=item.arn
            )
            if not owned(tags.tags, run):
                if item.group_id in ledger.identifiers("AWS::ElastiCache::ReplicationGroup"):
                    result.append(
                        Residual(
                            kind="redis-snapshot",
                            identifier=item.identifier,
                            state="ownership-unverified",
                        )
                    )
                continue
            result.append(
                Residual(kind="redis-snapshot", identifier=item.identifier, state=item.state)
            )
            if (
                delete
                and item.state == "available"
                and item.group_id in ledger.identifiers("AWS::ElastiCache::ReplicationGroup")
            ):
                api.call(
                    "elasticache", "delete_snapshot", EmptyResponse, SnapshotName=item.identifier
                )
    return result


def scan_residuals(
    api: AwsApi, run: CleanupInput, *, delete: bool, ledger: RunLedger
) -> tuple[Residual, ...]:
    resources = scan_ec2(api, run, delete=delete, ledger=ledger)
    resources.extend(scan_changed_identity(api, run, ledger))
    resources.extend(scan_databases(api, run, delete=delete, ledger=ledger))
    resources.extend(scan_storage(api, run, delete=delete, ledger=ledger))
    resources.extend(scan_networks(api, run, ledger, delete=delete))
    return tuple(resources)
