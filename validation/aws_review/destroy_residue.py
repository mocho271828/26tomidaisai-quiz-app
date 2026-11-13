from __future__ import annotations

from pydantic import Field

from quiz_app.load_test_aws.aws import ApiModel, AwsApi, EmptyResponse
from quiz_app.load_test_aws.responses import (
    AutomatedBackups,
    DbSnapshots,
    RedisSnapshots,
    ResourceTags,
    Tag,
    Volumes,
)
from validation.aws_review.destroy_inventory import DestroyLedger, owned
from validation.aws_review.models import ReviewTarget


class Snapshot(ApiModel):
    identifier: str = Field(alias="SnapshotId")
    volume_id: str = Field(alias="VolumeId")
    state: str = Field(alias="State")
    tags: tuple[Tag, ...] = Field(default=(), alias="Tags")


class Snapshots(ApiModel):
    snapshots: tuple[Snapshot, ...] = Field(alias="Snapshots")


def remove_storage_residue(
    api: AwsApi, target: ReviewTarget, ledger: DestroyLedger
) -> tuple[str, ...]:
    """CFN削除後に残った保存課金を、親の台帳と実所有tagを両方照合して回収する。"""
    residuals: list[str] = []
    volume_ids = ledger.identifiers("AWS::EC2::Volume")
    if volume_ids:
        for page in api.pages(
            "ec2",
            "describe_volumes",
            Volumes,
            Filters=[{"Name": "volume-id", "Values": sorted(volume_ids)}],
        ):
            for volume in page.volumes:
                residuals.append(f"ebs:{volume.identifier}")
                if volume.identifier not in volume_ids or not owned(volume.tags, target):
                    raise ValueError("aws_review_destroy_volume_ownership_mismatch")
                if volume.state == "available":
                    api.call("ec2", "delete_volume", EmptyResponse, VolumeId=volume.identifier)
        for page in api.pages(
            "ec2",
            "describe_snapshots",
            Snapshots,
            OwnerIds=[target.account_id],
            Filters=[{"Name": "volume-id", "Values": sorted(volume_ids)}],
        ):
            for snapshot in page.snapshots:
                residuals.append(f"ebs-snapshot:{snapshot.identifier}")
                if snapshot.volume_id not in volume_ids or not owned(snapshot.tags, target):
                    raise ValueError("aws_review_destroy_snapshot_ownership_mismatch")
                if snapshot.state == "completed":
                    api.call(
                        "ec2", "delete_snapshot", EmptyResponse, SnapshotId=snapshot.identifier
                    )
    databases = ledger.identifiers("AWS::RDS::DBInstance")
    for page in api.pages("rds", "describe_db_snapshots", DbSnapshots, SnapshotType="manual"):
        for snapshot in page.snapshots:
            if snapshot.db_identifier not in databases:
                continue
            residuals.append(f"rds-snapshot:{snapshot.identifier}")
            tags = api.call(
                "rds", "list_tags_for_resource", ResourceTags, ResourceName=snapshot.arn
            )
            if not owned(tags.tags, target):
                raise ValueError("aws_review_destroy_snapshot_ownership_mismatch")
            if snapshot.state == "available":
                api.call(
                    "rds",
                    "delete_db_snapshot",
                    EmptyResponse,
                    DBSnapshotIdentifier=snapshot.identifier,
                )
    for page in api.pages("rds", "describe_db_instance_automated_backups", AutomatedBackups):
        for backup in page.backups:
            if backup.db_identifier not in databases:
                continue
            residuals.append(f"rds-backup:{backup.identifier}")
            # IAMは親DBを制約できないため、変更できないDB resource IDの保存台帳を必須とする。
            if backup.identifier not in ledger.identifiers("RdsResourceId"):
                raise ValueError("aws_review_destroy_backup_ownership_mismatch")
            if backup.state == "retained":
                api.call(
                    "rds",
                    "delete_db_instance_automated_backup",
                    EmptyResponse,
                    DbiResourceId=backup.identifier,
                )
    groups = ledger.identifiers("AWS::ElastiCache::ReplicationGroup")
    for page in api.pages("elasticache", "describe_snapshots", RedisSnapshots):
        for snapshot in page.snapshots:
            if snapshot.group_id not in groups:
                continue
            residuals.append(f"redis-snapshot:{snapshot.identifier}")
            tags = api.call(
                "elasticache", "list_tags_for_resource", ResourceTags, ResourceName=snapshot.arn
            )
            if not owned(tags.tags, target):
                raise ValueError("aws_review_destroy_snapshot_ownership_mismatch")
            if snapshot.state == "available":
                api.call(
                    "elasticache",
                    "delete_snapshot",
                    EmptyResponse,
                    SnapshotName=snapshot.identifier,
                )
    return tuple(residuals)
