"""型変更の Conditional 許可に使う EC2 所有・EBS 証拠を外部 I/O なしで検証する。"""

from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from test_aws_review_deploy import ACCOUNT, CONFIG, owned_stack
from test_load_test_aws import FakeAws

from validation.aws_review import deploy
from validation.aws_review.instance_update import validate_update_instance
from validation.aws_review.stacks import Output, Stack

INSTANCE_ID = "i-0123456789abcdef0"
VPC_ID = "vpc-0123456789abcdef0"
SUBNET_ID = "subnet-0123456789abcdef0"


def existing_stack() -> Stack:
    return owned_stack().model_copy(
        update={
            "outputs": (
                Output(key="AppInstanceId", value=INSTANCE_ID),
                Output(key="VpcId", value=VPC_ID),
                Output(key="AppSubnetId", value=SUBNET_ID),
            )
        }
    )


def instance_fixture() -> dict[str, object]:
    stack = existing_stack()
    return {
        "InstanceId": INSTANCE_ID,
        "ImageId": CONFIG.ubuntu_ami_id,
        "InstanceType": "t3.medium",
        "Architecture": "x86_64",
        "RootDeviceType": "ebs",
        "VpcId": VPC_ID,
        "SubnetId": SUBNET_ID,
        "IamInstanceProfile": {
            "Arn": f"arn:aws:iam::{ACCOUNT}:instance-profile/gs-review-review-app"
        },
        "Placement": {"AvailabilityZone": CONFIG.app_az},
        "MetadataOptions": {"HttpTokens": "required", "HttpPutResponseHopLimit": 1},
        "State": {"Name": "running"},
        "Tags": [
            {"Key": key, "Value": value}
            for key, value in {
                "DemoId": CONFIG.demo_id,
                "Purpose": "quiz-rehearsal",
                "aws:cloudformation:stack-name": stack.name,
                "aws:cloudformation:stack-id": stack.arn,
            }.items()
        ],
    }


def api_fixture(
    instance: dict[str, object], *, owner: str = ACCOUNT, reservations: int = 1, instances: int = 1
) -> FakeAws:
    api = FakeAws()
    api.responses[("ec2", "describe_instances")] = {
        "Reservations": [{"OwnerId": owner, "Instances": [instance] * instances}] * reservations
    }
    return api


@pytest.mark.parametrize("instance_type", ["t3.medium", "c7i.xlarge"])
@pytest.mark.parametrize("state", ["running", "stopped"])
def test_same_ebs_x86_host_is_verified_before_type_change(instance_type: str, state: str) -> None:
    instance = {**instance_fixture(), "InstanceType": instance_type, "State": {"Name": state}}
    api = api_fixture(instance)
    validate_update_instance(api, CONFIG, existing_stack())
    assert api.calls == [("ec2", "describe_instances", {"InstanceIds": [INSTANCE_ID]})]


@pytest.mark.parametrize(
    "field,value",
    [
        ("InstanceId", "i-11111111111111111"),
        ("ImageId", "ami-11111111111111111"),
        ("InstanceType", "m7i.xlarge"),
        ("Architecture", "arm64"),
        ("RootDeviceType", "instance-store"),
        ("VpcId", "vpc-11111111111111111"),
        ("SubnetId", "subnet-11111111111111111"),
        ("IamInstanceProfile", {"Arn": f"arn:aws:iam::{ACCOUNT}:instance-profile/another"}),
        ("Placement", {"AvailabilityZone": CONFIG.secondary_az}),
        ("MetadataOptions", {"HttpTokens": "optional", "HttpPutResponseHopLimit": 1}),
        ("MetadataOptions", {"HttpTokens": "required", "HttpPutResponseHopLimit": 2}),
        ("State", {"Name": "terminated"}),
        ("Tags", []),
    ],
)
def test_unsafe_instance_stops_deploy_before_stack_mutation(
    monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    api = api_fixture({**instance_fixture(), field: value})
    monkeypatch.setattr(deploy, "get_stack", Mock(return_value=existing_stack()))
    monkeypatch.setattr(deploy, "preflight", Mock(return_value="pl-0123456789abcdef0"))
    mutation = Mock()
    monkeypatch.setattr(deploy, "apply_stack", mutation)
    with pytest.raises((ValueError, ValidationError)):
        deploy.deploy(api, CONFIG, Path("/not-used"))
    mutation.assert_not_called()
    assert len(api.calls) == 1


@pytest.mark.parametrize("reservations,instances", [(0, 1), (2, 1), (1, 0), (1, 2)])
def test_exactly_one_returned_instance_is_required(reservations: int, instances: int) -> None:
    api = api_fixture(instance_fixture(), reservations=reservations, instances=instances)
    with pytest.raises(ValidationError):
        validate_update_instance(api, CONFIG, existing_stack())


def test_owner_account_must_match() -> None:
    api = api_fixture(instance_fixture(), owner="999999999999")
    with pytest.raises(ValueError, match="instance_identity_mismatch"):
        validate_update_instance(api, CONFIG, existing_stack())


@pytest.mark.parametrize(
    "missing", ["IamInstanceProfile", "RootDeviceType", "Architecture", "Tags"]
)
def test_missing_proof_is_not_assumed_safe(missing: str) -> None:
    instance = instance_fixture()
    instance.pop(missing)
    with pytest.raises(ValidationError):
        validate_update_instance(api_fixture(instance), CONFIG, existing_stack())
