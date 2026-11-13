"""既存CloudFormationとDockerfileで確認環境を新規作成・更新する。"""

import base64
import subprocess
import tempfile
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from pydantic import Field, SecretStr

from quiz_app.load_test_aws.aws import ApiModel, AwsApi, EmptyResponse, require_account
from quiz_app.load_test_aws.responses import Tag
from validation.aws_review.diagnostics import progress
from validation.aws_review.domain import DeploymentResult, validate_certificate
from validation.aws_review.host_contracts import HostInput
from validation.aws_review.instance_update import validate_update_instance
from validation.aws_review.models import DeploymentInput, validate_monitoring_deadline
from validation.aws_review.remote import deploy_host, host_commands
from validation.aws_review.stacks import apply_stack, get_stack


class Image(ApiModel):
    identifier: str = Field(alias="ImageId")
    owner: str = Field(alias="OwnerId")
    architecture: str = Field(alias="Architecture")
    name: str = Field(alias="Name")
    state: str = Field(alias="State")


class Images(ApiModel):
    images: tuple[Image, ...] = Field(alias="Images")


class Zone(ApiModel):
    name: str = Field(alias="ZoneName")
    identifier: str = Field(alias="ZoneId")
    state: str = Field(alias="State")


class Zones(ApiModel):
    zones: tuple[Zone, ...] = Field(alias="AvailabilityZones")


class DatabaseOption(ApiModel):
    version: str = Field(alias="EngineVersion")


class DatabaseOptions(ApiModel):
    options: tuple[DatabaseOption, ...] = Field(alias="OrderableDBInstanceOptions")


class Prefix(ApiModel):
    identifier: str = Field(alias="PrefixListId", pattern=r"^pl-[0-9a-f]+$")


class Prefixes(ApiModel):
    prefixes: tuple[Prefix, ...] = Field(alias="PrefixLists")


class Authorization(ApiModel):
    token: SecretStr = Field(alias="authorizationToken")
    endpoint: str = Field(alias="proxyEndpoint")


class Authorizations(ApiModel):
    entries: tuple[Authorization, ...] = Field(alias="authorizationData")


class ImageDetail(ApiModel):
    digest: str = Field(alias="imageDigest", pattern=r"^sha256:[0-9a-f]{64}$")


class ImageDetails(ApiModel):
    images: tuple[ImageDetail, ...] = Field(alias="imageDetails")


class BucketTags(ApiModel):
    tags: tuple[Tag, ...] = Field(alias="TagSet")


class RedisMembers(ApiModel):
    identifier: str = Field(alias="ReplicationGroupId")
    clusters: tuple[str, ...] = Field(alias="MemberClusters", min_length=1, max_length=1)


class RedisGroups(ApiModel):
    groups: tuple[RedisMembers, ...] = Field(alias="ReplicationGroups", min_length=1, max_length=1)


def redis_cluster_id(api: AwsApi, identifier: str) -> str:
    """CloudWatch の dimension は replication group ではなく実際の cache cluster ID。"""
    result = api.call(
        "elasticache", "describe_replication_groups", RedisGroups, ReplicationGroupId=identifier
    )
    group = result.groups[0]
    if group.identifier != identifier or not group.clusters[0].startswith(identifier + "-"):
        raise ValueError("review_redis_cluster_identity_invalid")
    return group.clusters[0]


def run(root: Path, argv: list[str], *, stdin: bytes | None = None) -> None:
    # 実行ファイルは固定し、認証情報はstdinへ渡す。例外のargvはCLI境界で出力しない。
    subprocess.run(argv, cwd=root, input=stdin, check=True, timeout=1800)  # noqa: S603


def preflight(api: AwsApi, config: DeploymentInput) -> str:
    require_account(api, config.account_id)
    if config.public_entrypoint == "custom-domain":
        progress("certificate_check")
        validate_certificate(api, config)
    images = api.call(
        "ec2", "describe_images", Images, ImageIds=[config.ubuntu_ami_id], Owners=["099720109477"]
    )
    if len(images.images) != 1:
        raise ValueError("review_ami_missing")
    image = images.images[0]
    if (
        image.owner != "099720109477"
        or image.architecture != "x86_64"
        or image.state != "available"
        or not image.name.startswith("ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-")
    ):
        raise ValueError("review_ami_invalid")
    zones = api.call(
        "ec2", "describe_availability_zones", Zones, ZoneNames=[config.app_az, config.secondary_az]
    )
    if {zone.name for zone in zones.zones} != {config.app_az, config.secondary_az} or any(
        zone.state != "available" for zone in zones.zones
    ):
        raise ValueError("review_zones_unavailable")
    if any(zone.name == config.app_az and zone.identifier == "apne1-az3" for zone in zones.zones):
        raise ValueError("review_cloudfront_zone_unsupported")
    options = api.call(
        "rds",
        "describe_orderable_db_instance_options",
        DatabaseOptions,
        Engine="postgres",
        EngineVersion=config.database_engine_version,
        DBInstanceClass="db.t4g.small",
        Vpc=True,
    )
    if not options.options:
        raise ValueError("review_database_version_unavailable")
    prefixes = api.call(
        "ec2",
        "describe_managed_prefix_lists",
        Prefixes,
        Filters=[
            {
                "Name": "prefix-list-name",
                "Values": ["com.amazonaws.global.cloudfront.origin-facing"],
            }
        ],
    )
    if len(prefixes.prefixes) != 1:
        raise ValueError("review_cloudfront_prefix_missing")
    return prefixes.prefixes[0].identifier


def build_images(api: AwsApi, root: Path, config: DeploymentInput) -> tuple[str, str]:
    registry = f"{config.account_id}.dkr.ecr.{config.region}.amazonaws.com"
    authorization = api.call(
        "ecr", "get_authorization_token", Authorizations, registryIds=[config.account_id]
    )
    if (
        len(authorization.entries) != 1
        or authorization.entries[0].endpoint != f"https://{registry}"
    ):
        raise ValueError("review_ecr_registry_mismatch")
    user, password = base64.b64decode(
        authorization.entries[0].token.get_secret_value(), validate=True
    ).split(b":", 1)
    if user != b"AWS":
        raise ValueError("review_ecr_user_mismatch")
    result = []
    with tempfile.TemporaryDirectory(prefix="review-docker-") as directory:
        docker = ["docker", "--config", directory]
        run(
            root,
            [*docker, "login", "--username", "AWS", "--password-stdin", registry],
            stdin=password,
        )
        for component, dockerfile in (
            ("api", "backend/Dockerfile"),
            ("gateway", "frontend/Dockerfile"),
        ):
            repository = f"{config.prefix}/{component}"
            tag = f"{config.source_commit}-{uuid4().hex[:12]}"
            reference = f"{registry}/{repository}:{tag}"
            frontend_arguments = (
                [
                    "--build-arg",
                    f"VITE_EVENT_START_AT={config.vite_event_start_at}",
                    "--build-arg",
                    f"VITE_SURVEY_URL={config.vite_survey_url}",
                    "--build-arg",
                    f"VITE_HOME_URL={config.vite_home_url}",
                ]
                if component == "gateway"
                else []
            )
            run(
                root,
                [
                    *docker,
                    "build",
                    "--platform",
                    "linux/amd64",
                    "--file",
                    dockerfile,
                    "--build-arg",
                    f"SOURCE_COMMIT={config.source_commit}",
                    "--build-arg",
                    "DEPLOYMENT_ENVIRONMENT=aws-demo",
                    *frontend_arguments,
                    "--tag",
                    reference,
                    ".",
                ],
            )
            run(root, [*docker, "push", reference])
            found = api.call(
                "ecr",
                "describe_images",
                ImageDetails,
                registryId=config.account_id,
                repositoryName=repository,
                imageIds=[{"imageTag": tag}],
            )
            if len(found.images) != 1:
                raise ValueError("review_image_digest_missing")
            result.append(f"{registry}/{repository}@{found.images[0].digest}")
    return result[0], result[1]


def deploy(
    api: AwsApi, config: DeploymentInput, root: Path, *, now: datetime | None = None
) -> DeploymentResult:
    validate_monitoring_deadline(config, now=now or datetime.now(UTC))
    existing = get_stack(api, config, "workload")
    if (config.deployment_mode == "create") == (existing is not None):
        raise ValueError("review_deployment_mode_stack_mismatch")
    if existing is not None:
        stable_parameters = {
            "DemoId": config.demo_id,
            "ManualReview": "true",
            "AppAvailabilityZone": config.app_az,
            "SecondaryAvailabilityZone": config.secondary_az,
            "UbuntuAmiId": config.ubuntu_ami_id,
            "DatabaseEngineVersion": config.database_engine_version,
        }
        actual = {item.key: item.value for item in existing.parameters}
        if any(actual.get(key) != value for key, value in stable_parameters.items()):
            raise ValueError("review_update_cannot_replace_data_or_host")
    progress("deploy_start")
    prefix_list = preflight(api, config)
    if existing is not None:
        validate_update_instance(api, config, existing)
    templates = root / "infra/aws-rehearsal"
    # 終了トリガーは利用者の明示操作。過去の時刻を新しい確認環境へ引き継がない。
    control = apply_stack(
        api,
        config,
        "control",
        templates / "control.yaml",
        {"DemoId": config.demo_id, "EnableDeadline": "false"},
    )
    role = control.output("CloudFormationExecutionRoleArn")
    progress("control_ready")
    workload = apply_stack(
        api,
        config,
        "workload",
        templates / "workload.yaml",
        {
            "DemoId": config.demo_id,
            "ManualReview": "true",
            "AppInstanceType": config.app_instance_type,
            "AppAvailabilityZone": config.app_az,
            "SecondaryAvailabilityZone": config.secondary_az,
            "UbuntuAmiId": config.ubuntu_ami_id,
            "DatabaseEngineVersion": config.database_engine_version,
            "ArtifactBucketName": control.output("ArtifactBucketName"),
        },
        role_arn=role,
        allow_instance_type_conditional=existing is not None,
    )
    progress("workload_ready")
    cache_cluster_id = redis_cluster_id(api, workload.output("RedisIdentifier"))
    edge_parameters = {
        "DemoId": config.demo_id,
        "ReviewDomainName": (
            config.review_domain_name if config.public_entrypoint == "custom-domain" else ""
        ),
        "ReviewCertificateArn": (
            config.review_certificate_arn if config.public_entrypoint == "custom-domain" else ""
        ),
        "OriginInstanceArn": workload.output("AppInstanceArn"),
        "OriginPrivateDnsName": workload.output("AppPrivateDnsName"),
        "OriginSecurityGroupId": workload.output("AppSecurityGroupId"),
        "CloudFrontPrefixListId": prefix_list,
        "PublicEnabled": "false",
        "MonitoringEnabled": "true",
        "MonitoringInstanceId": workload.output("AppInstanceId"),
        "MonitoringDatabaseIdentifier": workload.output("DatabaseIdentifier"),
        "MonitoringRedisClusterId": cache_cluster_id,
    }
    edge = apply_stack(
        api, config, "edge", templates / "public-edge.yaml", edge_parameters, role_arn=role
    )
    cloudfront_domain = edge.output("CloudFrontDomainName")
    result = DeploymentResult(
        public_entrypoint=config.public_entrypoint,
        review_domain_name=config.review_domain_name,
        url=(
            f"https://{cloudfront_domain}"
            if config.public_entrypoint == "cloudfront"
            else edge.output("PublicUrl")
        ),
        cloudfront_domain_name=cloudfront_domain,
    )
    progress("edge_prepared")
    progress("image_build")
    api_image, gateway_image = build_images(api, root, config)
    progress("images_ready")
    host = HostInput(
        deployment_mode=config.deployment_mode,
        monitoring_expires_at=config.monitoring_expires_at,
        account_id=config.account_id,
        demo_id=config.demo_id,
        source_commit=config.source_commit,
        api_image=api_image,
        gateway_image=gateway_image,
        instance_id=workload.output("AppInstanceId"),
        private_ip=workload.output("AppPrivateIp"),
        public_url=result.url,
        review_domain_name=config.review_domain_name,
        database_identifier=workload.output("DatabaseIdentifier"),
        redis_identifier=workload.output("RedisIdentifier"),
        rds_hostname=workload.output("DatabaseEndpoint"),
        redis_hostname=workload.output("RedisEndpoint"),
        database_master_secret_arn=workload.output("DatabaseMasterSecretArn"),
        redis_auth_secret_arn=workload.output("RedisAuthSecretArn"),
        runtime_secret_arn=workload.output("RuntimeSecretArn"),
    )
    bucket = control.output("ArtifactBucketName")
    if bucket != f"gs-review-{config.demo_id}-{config.account_id}":
        raise ValueError("review_artifact_bucket_identity_mismatch")
    tags = api.call(
        "s3",
        "get_bucket_tagging",
        BucketTags,
        Bucket=bucket,
        ExpectedBucketOwner=config.account_id,
    )
    observed = {tag.key: tag.value for tag in tags.tags}
    if observed.get("DemoId") != config.demo_id or observed.get("Purpose") != "quiz-rehearsal":
        raise ValueError("review_artifact_bucket_ownership_mismatch")
    with tempfile.TemporaryDirectory(prefix="review-source-") as directory:
        bundle = Path(directory) / "source.bundle"
        run(root, ["git", "bundle", "create", str(bundle), "HEAD"])
        body = bundle.read_bytes()
        api.call(
            "s3",
            "put_object",
            EmptyResponse,
            Bucket=bucket,
            Key=f"{config.demo_id}/source.bundle",
            Body=body,
            ServerSideEncryption="AES256",
            ExpectedBucketOwner=config.account_id,
        )
        progress("source_uploaded")
        progress("host_preparing")
        deploy_host(api, host, host_commands(root, host, bucket, sha256(body).hexdigest()))
    progress("host_ready")
    # 全phase成功後だけ公開し、失敗時は無効状態を維持する。
    if get_stack(api, config, "workload") is None:
        raise ValueError("review_workload_disappeared")
    edge_parameters["PublicEnabled"] = "true"
    apply_stack(api, config, "edge", templates / "public-edge.yaml", edge_parameters, role_arn=role)
    progress("public_ready")
    return result
