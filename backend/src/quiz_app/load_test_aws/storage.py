from __future__ import annotations

from pydantic import Field

from quiz_app.load_test_aws.aws import ApiModel, AwsApi, AwsCallError, EmptyResponse
from quiz_app.load_test_aws.models import CleanupInput, Residual
from quiz_app.load_test_aws.ownership import RunLedger, owned
from quiz_app.load_test_aws.responses import Multiparts, ObjectVersions, Tag

EMPTY_LEDGER = RunLedger()


class Repository(ApiModel):
    name: str = Field(alias="repositoryName")
    arn: str = Field(alias="repositoryArn")


class Repositories(ApiModel):
    repositories: tuple[Repository, ...]


class EcrTag(ApiModel):
    key: str = Field(alias="Key")
    value: str = Field(alias="Value")


class EcrTags(ApiModel):
    tags: tuple[EcrTag, ...]


class Secret(ApiModel):
    arn: str = Field(alias="ARN")
    name: str = Field(alias="Name")
    tags: tuple[Tag, ...] = Field(default=(), alias="Tags")


class Secrets(ApiModel):
    secrets: tuple[Secret, ...] = Field(alias="SecretList")


class Bucket(ApiModel):
    name: str = Field(alias="Name")


class Buckets(ApiModel):
    buckets: tuple[Bucket, ...] = Field(alias="Buckets")


class BucketTags(ApiModel):
    tags: tuple[Tag, ...] = Field(alias="TagSet")


class LogTags(ApiModel):
    tags: dict[str, str]


class LogGroup(ApiModel):
    name: str = Field(alias="logGroupName")


class LogGroups(ApiModel):
    groups: tuple[LogGroup, ...] = Field(alias="logGroups")


class Alarm(ApiModel):
    name: str = Field(alias="AlarmName")


class Alarms(ApiModel):
    alarms: tuple[Alarm, ...] = Field(default=(), alias="MetricAlarms")
    composite_alarms: tuple[Alarm, ...] = Field(default=(), alias="CompositeAlarms")
    log_alarms: tuple[Alarm, ...] = Field(default=(), alias="LogAlarms")
    next_token: str | None = Field(default=None, alias="NextToken")


class Queue(ApiModel):
    url: str = Field(alias="QueueUrl")


class WorkerState(ApiModel):
    state: str = Field(alias="State")


class ActiveSchedule(ApiModel):
    state: str = Field(alias="State")


class DeleteError(ApiModel):
    code: str = Field(alias="Code")


class DeletedObjects(ApiModel):
    errors: tuple[DeleteError, ...] = Field(default=(), alias="Errors")


def empty_bucket(api: AwsApi, name: str) -> None:
    """S3はdelete marker・旧version・未完了uploadまで明示的に消す。"""
    for page in api.pages("s3", "list_object_versions", ObjectVersions, Bucket=name):
        objects = [
            {"Key": item.key, "VersionId": item.version} for item in (*page.versions, *page.markers)
        ]
        for offset in range(0, len(objects), 1_000):
            result = api.call(
                "s3",
                "delete_objects",
                DeletedObjects,
                Bucket=name,
                Delete={"Objects": objects[offset : offset + 1_000], "Quiet": True},
            )
            if result.errors:
                raise ValueError("aws_load_test_s3_partial_delete_failed")
    for page in api.pages("s3", "list_multipart_uploads", Multiparts, Bucket=name):
        for upload in page.uploads:
            api.call(
                "s3",
                "abort_multipart_upload",
                EmptyResponse,
                Bucket=name,
                Key=upload.key,
                UploadId=upload.upload_id,
            )


def scan_storage(
    api: AwsApi, run: CleanupInput, *, delete: bool, ledger: RunLedger = EMPTY_LEDGER
) -> list[Residual]:
    result: list[Residual] = []
    for page in api.pages("ecr", "describe_repositories", Repositories):
        for item in page.repositories:
            if not item.name.startswith(f"{run.prefix}/"):
                continue
            observed = api.call("ecr", "list_tags_for_resource", EcrTags, resourceArn=item.arn)
            if not owned(tuple(Tag(Key=tag.key, Value=tag.value) for tag in observed.tags), run):
                raise ValueError("aws_load_test_repository_ownership_mismatch")
            result.append(Residual(kind="ecr-repository", identifier=item.name, state="present"))
            if delete and item.name in ledger.identifiers("AWS::ECR::Repository"):
                api.call(
                    "ecr", "delete_repository", EmptyResponse, repositoryName=item.name, force=True
                )
    for page in api.pages(
        "secretsmanager",
        "list_secrets",
        Secrets,
        IncludePlannedDeletion=True,
        Filters=[{"Key": "name", "Values": [f"{run.prefix}/"]}],
    ):
        for item in page.secrets:
            if not owned(item.tags, run):
                raise ValueError("aws_load_test_secret_ownership_mismatch")
            result.append(Residual(kind="secret", identifier=item.arn, state="present"))
            if delete and item.arn in ledger.identifiers("AWS::SecretsManager::Secret"):
                api.call(
                    "secretsmanager",
                    "delete_secret",
                    EmptyResponse,
                    SecretId=item.arn,
                    ForceDeleteWithoutRecovery=True,
                )
    for page in api.pages("s3", "list_buckets", Buckets):
        for item in page.buckets:
            if item.name != f"{run.prefix}-{run.account_id}":
                continue
            observed = api.call("s3", "get_bucket_tagging", BucketTags, Bucket=item.name)
            if not owned(observed.tags, run):
                raise ValueError("aws_load_test_bucket_ownership_mismatch")
            result.append(Residual(kind="s3-bucket", identifier=item.name, state="present"))
            if delete and item.name in ledger.identifiers("AWS::S3::Bucket"):
                empty_bucket(api, item.name)
                api.call("s3", "delete_bucket", EmptyResponse, Bucket=item.name)
    # 制御LambdaのgroupはStep Functionsが最後に消す。ここではworkloadだけを扱う。
    for page in api.pages(
        "logs", "describe_log_groups", LogGroups, logGroupNamePrefix=f"/gs-loadtest/{run.prefix}/"
    ):
        for item in page.groups:
            result.append(Residual(kind="log-group", identifier=item.name, state="present"))
            if delete and item.name in ledger.identifiers("AWS::Logs::LogGroup"):
                observed = api.call(
                    "logs",
                    "list_tags_for_resource",
                    LogTags,
                    resourceArn=f"arn:aws:logs:{run.region}:{run.account_id}:log-group:{item.name}",
                )
                tags = tuple(Tag(Key=key, Value=value) for key, value in observed.tags.items())
                if not ledger.permits("AWS::Logs::LogGroup", item.name, tags, run):
                    raise ValueError("aws_load_test_log_ownership_mismatch")
                api.call("logs", "delete_log_group", EmptyResponse, logGroupName=item.name)
    return result


def scan_control_storage(api: AwsApi, run: CleanupInput) -> tuple[Residual, ...]:
    """外部statusではworker自身の保存課金も含める。worker終端判定とは分離する。"""
    result: list[Residual] = []
    group_name = f"/aws/lambda/{run.prefix}-cleanup"
    for page in api.pages("logs", "describe_log_groups", LogGroups, logGroupNamePrefix=group_name):
        for item in page.groups:
            if item.name == group_name:
                result.append(
                    Residual(kind="control-log-group", identifier=item.name, state="present")
                )
    alarms = api.call(
        "cloudwatch",
        "describe_alarms",
        Alarms,
        AlarmNames=[f"{run.prefix}-deadline-dlq"],
        AlarmTypes=["MetricAlarm", "CompositeAlarm", "LogAlarm"],
    )
    if alarms.next_token:
        raise ValueError("aws_load_test_alarm_inventory_incomplete")
    result.extend(
        Residual(kind="control-alarm", identifier=item.name, state="present")
        for item in (*alarms.alarms, *alarms.composite_alarms, *alarms.log_alarms)
    )
    try:
        queue = api.call(
            "sqs",
            "get_queue_url",
            Queue,
            QueueName=f"{run.prefix}-deadline-dlq",
            QueueOwnerAWSAccountId=run.account_id,
        )
    except AwsCallError as error:
        if error.code not in {"QueueDoesNotExist", "AWS.SimpleQueueService.NonExistentQueue"}:
            raise
    else:
        result.append(Residual(kind="control-dlq", identifier=queue.url, state="present"))
    return tuple(result)


def scan_active_control(api: AwsApi, run: CleanupInput) -> tuple[Residual, ...]:
    result: list[Residual] = []
    for service, operation, model, parameters, absent in (
        (
            "lambda",
            "get_function_configuration",
            WorkerState,
            {"FunctionName": f"{run.prefix}-cleanup"},
            "ResourceNotFoundException",
        ),
        (
            "scheduler",
            "get_schedule",
            ActiveSchedule,
            {"GroupName": run.prefix, "Name": f"{run.prefix}-deadline"},
            "ResourceNotFoundException",
        ),
    ):
        try:
            response = api.call(service, operation, model, **parameters)
        except AwsCallError as error:
            if error.code == absent:
                continue
            raise
        if isinstance(response, WorkerState):
            result.append(
                Residual(kind="cleanup-worker-code", identifier=run.prefix, state=response.state)
            )
        elif isinstance(response, ActiveSchedule):
            result.append(
                Residual(kind="deadline-schedule", identifier=run.prefix, state=response.state)
            )
    return tuple(result)
