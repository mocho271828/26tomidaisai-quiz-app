"""EC2 の型変更を許可する前に、同じ EBS host の所有と構成を照合する。"""

from typing import Literal

from pydantic import Field

from quiz_app.load_test_aws.aws import ApiModel, AwsApi
from quiz_app.load_test_aws.responses import Tag
from validation.aws_review.models import DeploymentInput
from validation.aws_review.stacks import Stack


class InstanceProfile(ApiModel):
    arn: str = Field(alias="Arn")


class Placement(ApiModel):
    zone: str = Field(alias="AvailabilityZone")


class Metadata(ApiModel):
    tokens: Literal["required"] = Field(alias="HttpTokens")
    hop_limit: Literal[1] = Field(alias="HttpPutResponseHopLimit")


class InstanceState(ApiModel):
    name: Literal["running", "stopped"] = Field(alias="Name")


class UpdateInstance(ApiModel):
    identifier: str = Field(alias="InstanceId")
    image: str = Field(alias="ImageId")
    instance_type: Literal["t3.medium", "c7i.xlarge"] = Field(alias="InstanceType")
    architecture: Literal["x86_64"] = Field(alias="Architecture")
    root_device: Literal["ebs"] = Field(alias="RootDeviceType")
    vpc: str = Field(alias="VpcId")
    subnet: str = Field(alias="SubnetId")
    profile: InstanceProfile = Field(alias="IamInstanceProfile")
    placement: Placement = Field(alias="Placement")
    metadata: Metadata = Field(alias="MetadataOptions")
    state: InstanceState = Field(alias="State")
    tags: tuple[Tag, ...] = Field(alias="Tags")


class Reservation(ApiModel):
    owner: str = Field(alias="OwnerId")
    instances: tuple[UpdateInstance, ...] = Field(alias="Instances", min_length=1, max_length=1)


class InstanceResponse(ApiModel):
    reservations: tuple[Reservation, ...] = Field(alias="Reservations", min_length=1, max_length=1)


def validate_update_instance(api: AwsApi, config: DeploymentInput, stack: Stack) -> None:
    """型変更の Conditional を、同 AMI・x86_64・EBS の同一専用 host に限る。"""
    instance_id = stack.output("AppInstanceId")
    response = api.call("ec2", "describe_instances", InstanceResponse, InstanceIds=[instance_id])
    reservation = response.reservations[0]
    instance = reservation.instances[0]
    expected_tags = {
        "DemoId": config.demo_id,
        "Purpose": "quiz-rehearsal",
        "aws:cloudformation:stack-name": stack.name,
        "aws:cloudformation:stack-id": stack.arn,
    }
    actual_tags = {item.key: item.value for item in instance.tags}
    profile_arn = (
        f"arn:aws:iam::{config.account_id}:instance-profile/gs-review-{config.demo_id}-app"
    )
    if (
        reservation.owner != config.account_id
        or instance.identifier != instance_id
        or instance.image != config.ubuntu_ami_id
        or instance.vpc != stack.output("VpcId")
        or instance.subnet != stack.output("AppSubnetId")
        or instance.placement.zone != config.app_az
        or instance.profile.arn != profile_arn
        or any(actual_tags.get(key) != value for key, value in expected_tags.items())
    ):
        raise ValueError("review_update_instance_identity_mismatch")
