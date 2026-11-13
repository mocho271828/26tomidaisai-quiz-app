from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from quiz_app.load_test_aws.aws import ApiModel


class Tag(ApiModel):
    key: str = Field(alias="Key")
    value: str = Field(alias="Value")


class Output(ApiModel):
    key: str = Field(alias="OutputKey")
    value: str = Field(alias="OutputValue")


class Stack(ApiModel):
    stack_id: str = Field(alias="StackId")
    name: str = Field(alias="StackName")
    status: str = Field(alias="StackStatus")
    tags: tuple[Tag, ...] = Field(default=(), alias="Tags")
    outputs: tuple[Output, ...] = Field(default=(), alias="Outputs")


class Stacks(ApiModel):
    stacks: tuple[Stack, ...] = Field(alias="Stacks")


class StackSummary(ApiModel):
    stack_id: str = Field(alias="StackId")
    name: str = Field(alias="StackName")
    status: str = Field(alias="StackStatus")


class StackSummaries(ApiModel):
    stacks: tuple[StackSummary, ...] = Field(alias="StackSummaries")


class StackCreated(ApiModel):
    stack_id: str = Field(alias="StackId")


class StackResource(ApiModel):
    physical_id: str | None = Field(default=None, alias="PhysicalResourceId")
    kind: str = Field(alias="ResourceType")
    status: str = Field(alias="ResourceStatus")


class StackResources(ApiModel):
    resources: tuple[StackResource, ...] = Field(alias="StackResourceSummaries")


class ResourceTags(ApiModel):
    tags: tuple[Tag, ...] = Field(default=(), alias="TagList")


class Ec2State(ApiModel):
    name: str = Field(alias="Name")


class InstanceEbs(ApiModel):
    volume_id: str = Field(alias="VolumeId")


class InstanceBlockDevice(ApiModel):
    ebs: InstanceEbs | None = Field(default=None, alias="Ebs")


class Instance(ApiModel):
    identifier: str = Field(alias="InstanceId")
    state: Ec2State = Field(alias="State")
    tags: tuple[Tag, ...] = Field(default=(), alias="Tags")
    block_devices: tuple[InstanceBlockDevice, ...] = Field(default=(), alias="BlockDeviceMappings")


class Reservation(ApiModel):
    instances: tuple[Instance, ...] = Field(alias="Instances")


class Instances(ApiModel):
    reservations: tuple[Reservation, ...] = Field(alias="Reservations")


class Volume(ApiModel):
    identifier: str = Field(alias="VolumeId")
    state: str = Field(alias="State")
    tags: tuple[Tag, ...] = Field(default=(), alias="Tags")


class Volumes(ApiModel):
    volumes: tuple[Volume, ...] = Field(alias="Volumes")


class Snapshot(ApiModel):
    identifier: str = Field(alias="SnapshotId")
    state: str = Field(alias="State")
    tags: tuple[Tag, ...] = Field(default=(), alias="Tags")


class Snapshots(ApiModel):
    snapshots: tuple[Snapshot, ...] = Field(alias="Snapshots")


class Address(ApiModel):
    identifier: str = Field(alias="AllocationId")
    association_id: str | None = Field(default=None, alias="AssociationId")
    tags: tuple[Tag, ...] = Field(default=(), alias="Tags")


class Addresses(ApiModel):
    addresses: tuple[Address, ...] = Field(alias="Addresses")


class Database(ApiModel):
    identifier: str = Field(alias="DBInstanceIdentifier")
    arn: str = Field(alias="DBInstanceArn")
    state: str = Field(alias="DBInstanceStatus")
    tags: tuple[Tag, ...] = Field(default=(), alias="TagList")


class Databases(ApiModel):
    databases: tuple[Database, ...] = Field(alias="DBInstances")


class DbSnapshot(ApiModel):
    db_identifier: str | None = Field(default=None, alias="DBInstanceIdentifier")
    identifier: str = Field(alias="DBSnapshotIdentifier")
    arn: str = Field(alias="DBSnapshotArn")
    state: str = Field(alias="Status")


class DbSnapshots(ApiModel):
    snapshots: tuple[DbSnapshot, ...] = Field(alias="DBSnapshots")


class AutomatedBackup(ApiModel):
    tags: tuple[Tag, ...] = Field(default=(), alias="TagList")
    identifier: str = Field(alias="DbiResourceId")
    db_identifier: str = Field(alias="DBInstanceIdentifier")
    state: str = Field(alias="Status")


class AutomatedBackups(ApiModel):
    backups: tuple[AutomatedBackup, ...] = Field(alias="DBInstanceAutomatedBackups")


class RedisGroup(ApiModel):
    identifier: str = Field(alias="ReplicationGroupId")
    arn: str = Field(alias="ARN")
    state: str = Field(alias="Status")


class RedisGroups(ApiModel):
    groups: tuple[RedisGroup, ...] = Field(alias="ReplicationGroups")


class RedisSnapshot(ApiModel):
    group_id: str | None = Field(default=None, alias="ReplicationGroupId")
    identifier: str = Field(alias="SnapshotName")
    arn: str = Field(alias="ARN")
    state: str = Field(alias="SnapshotStatus")


class RedisSnapshots(ApiModel):
    snapshots: tuple[RedisSnapshot, ...] = Field(alias="Snapshots")


class TaggedResource(ApiModel):
    arn: str = Field(alias="ResourceARN")
    tags: tuple[Tag, ...] = Field(default=(), alias="Tags")


class TaggedResources(ApiModel):
    resources: tuple[TaggedResource, ...] = Field(alias="ResourceTagMappingList")


class ObjectVersion(ApiModel):
    key: str = Field(alias="Key")
    version: str = Field(alias="VersionId")


class ObjectVersions(ApiModel):
    versions: tuple[ObjectVersion, ...] = Field(default=(), alias="Versions")
    markers: tuple[ObjectVersion, ...] = Field(default=(), alias="DeleteMarkers")


class Multipart(ApiModel):
    key: str = Field(alias="Key")
    upload_id: str = Field(alias="UploadId")


class Multiparts(ApiModel):
    uploads: tuple[Multipart, ...] = Field(default=(), alias="Uploads")


class Subscription(ApiModel):
    arn: str = Field(alias="SubscriptionArn")
    protocol: str = Field(alias="Protocol")
    endpoint: str = Field(alias="Endpoint")


class Subscriptions(ApiModel):
    subscriptions: tuple[Subscription, ...] = Field(alias="Subscriptions")


class Execution(ApiModel):
    status: Literal["RUNNING", "SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED", "PENDING_REDRIVE"] = (
        "RUNNING"
    )
    arn: str = Field(alias="executionArn")
    started_at: datetime = Field(alias="startDate")


class Command(ApiModel):
    identifier: str = Field(alias="CommandId")


class CommandResponse(ApiModel):
    command: Command = Field(alias="Command")


class Function(ApiModel):
    arn: str = Field(alias="FunctionArn")
    state: str = Field(default="Pending", alias="State")
