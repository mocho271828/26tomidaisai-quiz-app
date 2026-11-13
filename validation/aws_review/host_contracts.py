"""確認環境のhost入力とAWS所有証明を、DB操作前に検証する。"""

import re
from ipaddress import IPv4Address
from typing import Literal, Protocol, Self, cast

import boto3
import httpx
from botocore.config import Config
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from validation.aws_review.models import ReviewDomainName


class HostRefusedError(RuntimeError):
    """秘密値を含まない固定codeで配置を停止する。"""


class HostInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    deployment_mode: Literal["create", "update"] = "update"
    monitoring_expires_at: AwareDatetime | None = None
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    region: Literal["ap-northeast-1"] = "ap-northeast-1"
    demo_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", min_length=1, max_length=21)
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    api_image: str
    gateway_image: str
    instance_id: str = Field(pattern=r"^i-[a-f0-9]{17}$")
    private_ip: str
    public_url: str
    review_domain_name: ReviewDomainName = ""
    database_identifier: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9-]{0,62}$")
    redis_identifier: str = Field(pattern=r"^[a-z][a-z0-9-]{0,39}$")
    rds_hostname: str = Field(pattern=r"^[a-zA-Z0-9.-]+\.ap-northeast-1\.rds\.amazonaws\.com$")
    redis_hostname: str = Field(pattern=r"^[a-zA-Z0-9.-]+\.cache\.amazonaws\.com$")
    database_master_secret_arn: str
    redis_auth_secret_arn: str
    runtime_secret_arn: str

    @field_validator("private_ip")
    @classmethod
    def private_ipv4_only(cls, value: str) -> str:
        address = IPv4Address(value)
        if (
            not address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_unspecified
            or address.is_reserved
            or address.is_multicast
        ):
            raise ValueError("review_private_ipv4_required")
        return str(address)

    @model_validator(mode="after")
    def restrict_resource_names(self) -> Self:
        if self.deployment_mode == "create" and self.monitoring_expires_at is None:
            raise ValueError("review_create_monitoring_expiry_required")
        if self.review_domain_name:
            valid_origin = self.public_url == f"https://{self.review_domain_name}"
        else:
            valid_origin = (
                re.fullmatch(r"https://[a-z0-9]+\.cloudfront\.net", self.public_url) is not None
            )
        if not valid_origin:
            raise ValueError("review_public_url_mismatch")
        repository = (
            f"{self.account_id}.dkr.ecr.{self.region}.amazonaws.com/gs-rehearsal-{self.demo_id}"
        )
        for name, image in (("api", self.api_image), ("gateway", self.gateway_image)):
            if (
                re.fullmatch(re.escape(f"{repository}/{name}@sha256:") + r"[a-f0-9]{64}", image)
                is None
            ):
                raise ValueError("review_image_identity_invalid")
        prefix = f"arn:aws:secretsmanager:{self.region}:{self.account_id}:secret:"
        if not self.database_master_secret_arn.startswith(prefix + "rds!"):
            raise ValueError("review_master_secret_identity_invalid")
        for kind, arn in (
            ("redis", self.redis_auth_secret_arn),
            ("runtime", self.runtime_secret_arn),
        ):
            if not arn.startswith(f"{prefix}gs-rehearsal-{self.demo_id}-{kind}-"):
                raise ValueError("review_secret_identity_invalid")
        if self.redis_identifier != f"gs-rehearsal-{self.demo_id}-redis":
            raise ValueError("review_redis_identity_invalid")
        return self


class AwsClient(Protocol):
    def describe_instances(self, **kwargs: object) -> object: ...
    def describe_db_instances(self, **kwargs: object) -> object: ...
    def describe_stack_resource(self, **kwargs: object) -> object: ...
    def describe_replication_groups(self, **kwargs: object) -> object: ...
    def list_tags_for_resource(self, **kwargs: object) -> object: ...
    def describe_secret(self, **kwargs: object) -> object: ...
    def get_secret_value(self, **kwargs: object) -> object: ...


def aws_client(service: str) -> AwsClient:
    """EC2 roleを使い、外部I/Oの待機とretryを制限する。"""
    return cast(
        AwsClient,
        boto3.client(
            service,
            region_name="ap-northeast-1",
            config=Config(
                connect_timeout=5,
                read_timeout=15,
                retries={"total_max_attempts": 3, "mode": "standard"},
                ignore_configured_endpoint_urls=True,
                proxies={},
            ),
        ),
    )


class AwsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)


class Tag(AwsResponse):
    key: str = Field(alias="Key")
    value: str = Field(alias="Value")


class Endpoint(AwsResponse):
    address: str = Field(alias="Address")
    port: int = Field(alias="Port")


class MasterSecret(AwsResponse):
    arn: str = Field(alias="SecretArn")


class DatabaseSubnet(AwsResponse):
    vpc_id: str = Field(alias="VpcId")


class Database(AwsResponse):
    identifier: str = Field(alias="DBInstanceIdentifier")
    arn: str = Field(alias="DBInstanceArn")
    name: Literal["quiz"] = Field(alias="DBName")
    username: Literal["quizadmin"] = Field(alias="MasterUsername")
    status: Literal["available"] = Field(alias="DBInstanceStatus")
    public: Literal[False] = Field(alias="PubliclyAccessible")
    endpoint: Endpoint = Field(alias="Endpoint")
    secret: MasterSecret = Field(alias="MasterUserSecret")
    subnet: DatabaseSubnet = Field(alias="DBSubnetGroup")
    tags: list[Tag] = Field(alias="TagList")


class DatabaseResponse(AwsResponse):
    databases: list[Database] = Field(alias="DBInstances", min_length=1, max_length=1)


class Instance(AwsResponse):
    identifier: str = Field(alias="InstanceId")
    private_ip: str = Field(alias="PrivateIpAddress")
    vpc_id: str = Field(alias="VpcId")
    tags: list[Tag] = Field(alias="Tags")


class Reservation(AwsResponse):
    owner: str = Field(alias="OwnerId")
    instances: list[Instance] = Field(alias="Instances", min_length=1, max_length=1)


class InstancesResponse(AwsResponse):
    reservations: list[Reservation] = Field(alias="Reservations", min_length=1, max_length=1)


class NodeGroup(AwsResponse):
    endpoint: Endpoint = Field(alias="PrimaryEndpoint")


class RedisGroup(AwsResponse):
    identifier: str = Field(alias="ReplicationGroupId")
    arn: str = Field(alias="ARN")
    status: Literal["available"] = Field(alias="Status")
    tls: Literal[True] = Field(alias="TransitEncryptionEnabled")
    auth: Literal[True] = Field(alias="AuthTokenEnabled")
    nodes: list[NodeGroup] = Field(alias="NodeGroups", min_length=1, max_length=1)


class RedisResponse(AwsResponse):
    groups: list[RedisGroup] = Field(alias="ReplicationGroups", min_length=1, max_length=1)


class RedisStackResource(AwsResponse):
    stack_id: str = Field(alias="StackId")
    stack_name: str = Field(alias="StackName")
    logical_id: Literal["Redis"] = Field(alias="LogicalResourceId")
    physical_id: str = Field(alias="PhysicalResourceId")
    kind: Literal["AWS::ElastiCache::ReplicationGroup"] = Field(alias="ResourceType")
    status: Literal["CREATE_COMPLETE", "UPDATE_COMPLETE"] = Field(alias="ResourceStatus")


class RedisStackResponse(AwsResponse):
    resource: RedisStackResource = Field(alias="StackResourceDetail")


class TagsResponse(AwsResponse):
    tags: list[Tag] = Field(alias="TagList")


class SecretMetadata(AwsResponse):
    arn: str = Field(alias="ARN")
    tags: list[Tag] = Field(alias="Tags")


class IdentityDocument(AwsResponse):
    account: str = Field(alias="accountId")
    instance: str = Field(alias="instanceId")
    address: str = Field(alias="privateIp")
    region: str


def _require_tags(tags: list[Tag], inputs: HostInput) -> None:
    actual = {tag.key: tag.value for tag in tags}
    expected = {
        "DemoId": inputs.demo_id,
        "Purpose": "quiz-rehearsal",
        "aws:cloudformation:stack-name": f"gs-rehearsal-{inputs.demo_id}-workload",
    }
    if any(actual.get(key) != value for key, value in expected.items()):
        raise HostRefusedError("review_resource_ownership_mismatch")


def _require_redis_ownership(
    inputs: HostInput, instance: Instance, group: RedisGroup, tags: list[Tag]
) -> None:
    stack_name = f"gs-rehearsal-{inputs.demo_id}-workload"
    actual = {tag.key: tag.value for tag in tags}
    if (
        actual.get("DemoId") != inputs.demo_id
        or actual.get("Purpose") != "quiz-rehearsal"
        or actual.get("aws:cloudformation:stack-name", stack_name) != stack_name
    ):
        raise HostRefusedError("review_resource_ownership_mismatch")
    instance_tags = {tag.key: tag.value for tag in instance.tags}
    stack_id = instance_tags.get("aws:cloudformation:stack-id", "")
    prefix = f"arn:aws:cloudformation:{inputs.region}:{inputs.account_id}:stack/{stack_name}/"
    if (
        re.fullmatch(re.escape(prefix) + r"[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}", stack_id)
        is None
    ):
        raise HostRefusedError("review_instance_stack_identity_mismatch")
    # Redisへの自動stack tag伝播は保証されないため、EC2と同じCFN台帳で所有を証明する。
    resource = RedisStackResponse.model_validate(
        aws_client("cloudformation").describe_stack_resource(
            StackName=stack_id, LogicalResourceId="Redis"
        )
    ).resource
    if (
        resource.stack_id != stack_id
        or resource.stack_name != stack_name
        or resource.physical_id != group.identifier
    ):
        raise HostRefusedError("review_redis_stack_identity_mismatch")


def require_ownership(inputs: HostInput) -> None:
    """IMDSv2とAWS resource identityを照合し、別環境へのDB書込を拒否する。"""
    metadata_url = "http://169.254.169.254/latest"
    with httpx.Client(timeout=5, trust_env=False) as client:
        token = client.put(
            f"{metadata_url}/api/token", headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"}
        )
        token.raise_for_status()
        document = client.get(
            f"{metadata_url}/dynamic/instance-identity/document",
            headers={"X-aws-ec2-metadata-token": token.text},
        )
        document.raise_for_status()
        identity = IdentityDocument.model_validate_json(document.content)
    if (identity.account, identity.instance, identity.address, identity.region) != (
        inputs.account_id,
        inputs.instance_id,
        inputs.private_ip,
        inputs.region,
    ):
        raise HostRefusedError("review_host_identity_mismatch")
    reservation = InstancesResponse.model_validate(
        aws_client("ec2").describe_instances(InstanceIds=[inputs.instance_id])
    ).reservations[0]
    instance = reservation.instances[0]
    _require_tags(instance.tags, inputs)
    if (reservation.owner, instance.identifier, instance.private_ip) != (
        inputs.account_id,
        inputs.instance_id,
        inputs.private_ip,
    ):
        raise HostRefusedError("review_instance_identity_mismatch")
    database = DatabaseResponse.model_validate(
        aws_client("rds").describe_db_instances(DBInstanceIdentifier=inputs.database_identifier)
    ).databases[0]
    _require_tags(database.tags, inputs)
    expected_arn = (
        f"arn:aws:rds:{inputs.region}:{inputs.account_id}:db:{inputs.database_identifier}"
    )
    if (
        database.identifier != inputs.database_identifier
        or database.arn != expected_arn
        or database.subnet.vpc_id != instance.vpc_id
        or database.endpoint.address != inputs.rds_hostname
        or database.endpoint.port != 5432
        or database.secret.arn != inputs.database_master_secret_arn
    ):
        raise HostRefusedError("review_database_identity_mismatch")
    redis = aws_client("elasticache")
    group = RedisResponse.model_validate(
        redis.describe_replication_groups(ReplicationGroupId=inputs.redis_identifier)
    ).groups[0]
    expected_redis_arn = (
        f"arn:aws:elasticache:{inputs.region}:{inputs.account_id}:replicationgroup:"
        f"{inputs.redis_identifier}"
    )
    if (
        group.identifier != inputs.redis_identifier
        or group.arn != expected_redis_arn
        or group.nodes[0].endpoint.address != inputs.redis_hostname
        or group.nodes[0].endpoint.port != 6379
    ):
        raise HostRefusedError("review_redis_identity_mismatch")
    _require_redis_ownership(
        inputs,
        instance,
        group,
        TagsResponse.model_validate(redis.list_tags_for_resource(ResourceName=group.arn)).tags,
    )
    secrets = aws_client("secretsmanager")
    for arn in (inputs.redis_auth_secret_arn, inputs.runtime_secret_arn):
        metadata = SecretMetadata.model_validate(secrets.describe_secret(SecretId=arn))
        if metadata.arn != arn:
            raise HostRefusedError("review_secret_identity_mismatch")
        _require_tags(metadata.tags, inputs)


class SecretResponse(AwsResponse):
    arn: str = Field(alias="ARN")
    value: SecretStr = Field(alias="SecretString")


def read_runtime_secret(client: AwsClient, arn: str) -> str:
    """EC2内だけで解決し、応答・秘密本文をログやCLI引数へ渡さない。"""
    response = SecretResponse.model_validate(client.get_secret_value(SecretId=arn))
    if response.arn != arn:
        raise HostRefusedError("review_secret_response_mismatch")
    return response.value.get_secret_value()
