from __future__ import annotations

from pydantic import Field

from quiz_app.load_test_aws.aws import ApiModel, AwsApi, AwsCallError, EmptyResponse
from quiz_app.load_test_aws.networks import Gateways, Interfaces, Vpcs
from quiz_app.load_test_aws.responses import Addresses, Instances, TaggedResources
from quiz_app.load_test_aws.storage import LogGroups
from validation.aws_review.destroy_inventory import DestroyLedger
from validation.aws_review.models import ReviewTarget


class NatGateway(ApiModel):
    identifier: str = Field(alias="NatGatewayId")
    state: str = Field(alias="State")


class NatGateways(ApiModel):
    items: tuple[NatGateway, ...] = Field(alias="NatGateways")


class CacheCluster(ApiModel):
    identifier: str = Field(alias="CacheClusterId")
    group: str | None = Field(default=None, alias="ReplicationGroupId")


class CacheClusters(ApiModel):
    items: tuple[CacheCluster, ...] = Field(alias="CacheClusters")


def present(
    api: AwsApi, service: str, operation: str, missing: tuple[str, ...], **parameters: object
) -> bool:
    """成功応答は存在を意味する。明示した不存在以外の失敗は必ず伝播する。"""
    try:
        api.call(service, operation, EmptyResponse, **parameters)
    except AwsCallError as error:
        if error.code in missing:
            return False
        raise
    return True


_GETTERS = (
    (
        "AWS::CloudWatch::Dashboard",
        "cloudwatch",
        "get_dashboard",
        "DashboardName",
        ("ResourceNotFound",),
    ),
    (
        "AWS::CloudFront::Distribution",
        "cloudfront",
        "get_distribution",
        "Id",
        ("NoSuchDistribution",),
    ),
    ("AWS::CloudFront::VpcOrigin", "cloudfront", "get_vpc_origin", "Id", ("EntityNotFound",)),
    (
        "AWS::RDS::DBInstance",
        "rds",
        "describe_db_instances",
        "DBInstanceIdentifier",
        ("DBInstanceNotFound",),
    ),
    (
        "AWS::RDS::DBSubnetGroup",
        "rds",
        "describe_db_subnet_groups",
        "DBSubnetGroupName",
        ("DBSubnetGroupNotFoundFault",),
    ),
    (
        "AWS::ElastiCache::ReplicationGroup",
        "elasticache",
        "describe_replication_groups",
        "ReplicationGroupId",
        ("ReplicationGroupNotFoundFault",),
    ),
    (
        "AWS::ElastiCache::SubnetGroup",
        "elasticache",
        "describe_cache_subnet_groups",
        "CacheSubnetGroupName",
        ("CacheSubnetGroupNotFoundFault",),
    ),
    (
        "AWS::SecretsManager::Secret",
        "secretsmanager",
        "describe_secret",
        "SecretId",
        ("ResourceNotFoundException",),
    ),
    ("AWS::IAM::Role", "iam", "get_role", "RoleName", ("NoSuchEntity",)),
    (
        "AWS::IAM::InstanceProfile",
        "iam",
        "get_instance_profile",
        "InstanceProfileName",
        ("NoSuchEntity",),
    ),
    (
        "AWS::StepFunctions::StateMachine",
        "stepfunctions",
        "describe_state_machine",
        "stateMachineArn",
        ("StateMachineDoesNotExist",),
    ),
)


def verify_resources(
    api: AwsApi, target: ReviewTarget, ledger: DestroyLedger, *, include_control: bool
) -> tuple[str, ...]:
    """CloudFormationの状態とは独立に、物理IDの不存在を各サービスへ照会する。"""
    result: list[str] = []
    control_kinds = {
        "AWS::IAM::Role",
        "AWS::IAM::InstanceProfile",
        "AWS::StepFunctions::StateMachine",
        "AWS::S3::Bucket",
        "AWS::Scheduler::Schedule",
    }
    for kind, service, operation, parameter, missing in _GETTERS:
        if not include_control and kind in control_kinds:
            continue
        for identifier in sorted(ledger.identifiers(kind)):
            if present(api, service, operation, missing, **{parameter: identifier}):
                result.append(f"{kind}:{identifier}")
    instance_ids = ledger.identifiers("AWS::EC2::Instance")
    if instance_ids:
        for page in api.pages(
            "ec2",
            "describe_instances",
            Instances,
            Filters=[{"Name": "instance-id", "Values": sorted(instance_ids)}],
        ):
            for reservation in page.reservations:
                result.extend(
                    f"instance:{item.identifier}"
                    for item in reservation.instances
                    if item.state.name != "terminated"
                )
    # CFNのEIP physical IDはAllocationIdでなく公開IPv4である。
    for address in ledger.identifiers("AWS::EC2::EIP"):
        observed = api.call(
            "ec2",
            "describe_addresses",
            Addresses,
            Filters=[{"Name": "public-ip", "Values": [address]}],
        )
        result.extend(f"eip:{item.identifier}" for item in observed.addresses)
    nat_ids = ledger.identifiers("AWS::EC2::NatGateway")
    if nat_ids:
        for page in api.pages(
            "ec2",
            "describe_nat_gateways",
            NatGateways,
            Filter=[{"Name": "nat-gateway-id", "Values": sorted(nat_ids)}],
        ):
            result.extend(
                f"nat:{item.identifier}" for item in page.items if item.state != "deleted"
            )
    for vpc in ledger.identifiers("AWS::EC2::VPC"):
        filters = [{"Name": "vpc-id", "Values": [vpc]}]
        observed = api.call("ec2", "describe_vpcs", Vpcs, Filters=filters)
        result.extend(f"vpc:{item.identifier}" for item in observed.items)
        for page in api.pages("ec2", "describe_network_interfaces", Interfaces, Filters=filters):
            result.extend(f"eni:{item.identifier}" for item in page.items)
    gateway_ids = ledger.identifiers("AWS::EC2::InternetGateway")
    if gateway_ids:
        gateways = api.call(
            "ec2",
            "describe_internet_gateways",
            Gateways,
            Filters=[{"Name": "internet-gateway-id", "Values": sorted(gateway_ids)}],
        )
        result.extend(f"internet-gateway:{item.identifier}" for item in gateways.items)
    for page in api.pages("elasticache", "describe_cache_clusters", CacheClusters):
        result.extend(
            f"cache-cluster:{item.identifier}"
            for item in page.items
            if item.group in ledger.identifiers("AWS::ElastiCache::ReplicationGroup")
        )
    for repository in ledger.identifiers("AWS::ECR::Repository"):
        if present(
            api,
            "ecr",
            "describe_repositories",
            ("RepositoryNotFoundException",),
            repositoryNames=[repository],
            registryId=target.account_id,
        ):
            result.append(f"ecr:{repository}")
    for name in ledger.identifiers("AWS::Logs::LogGroup"):
        for page in api.pages("logs", "describe_log_groups", LogGroups, logGroupNamePrefix=name):
            result.extend(f"logs:{item.name}" for item in page.groups if item.name == name)
    if include_control:
        for bucket in ledger.identifiers("AWS::S3::Bucket"):
            if present(
                api,
                "s3",
                "head_bucket",
                ("404", "NoSuchBucket", "NotFound"),
                Bucket=bucket,
                ExpectedBucketOwner=target.account_id,
            ):
                result.append(f"s3:{bucket}")
        for identifier in ledger.identifiers("AWS::Scheduler::Schedule"):
            name = identifier.rsplit("/", 1)[-1]
            if present(
                api,
                "scheduler",
                "get_schedule",
                ("ResourceNotFoundException",),
                Name=name,
                GroupName="default",
            ):
                result.append(f"scheduler:{name}")
    return tuple(result)


def require_no_untracked_resources(
    api: AwsApi, target: ReviewTarget, ledger: DestroyLedger
) -> None:
    """tag検索は補助検査だけに使い、台帳外の資源を勝手に削除しない。"""
    known = {resource.identifier for resource in ledger.resources}
    for page in api.pages(
        "resourcegroupstaggingapi",
        "get_resources",
        TaggedResources,
        TagFilters=[
            {"Key": "DemoId", "Values": [target.demo_id]},
            {"Key": "Purpose", "Values": ["quiz-rehearsal"]},
        ],
    ):
        for item in page.resources:
            if ":cloudformation:" in item.arn:
                if item.arn not in {stack.stack_id for stack in ledger.stacks}:
                    raise ValueError("aws_review_destroy_untracked_resource")
                continue
            # 削除済みEC2/NATなどのtag履歴が返るため、存在判定は上のサービスAPIで行う。
            service = item.arn.split(":", 5)[2]
            resource = item.arn.split(":", 5)[-1]
            if service == "ecr":
                identifier = resource.removeprefix("repository/")
            elif service in {"rds", "elasticache", "s3"}:
                identifier = resource.rsplit(":", 1)[-1]
            elif service == "logs":
                identifier = resource.removeprefix("log-group:").removesuffix(":*")
            else:
                identifier = resource.rsplit("/", 1)[-1]
            if item.arn not in known and identifier not in known:
                raise ValueError("aws_review_destroy_untracked_resource")
