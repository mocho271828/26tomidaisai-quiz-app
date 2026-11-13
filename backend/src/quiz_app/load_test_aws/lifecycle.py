from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from datetime import datetime, timedelta
from typing import Literal

from pydantic import Field

from quiz_app.load_test.controller_contracts import manifest_context
from quiz_app.load_test.manifest import LoadTestManifest
from quiz_app.load_test_aws.alarms import require_deadline_alarm
from quiz_app.load_test_aws.aws import (
    ApiModel,
    AwsApi,
    AwsCallError,
    EmptyResponse,
    require_account,
)
from quiz_app.load_test_aws.bootstrap import ensure_execution_role
from quiz_app.load_test_aws.inventory import find_stack, owned
from quiz_app.load_test_aws.models import (
    RUN_SECONDS,
    AwsPreparationEvidence,
    AwsRunPlan,
    CleanupInput,
    SchemaReceipt,
    deadline_target,
    execution_input,
)
from quiz_app.load_test_aws.pricing import PricingEvidence, refresh_pricing
from quiz_app.load_test_aws.responses import (
    CommandResponse,
    Execution,
    Function,
    Stack,
    StackCreated,
    Subscriptions,
)
from quiz_app.load_test_aws.templates import parameters, tags, template_body

CLEANUP_WORKER_TIMEOUT_SECONDS = 120


class OperationResult(ApiModel):
    status: str
    resource: str
    pricing: PricingEvidence | None = None


class Image(ApiModel):
    identifier: str = Field(alias="ImageId")
    architecture: str = Field(alias="Architecture")
    state: str = Field(alias="State")
    root_device: str = Field(alias="RootDeviceType")
    root_device_name: str = Field(alias="RootDeviceName")
    owner: str = Field(alias="OwnerId")
    name: str = Field(alias="Name")


class Images(ApiModel):
    images: tuple[Image, ...] = Field(alias="Images")


class ExistingExecution(Execution):
    input: str


class DisabledSnapStart(ApiModel):
    apply_on: Literal["None"] = Field(alias="ApplyOn")


class FunctionConfiguration(ApiModel):
    function_name: str = Field(alias="FunctionName")
    state: str = Field(alias="State")
    code_sha256: str = Field(alias="CodeSha256")
    role: str = Field(alias="Role")
    handler: str = Field(alias="Handler")
    timeout: int = Field(alias="Timeout", strict=True, ge=1, le=900)
    version: str = Field(alias="Version")
    runtime: Literal["python3.12"] = Field(alias="Runtime")
    package_type: Literal["Zip"] = Field(alias="PackageType")
    architectures: tuple[Literal["x86_64"], ...] = Field(
        alias="Architectures", min_length=1, max_length=1
    )
    layers: tuple[()] = Field(default=(), alias="Layers")
    snap_start: DisabledSnapStart | None = Field(default=None, alias="SnapStart")
    durable_config: None = Field(default=None, alias="DurableConfig")
    capacity_provider_config: None = Field(default=None, alias="CapacityProviderConfig")


class FunctionConcurrency(ApiModel):
    reserved: int | None = Field(default=None, alias="ReservedConcurrentExecutions", strict=True)


class UnreservedAccountLimit(ApiModel):
    unreserved: int = Field(alias="UnreservedConcurrentExecutions", strict=True, gt=0)


class AccountSettings(ApiModel):
    limit: UnreservedAccountLimit = Field(alias="AccountLimit")


class NoProvisionedConcurrency(ApiModel):
    configurations: tuple[()] = Field(alias="ProvisionedConcurrencyConfigs")
    next_marker: None = Field(default=None, alias="NextMarker")


class DeadlineRetryPolicy(ApiModel):
    maximum_event_age: int = Field(alias="MaximumEventAgeInSeconds")
    maximum_attempts: int = Field(alias="MaximumRetryAttempts")


class DeadlineLetterQueue(ApiModel):
    arn: str = Field(alias="Arn")


class DeadlineTimeWindow(ApiModel):
    mode: str = Field(alias="Mode")


class SchedulerTarget(ApiModel):
    arn: str = Field(alias="Arn")
    input: str = Field(alias="Input")
    role_arn: str = Field(alias="RoleArn")
    retry_policy: DeadlineRetryPolicy = Field(alias="RetryPolicy")
    dead_letter_queue: DeadlineLetterQueue = Field(alias="DeadLetterConfig")


class Schedule(ApiModel):
    state: str = Field(alias="State")
    expression: str = Field(alias="ScheduleExpression")
    timezone: str = Field(alias="ScheduleExpressionTimezone")
    time_window: DeadlineTimeWindow = Field(alias="FlexibleTimeWindow")
    target: SchedulerTarget = Field(alias="Target")
    action_after_completion: str = Field(default="NONE", alias="ActionAfterCompletion")


class IamRole(ApiModel):
    arn: str = Field(alias="Arn")


class IamRoleResponse(ApiModel):
    role: IamRole = Field(alias="Role")


def output(stack: Stack, key: str) -> str:
    values = [item.value for item in stack.outputs if item.key == key]
    if len(values) != 1:
        raise ValueError("aws_load_test_stack_output_invalid")
    return values[0]


def require_manifest(plan: AwsRunPlan, manifest_bytes: bytes) -> LoadTestManifest:
    """料金資源作成前に既存v3契約で全image digestとrun/sourceを確定する。"""
    if not 0 < len(manifest_bytes) <= 1_000_000:
        raise ValueError("aws_load_test_manifest_size_invalid")
    manifest = LoadTestManifest.model_validate_json(manifest_bytes)
    if (
        manifest_context(manifest).manifest_sha256 != plan.manifest_sha256
        or manifest.run_id != plan.run_id
        or manifest.artifacts.source_commit != plan.source_commit
        or manifest.schema_revision != plan.schema_revision
        or manifest.public_origin != "https://10.77.1.10"
    ):
        raise ValueError("aws_load_test_manifest_binding_mismatch")
    return manifest


def preflight(
    api: AwsApi, plan: AwsRunPlan, *, now: datetime, manifest_bytes: bytes
) -> PricingEvidence:
    """認証・account・時刻・AMIを読み取りで検証する。"""
    manifest = require_manifest(plan, manifest_bytes)
    plan.require_creation_window(now)
    if (
        manifest.scheduled_start_at <= now + timedelta(seconds=30)
        or manifest.scheduled_start_at < plan.created_at
        or manifest.scheduled_start_at + timedelta(seconds=RUN_SECONDS + 600) > plan.expires_at
    ):
        raise ValueError("aws_load_test_manifest_start_window_invalid")
    require_account(api, plan.account_id)
    if plan.cloudformation_role_arn is not None:
        role_name = plan.cloudformation_role_arn.rsplit("/", 1)[-1]
        role = api.call("iam", "get_role", IamRoleResponse, RoleName=role_name)
        if role.role.arn != plan.cloudformation_role_arn:
            raise ValueError("aws_load_test_cloudformation_role_mismatch")
    images = api.call(
        "ec2", "describe_images", Images, ImageIds=[plan.ami_id], Owners=["099720109477"]
    )
    if len(images.images) != 1:
        raise ValueError("aws_load_test_ami_not_found")
    image = images.images[0]
    if image.owner != "099720109477" or not image.name.startswith(
        "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-"
    ):
        raise ValueError("aws_load_test_ubuntu_2404_required")
    if (
        image.identifier,
        image.architecture,
        image.state,
        image.root_device,
        image.root_device_name,
    ) != (
        plan.ami_id,
        "x86_64",
        "available",
        "ebs",
        "/dev/sda1",
    ):
        raise ValueError("aws_load_test_ami_invalid")
    pricing = refresh_pricing(api, now=now)
    pricing.assert_within_budget(now=now)
    return pricing


def create_stack(api: AwsApi, plan: AwsRunPlan, kind: str) -> StackCreated:
    return api.call(
        "cloudformation",
        "create_stack",
        StackCreated,
        StackName=plan.control_stack if kind == "control" else plan.workload_stack,
        TemplateBody=template_body(kind),
        Parameters=parameters(plan, kind),
        RoleARN=plan.execution_role_arn,
        Capabilities=["CAPABILITY_NAMED_IAM"],
        OnFailure="DELETE",
        TimeoutInMinutes=30,
        EnableTerminationProtection=False,
        Tags=tags(plan),
        ClientRequestToken=f"{plan.run_id}-{kind}",
    )


def require_stack_ready(stack: Stack, plan: AwsRunPlan) -> None:
    if not owned(stack.tags, plan.cleanup_input()):
        raise ValueError("aws_load_test_stack_ownership_mismatch")
    # 課金資源作成前の制御定義の補正は許すが、試験環境の再構築を開始条件にしない。
    ready_statuses = {"CREATE_COMPLETE"}
    if stack.name == plan.control_stack:
        ready_statuses.add("UPDATE_COMPLETE")
    if stack.status not in ready_statuses:
        raise ValueError("aws_load_test_stack_not_ready")


def require_notification(api: AwsApi, plan: AwsRunPlan, control: Stack) -> None:
    topic = output(control, "NotificationTopicArn")
    confirmed = False
    for page in api.pages("sns", "list_subscriptions_by_topic", Subscriptions, TopicArn=topic):
        for item in page.subscriptions:
            if (
                item.protocol == "email"
                and item.endpoint == plan.notification_email.get_secret_value()
            ):
                confirmed = item.arn.startswith("arn:aws:sns:")
    if not confirmed:
        raise ValueError("aws_load_test_notification_subscription_unconfirmed")


def require_deadline(
    api: AwsApi, plan: AwsRunPlan, control: Stack, *, arm_auto_delete: bool = False
) -> None:
    observed = api.call(
        "scheduler",
        "get_schedule",
        Schedule,
        Name=f"{plan.prefix}-deadline",
        GroupName=plan.prefix,
    )
    expected_input = deadline_target(plan.cleanup_input())
    if (
        observed.state != "ENABLED"
        or observed.expression != plan.expires_at.strftime("at(%Y-%m-%dT%H:%M:%S)")
        or observed.timezone != "UTC"
        or observed.time_window.mode != "OFF"
        or observed.target.arn != "arn:aws:scheduler:::aws-sdk:sfn:startExecution"
        or observed.target.role_arn != output(control, "SchedulerRoleArn")
        or observed.target.dead_letter_queue.arn != output(control, "DeadlineQueueArn")
        or observed.target.retry_policy.maximum_event_age != 3600
        or observed.target.retry_policy.maximum_attempts != 20
        or json.loads(observed.target.input) != expected_input
    ):
        raise ValueError("aws_load_test_deadline_not_armed")
    if observed.action_after_completion == "DELETE":
        return
    if not arm_auto_delete:
        raise ValueError("aws_load_test_deadline_auto_delete_missing")
    # CloudFormationのSchedule型にはこのAPI propertyがないため、作成前readback付きで設定する。
    api.call(
        "scheduler",
        "update_schedule",
        EmptyResponse,
        Name=f"{plan.prefix}-deadline",
        GroupName=plan.prefix,
        ScheduleExpression=observed.expression,
        ScheduleExpressionTimezone="UTC",
        State="ENABLED",
        ActionAfterCompletion="DELETE",
        FlexibleTimeWindow={"Mode": "OFF"},
        Target={
            "Arn": "arn:aws:scheduler:::aws-sdk:sfn:startExecution",
            "RoleArn": output(control, "SchedulerRoleArn"),
            "Input": json.dumps(expected_input, separators=(",", ":")),
            "RetryPolicy": {"MaximumEventAgeInSeconds": 3600, "MaximumRetryAttempts": 20},
            "DeadLetterConfig": {"Arn": output(control, "DeadlineQueueArn")},
        },
    )
    require_deadline(api, plan, control)


def validate_worker_package(body: bytes, expected_sha256: str) -> None:
    if (
        not 1 <= len(body) <= 50 * 1024 * 1024
        or hashlib.sha256(body).hexdigest() != expected_sha256
    ):
        raise ValueError("aws_load_test_worker_package_mismatch")
    with zipfile.ZipFile(io.BytesIO(body)) as package:
        names = set(package.namelist())
        required = {
            "quiz_app/__init__.py",
            "quiz_app/load_test_aws/cleanup.py",
            "pydantic/__init__.py",
            "boto3/__init__.py",
            "logfire/__init__.py",
        }
        if not required <= names or not any(
            name.startswith("pydantic_core/")
            and "cpython-312-x86_64-linux" in name
            and name.endswith(".so")
            for name in names
        ):
            raise ValueError("aws_load_test_worker_linux_dependencies_missing")
        if sum(item.file_size for item in package.infolist()) > 250 * 1024 * 1024:
            raise ValueError("aws_load_test_worker_uncompressed_size_exceeded")
        if any(name.startswith("/") or ".." in name.split("/") for name in names):
            raise ValueError("aws_load_test_worker_archive_path_invalid")


def require_worker(api: AwsApi, plan: AwsRunPlan, control: Stack) -> None:
    """workload作成・開始の両境界で、停止待機の前提となるworker設定を再照会する。"""
    worker_name = f"{plan.prefix}-cleanup"
    worker = api.call(
        "lambda", "get_function_configuration", FunctionConfiguration, FunctionName=worker_name
    )
    if (
        worker.function_name != worker_name
        or worker.state != "Active"
        or worker.code_sha256
        != base64.b64encode(bytes.fromhex(plan.worker_package_sha256)).decode()
        or worker.role != output(control, "WorkerRoleArn")
        or worker.handler != "quiz_app.load_test_aws.cleanup.handler"
        or worker.timeout != CLEANUP_WORKER_TIMEOUT_SECONDS
        or worker.version != "$LATEST"
    ):
        raise ValueError("aws_load_test_worker_not_ready")
    # 呼出し対象の$LATESTだけでなく、課金される別versionの予約環境も拒否する。
    api.call(
        "lambda",
        "list_provisioned_concurrency_configs",
        NoProvisionedConcurrency,
        FunctionName=worker_name,
    )
    concurrency = api.call(
        "lambda", "get_function_concurrency", FunctionConcurrency, FunctionName=worker_name
    )
    if concurrency.reserved is not None:
        raise ValueError("aws_load_test_worker_unreserved_required")
    # 未予約枠の存在を確認する。現在の空きや他functionとの競合解消を保証する値ではない。
    api.call("lambda", "get_account_settings", AccountSettings)


def prepare(
    api: AwsApi, plan: AwsRunPlan, worker_package: bytes, *, now: datetime, manifest_bytes: bytes
) -> OperationResult:
    """guardと購読とworkerが揃うまでworkload作成を拒否する段階的prepare。"""
    pricing = preflight(api, plan, now=now, manifest_bytes=manifest_bytes)
    validate_worker_package(worker_package, plan.worker_package_sha256)
    if ensure_execution_role(api, plan):
        return OperationResult(
            status="execution_role_created", resource=plan.execution_role_arn, pricing=pricing
        )
    control = find_stack(api, plan.control_stack)
    if control is None:
        created = create_stack(api, plan, "control")
        return OperationResult(
            status="control_creating", resource=created.stack_id, pricing=pricing
        )
    require_stack_ready(control, plan)
    require_notification(api, plan, control)
    require_deadline(api, plan, control, arm_auto_delete=True)
    require_deadline_alarm(api, plan, output(control, "NotificationTopicArn"), create_missing=True)
    worker_name = f"{plan.prefix}-cleanup"
    try:
        api.call(
            "lambda", "get_function_configuration", FunctionConfiguration, FunctionName=worker_name
        )
    except AwsCallError as error:
        if error.code != "ResourceNotFoundException":
            raise
        created_worker = api.call(
            "lambda",
            "create_function",
            Function,
            FunctionName=worker_name,
            Runtime="python3.12",
            Role=output(control, "WorkerRoleArn"),
            Handler="quiz_app.load_test_aws.cleanup.handler",
            Code={"ZipFile": worker_package},
            Timeout=CLEANUP_WORKER_TIMEOUT_SECONDS,
            MemorySize=512,
            Publish=True,
            Architectures=["x86_64"],
            Environment={
                "Variables": {
                    "LOAD_TEST_RUN_ID": str(plan.run_id),
                    "LOAD_TEST_ACCOUNT_ID": plan.account_id,
                }
            },
            Tags={tag["Key"]: tag["Value"] for tag in tags(plan)},
        )
        return OperationResult(
            status="worker_creating", resource=created_worker.arn, pricing=pricing
        )
    require_worker(api, plan, control)
    workload = find_stack(api, plan.workload_stack)
    if workload is None:
        created = create_stack(api, plan, "workload")
        return OperationResult(
            status="workload_creating", resource=created.stack_id, pricing=pricing
        )
    require_stack_ready(workload, plan)
    return OperationResult(
        status="awaiting_human_schema_and_deployment", resource=workload.stack_id, pricing=pricing
    )


def start(
    api: AwsApi,
    plan: AwsRunPlan,
    preparation: AwsPreparationEvidence,
    schema: SchemaReceipt,
    *,
    now: datetime,
    manifest_bytes: bytes,
) -> CommandResponse:
    preflight(api, plan, now=now, manifest_bytes=manifest_bytes)
    if (
        preparation.run_id != plan.run_id
        or preparation.account_id != plan.account_id
        or preparation.source_commit != plan.source_commit
        or preparation.manifest_sha256 != plan.manifest_sha256
        or preparation.expires_at != plan.expires_at
        or schema.run_id != plan.run_id
        or schema.account_id != plan.account_id
        or schema.schema_revision != plan.schema_revision
        or not plan.created_at <= schema.verified_at <= now
        or not plan.created_at <= preparation.prepared_at <= now
        or now + timedelta(seconds=RUN_SECONDS + 600) >= plan.expires_at
    ):
        raise ValueError("aws_load_test_preparation_or_schema_mismatch")
    control = find_stack(api, plan.control_stack)
    workload = find_stack(api, plan.workload_stack)
    if control is None or workload is None:
        raise ValueError("aws_load_test_stacks_missing")
    require_stack_ready(control, plan)
    require_stack_ready(workload, plan)
    require_notification(api, plan, control)
    require_deadline(api, plan, control)
    require_deadline_alarm(api, plan, output(control, "NotificationTopicArn"))
    require_worker(api, plan, control)
    expected = {
        "RunnerInstanceId": preparation.runner_instance_id,
        "AppInstanceId": preparation.app_instance_id,
        "RunnerPrivateIp": str(preparation.runner_private_ip),
        "AppPrivateIp": str(preparation.app_private_ip),
    }
    if any(output(workload, key) != value for key, value in expected.items()):
        raise ValueError("aws_load_test_host_binding_mismatch")
    return api.call(
        "ssm",
        "send_command",
        CommandResponse,
        InstanceIds=[preparation.runner_instance_id],
        DocumentName=output(control, "RunDocumentName"),
        TimeoutSeconds=60,
        MaxConcurrency="1",
        MaxErrors="0",
        CloudWatchOutputConfig={"CloudWatchOutputEnabled": False},
    )


def request_cleanup(api: AwsApi, run: CleanupInput) -> Execution:
    """成功・失敗・signalの全経路から同じAWS側の有限cleanupを要求する。"""
    require_account(api, run.account_id)
    payload = execution_input(run)
    try:
        return api.call(
            "stepfunctions",
            "start_execution",
            Execution,
            stateMachineArn=f"arn:aws:states:{run.region}:{run.account_id}:stateMachine:{run.prefix}-cleanup",
            name="cleanup",
            input=payload,
        )
    except AwsCallError as error:
        if error.code != "ExecutionAlreadyExists":
            raise
        expected_arn = (
            f"arn:aws:states:{run.region}:{run.account_id}:execution:{run.prefix}-cleanup:cleanup"
        )
        existing = api.call(
            "stepfunctions",
            "describe_execution",
            ExistingExecution,
            executionArn=expected_arn,
        )
        if existing.input != payload or existing.arn != expected_arn:
            raise ValueError("aws_load_test_cleanup_execution_input_mismatch") from None
        return Execution(arn=existing.arn, started_at=existing.started_at, status=existing.status)
