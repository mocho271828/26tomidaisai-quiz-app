from __future__ import annotations

from pydantic import Field

from quiz_app.load_test_aws.aws import ApiModel, AwsApi
from quiz_app.load_test_aws.models import CleanupInput, Residual
from quiz_app.load_test_aws.ownership import RunLedger
from quiz_app.load_test_aws.responses import Instances, Snapshots, Tag, Volumes


class Image(ApiModel):
    identifier: str = Field(alias="ImageId")
    state: str = Field(alias="State")
    tags: tuple[Tag, ...] = Field(default=(), alias="Tags")


class Images(ApiModel):
    images: tuple[Image, ...] = Field(alias="Images")


def scan_changed_identity(api: AwsApi, run: CleanupInput, ledger: RunLedger) -> list[Residual]:
    """tag欠落が不存在に見えないよう、台帳の物理IDでも独立に照会する。"""
    result: list[Residual] = []
    identifiers = ledger.identifiers("AWS::EC2::Instance")
    if identifiers:
        for page in api.pages(
            "ec2",
            "describe_instances",
            Instances,
            Filters=[{"Name": "instance-id", "Values": sorted(identifiers)}],
        ):
            for reservation in page.reservations:
                for instance in reservation.instances:
                    if instance.state.name != "terminated" and not ledger.permits(
                        "AWS::EC2::Instance", instance.identifier, instance.tags, run
                    ):
                        result.append(
                            Residual(
                                kind="ec2-instance",
                                identifier=instance.identifier,
                                state="ownership-unverified",
                            )
                        )
    for kind, operation, model, filter_name, field, residual_kind in (
        ("AWS::EC2::Volume", "describe_volumes", Volumes, "volume-id", "volumes", "ebs-volume"),
        (
            "AWS::EC2::Snapshot",
            "describe_snapshots",
            Snapshots,
            "snapshot-id",
            "snapshots",
            "ebs-snapshot",
        ),
        ("AWS::EC2::Image", "describe_images", Images, "image-id", "images", "ami"),
    ):
        identifiers = ledger.identifiers(kind)
        if not identifiers:
            continue
        for page in api.pages(
            "ec2", operation, model, Filters=[{"Name": filter_name, "Values": sorted(identifiers)}]
        ):
            for item in getattr(page, field):
                if not ledger.permits(kind, item.identifier, item.tags, run):
                    result.append(
                        Residual(
                            kind=residual_kind,
                            identifier=item.identifier,
                            state="ownership-unverified",
                        )
                    )
    if ledger.stack_ids:
        # EC2削除後にも残る間接EBSは、変更できないCFN予約tagから独立に検査する。
        for page in api.pages(
            "ec2",
            "describe_volumes",
            Volumes,
            Filters=[
                {"Name": "tag:aws:cloudformation:stack-id", "Values": sorted(ledger.stack_ids)}
            ],
        ):
            for item in page.volumes:
                if not ledger.permits("AWS::EC2::Volume", item.identifier, item.tags, run):
                    result.append(
                        Residual(
                            kind="ebs-volume",
                            identifier=item.identifier,
                            state="ownership-unverified",
                        )
                    )
    return result
