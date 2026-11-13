"""GitHub/SSMへ固定phase・安全な発生位置・固定の設定案内だけを出力する。"""

import json
import sys
import traceback
from pathlib import Path
from typing import Literal, get_args

import logfire
from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from quiz_app.load_test_aws.aws import AwsCallError
from validation.aws_review.configuration_errors import ConfigurationIssue

ROOT = Path(__file__).resolve().parents[2]
FailureCode = Literal["review_host_failed", "aws_review_operation_failed"]
ReviewPhase = Literal[
    "deploy_start",
    "certificate_check",
    "control_ready",
    "workload_ready",
    "edge_prepared",
    "image_build",
    "images_ready",
    "source_uploaded",
    "host_preparing",
    "host_ready",
    "public_ready",
    "destroy_start",
    "destroy_progress",
    "destroy_verify",
    "destroy_complete",
    "stack_wait",
    "ssm_wait",
    "host_ownership",
    "host_stopping",
    "database_reset",
    "host_preflight",
    "host_starting",
]
StackKind = Literal["control", "workload", "edge"]
ProgressStep = Literal[
    "change_set",
    "stack",
    "ssm_connection",
    "ssm_command",
    "event_status_command",
    "resource_details",
]
ResourceName = Literal[
    "AppInstance",
    "Database",
    "Redis",
    "NatGateway",
    "VpcOrigin",
    "Distribution",
    "ArtifactBucket",
    "CloudFormationExecutionRole",
    "ApiRepository",
    "GatewayRepository",
    "ApplicationLogGroup",
]
RESOURCE_LABELS: dict[ResourceName, tuple[StackKind, str, str]] = {
    "AppInstance": ("workload", "AWS::EC2::Instance", "アプリ実行用EC2"),
    "Database": ("workload", "AWS::RDS::DBInstance", "PostgreSQLデータベース"),
    "Redis": ("workload", "AWS::ElastiCache::ReplicationGroup", "Redis"),
    "NatGateway": ("workload", "AWS::EC2::NatGateway", "外向き通信用NAT Gateway"),
    "VpcOrigin": ("edge", "AWS::CloudFront::VpcOrigin", "CloudFront VPCオリジン"),
    "Distribution": ("edge", "AWS::CloudFront::Distribution", "CloudFront配信"),
    "ArtifactBucket": ("control", "AWS::S3::Bucket", "配置資材用S3"),
    "CloudFormationExecutionRole": ("control", "AWS::IAM::Role", "配置実行用IAM role"),
    "ApiRepository": ("workload", "AWS::ECR::Repository", "API image用ECR"),
    "GatewayRepository": ("workload", "AWS::ECR::Repository", "gateway image用ECR"),
    "ApplicationLogGroup": ("workload", "AWS::Logs::LogGroup", "アプリログ保存先"),
}
ProgressState = Literal[
    "CREATE_PENDING",
    "CREATE_IN_PROGRESS",
    "CREATE_FAILED",
    "CREATE_COMPLETE",
    "DELETE_IN_PROGRESS",
    "DELETE_FAILED",
    "DELETE_COMPLETE",
    "DELETE_SKIPPED",
    "UPDATE_IN_PROGRESS",
    "UPDATE_FAILED",
    "UPDATE_COMPLETE",
    "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
    "UPDATE_ROLLBACK_IN_PROGRESS",
    "UPDATE_ROLLBACK_COMPLETE",
    "UPDATE_ROLLBACK_FAILED",
    "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS",
    "ROLLBACK_IN_PROGRESS",
    "ROLLBACK_COMPLETE",
    "ROLLBACK_FAILED",
    "REVIEW_IN_PROGRESS",
    "FAILED",
    "Pending",
    "InProgress",
    "Delayed",
    "Success",
    "Cancelled",
    "TimedOut",
    "Failed",
    "Cancelling",
    "Online",
    "ConnectionLost",
    "Inactive",
    "WAITING",
    "UNKNOWN",
    "UNAVAILABLE",
    "NO_CHANGES",
]


class ProgressContext(BaseModel):
    """進捗へ渡せる対象と状態を固定語彙に限定する。"""

    model_config = ConfigDict(
        extra="forbid", frozen=True, revalidate_instances="always", hide_input_in_errors=True
    )
    stack: StackKind
    step: ProgressStep
    resource: ResourceName | None = None
    state: ProgressState


def known_progress_state(value: str) -> ProgressState:
    """未知のAWS状態文字列は公開せず、固定の欠測表示へ変換する。"""
    return next((state for state in get_args(ProgressState) if state == value), "UNKNOWN")


_AWS_OPERATIONS = {
    "acm": frozenset({"describe_certificate"}),
    "cloudformation": frozenset(
        {
            "create_change_set",
            "delete_change_set",
            "delete_stack",
            "describe_change_set",
            "describe_stacks",
            "execute_change_set",
            "list_stack_resources",
            "list_stacks",
        }
    ),
    "cloudfront": frozenset({"get_distribution", "get_vpc_origin"}),
    "ec2": frozenset(
        {
            "delete_snapshot",
            "delete_volume",
            "describe_addresses",
            "describe_availability_zones",
            "describe_images",
            "describe_instances",
            "describe_internet_gateways",
            "describe_managed_prefix_lists",
            "describe_nat_gateways",
            "describe_network_interfaces",
            "describe_snapshots",
            "describe_volumes",
            "describe_vpcs",
            "stop_instances",
            "terminate_instances",
        }
    ),
    "ecr": frozenset({"describe_images", "describe_repositories", "get_authorization_token"}),
    "elasticache": frozenset(
        {
            "delete_snapshot",
            "describe_cache_clusters",
            "describe_cache_subnet_groups",
            "describe_replication_groups",
            "describe_snapshots",
            "list_tags_for_resource",
        }
    ),
    "iam": frozenset({"get_role", "get_instance_profile"}),
    "logs": frozenset({"describe_log_groups"}),
    "rds": frozenset(
        {
            "delete_db_instance_automated_backup",
            "delete_db_snapshot",
            "describe_db_instance_automated_backups",
            "describe_db_instances",
            "describe_db_snapshots",
            "describe_db_subnet_groups",
            "describe_orderable_db_instance_options",
            "list_tags_for_resource",
        }
    ),
    "s3": frozenset(
        {
            "abort_multipart_upload",
            "delete_objects",
            "get_bucket_tagging",
            "get_object",
            "head_bucket",
            "list_multipart_uploads",
            "list_object_versions",
            "list_objects_v2",
            "put_object",
        }
    ),
    "scheduler": frozenset({"get_schedule"}),
    "secretsmanager": frozenset({"describe_secret", "get_secret_value"}),
    "ssm": frozenset(
        {
            "cancel_command",
            "describe_instance_information",
            "get_command_invocation",
            "send_command",
        }
    ),
    "stepfunctions": frozenset({"describe_state_machine"}),
    "sts": frozenset({"get_caller_identity"}),
}
_HOST_AWS_OPERATIONS = frozenset(
    {
        "DescribeInstances",
        "DescribeDBInstances",
        "DescribeReplicationGroups",
        "DescribeStackResource",
        "ListTagsForResource",
        "DescribeSecret",
        "GetSecretValue",
    }
)
_AWS_ERROR_CODES = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "UnauthorizedOperation",
        "Throttling",
        "ThrottlingException",
        "RequestLimitExceeded",
        "ValidationError",
        "ValidationException",
        "ResourceNotFoundException",
        "InvalidClientTokenId",
        "ExpiredToken",
        "ExpiredTokenException",
        "IncompleteSignature",
        "SignatureDoesNotMatch",
        "UnrecognizedClientException",
        "DBInstanceNotFound",
        "DBInstanceNotFoundFault",
        "NoSuchEntity",
        "NoSuchBucket",
        "NoSuchKey",
        "ResourceNotFound",
        "InvalidInstanceID.NotFound",
        "InvocationDoesNotExist",
        "StackNotFoundException",
        "RepositoryNotFoundException",
        "ReplicationGroupNotFoundFault",
        "CacheSubnetGroupNotFoundFault",
        "DBSubnetGroupNotFoundFault",
        "NoSuchDistribution",
        "EntityNotFound",
        "StateMachineDoesNotExist",
    }
)
_ERROR_TYPES = frozenset(
    {
        "AwsCallError",
        "ClientError",
        "ValueError",
        "ValidationError",
        "OSError",
        "FileNotFoundError",
        "PermissionError",
        "TimeoutError",
        "BlockingIOError",
        "OperationalError",
        "InterfaceError",
        "CalledProcessError",
        "TimeoutExpired",
        "HostRefusedError",
        "RehearsalPreparationRefusedError",
        "DependencyHostValidationError",
        "AwsDemoDeploymentValidationError",
        "HTTPStatusError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "NoCredentialsError",
        "EndpointConnectionError",
    }
)


class _AwsError(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)
    code: str = Field(alias="Code")


class _AwsErrorResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)
    error: _AwsError = Field(alias="Error")


def _aws_details(error: BaseException) -> dict[str, str]:
    details: dict[str, str] = {}
    if isinstance(error, AwsCallError):
        if (
            isinstance(error.service, str)
            and isinstance(error.operation, str)
            and error.operation in _AWS_OPERATIONS.get(error.service, ())
        ):
            details = {"aws_service": error.service, "aws_operation": error.operation}
        code = error.code
    elif isinstance(error, ClientError):
        try:
            code = _AwsErrorResponse.model_validate(error.response).error.code
        except ValidationError:
            return {}
        if error.operation_name in _HOST_AWS_OPERATIONS:
            details["aws_operation"] = error.operation_name
    else:
        return {}
    if isinstance(code, str) and code in _AWS_ERROR_CODES:
        details["aws_code"] = code
    return details


def configure_logging() -> None:
    """spanが自動記録する生の例外も、consoleと外部exporterへ転送しない。"""
    logfire.configure(send_to_logfire=False, inspect_arguments=False, console=False)


def progress(phase: ReviewPhase, *, context: ProgressContext | None = None) -> None:
    """固定phaseと検証済みの対象・状態だけをJobの実行ログへflushする。"""
    if phase not in get_args(ReviewPhase):
        raise ValueError("review_phase_invalid")
    details: dict[str, str] = {}
    if context is not None:
        context = ProgressContext.model_validate(context)
        steps = {
            "change_set": "変更計画の準備",
            "stack": "AWSリソースの作成・更新" if phase == "stack_wait" else "AWSリソースの削除",
            "ssm_connection": "SSM接続の準備",
            "ssm_command": "アプリ配置・DB初期化の実行",
            "event_status_command": "Event ID・versionの読み取り",
            "resource_details": "詳細進捗取得不可（通常の完了判定を継続）",
        }
        details = context.model_dump(exclude_none=True)
        label = f"{context.stack} stack"
        if context.resource is not None:
            stack, resource_type, label = RESOURCE_LABELS[context.resource]
            if stack != context.stack:
                raise ValueError("review_progress_resource_stack_mismatch")
            details["resource_type"] = resource_type
            label = f"{label} ({context.resource})"
        operation = "変更不要" if context.state == "NO_CHANGES" else steps[context.step]
        details["message"] = f"{label}: {operation} / {context.state}"
    logfire.info("AWS review progress", phase=phase, **details)
    print(
        json.dumps({"status": "running", "phase": phase, **details}, ensure_ascii=False),
        file=sys.stderr,
        flush=True,
    )


def progress_unavailable(phase: ReviewPhase, kind: StackKind, error: BaseException) -> None:
    """任意の詳細照会の失敗だけでは配置・撤去を止めず、安全な理由を記録する。"""
    progress(
        phase,
        context=ProgressContext(stack=kind, step="resource_details", state="UNAVAILABLE"),
    )
    logfire.warning("AWS review detailed progress unavailable", **_aws_details(error))


def log_failure(
    code: FailureCode,
    error: BaseException,
    *,
    configuration_errors: tuple[ConfigurationIssue, ...] = (),
) -> None:
    """生例外を捨て、安全なstack位置と許可された診断情報だけを残す。"""
    safe_code = code if code in get_args(FailureCode) else "aws_review_operation_failed"
    type_name = type(error).__name__
    error_type = type_name if type_name in _ERROR_TYPES else "Error"
    aws_details = _aws_details(error)
    configuration_details = (
        {"configuration_errors": [issue.model_dump(mode="json") for issue in configuration_errors]}
        if configuration_errors
        else {}
    )
    locations: list[dict[str, str | int]] = []
    for frame, line in traceback.walk_tb(error.__traceback__):
        try:
            path = Path(frame.f_code.co_filename).resolve()
        except (OSError, RuntimeError):
            continue
        if not path.is_relative_to(ROOT):
            continue
        relative = path.relative_to(ROOT)
        if relative.parts and (
            relative.parts[0] in {"validation", "tests"}
            or relative.parts[:3]
            == (
                "backend",
                "src",
                "quiz_app",
            )
        ):
            locations.append({"module": str(relative), "line": line})
    locations = locations[-12:]
    # 元例外objectを再記録せず、検証用のstack情報だけをLogfireへ渡す。
    logfire.error(
        "AWS review execution failed",
        error_code=safe_code,
        error_type=error_type,
        stack=locations,
        **aws_details,
        **configuration_details,
    )
    try:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "code": safe_code,
                    "error_type": error_type,
                    "locations": locations,
                    **aws_details,
                    **configuration_details,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
            flush=True,
        )
    except (OSError, ValueError):
        logfire.warning("AWS review diagnostic output unavailable", error_code=safe_code)
