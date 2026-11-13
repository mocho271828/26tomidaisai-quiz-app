"""配布直前に削除期限を再確認し、有限のSSM commandだけを送る。"""

from __future__ import annotations

import base64
import shlex
import time
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, TypeAdapter

from quiz_app.load_test_aws.aws import (
    ApiModel,
    AwsApi,
    AwsCallError,
    EmptyResponse,
    require_account,
)
from quiz_app.load_test_aws.deployment_models import HOSTNAME, HostTopology
from quiz_app.load_test_aws.inventory import find_stack
from quiz_app.load_test_aws.lifecycle import (
    output,
    require_deadline,
    require_notification,
    require_stack_ready,
)
from quiz_app.load_test_aws.models import AwsRunPlan
from quiz_app.load_test_aws.responses import CommandResponse, Stack


class SecretResponse(ApiModel):
    value: SecretStr = Field(alias="SecretString")


class CommandInvocation(ApiModel):
    status: str = Field(alias="Status")
    response_code: int = Field(alias="ResponseCode")
    stdout: str = Field(alias="StandardOutputContent", max_length=24_000)


def deployment_preflight(api: AwsApi, plan: AwsRunPlan, *, now: datetime) -> Stack:
    """課金resourceへ触る前にaccount・期限・通知・stack所有を毎回照合する。"""
    plan.require_creation_window(now)
    require_account(api, plan.account_id)
    control = find_stack(api, plan.control_stack)
    workload = find_stack(api, plan.workload_stack)
    if control is None or workload is None:
        raise ValueError("aws_load_test_deployment_stack_missing")
    require_stack_ready(control, plan)
    require_stack_ready(workload, plan)
    require_notification(api, plan, control)
    require_deadline(api, plan, control)
    return workload


def upload(
    api: AwsApi, plan: AwsRunPlan, role: Literal["app", "runner"], body: bytes, suffix: str
) -> tuple[str, str]:
    if suffix not in {"tar.gz", "human.json", "ready.json"} or len(body) > 100 * 1024 * 1024:
        raise ValueError("aws_load_test_upload_invalid")
    digest = sha256(body).hexdigest()
    key = f"deployment/{role}/{digest}.{suffix}"
    try:
        api.call(
            "s3",
            "put_object",
            EmptyResponse,
            Bucket=f"{plan.prefix}-{plan.account_id}",
            Key=key,
            Body=body,
            ServerSideEncryption="AES256",
            IfNoneMatch="*",
            ChecksumSHA256=base64.b64encode(bytes.fromhex(digest)).decode(),
        )
    except AwsCallError as error:
        if error.code != "PreconditionFailed":
            raise

        # keyがcontent hashなので既存objectもchecksum readbackで同一性を確定する。
        class ObjectHead(ApiModel):
            checksum: str = Field(alias="ChecksumSHA256")
            length: int = Field(alias="ContentLength")

        observed = api.call(
            "s3",
            "head_object",
            ObjectHead,
            Bucket=f"{plan.prefix}-{plan.account_id}",
            Key=key,
            ChecksumMode="ENABLED",
        )
        if observed.checksum != base64.b64encode(
            bytes.fromhex(digest)
        ).decode() or observed.length != len(body):
            raise ValueError("aws_load_test_existing_deployment_object_mismatch") from None
    return key, digest


def command(api: AwsApi, instance_id: str, script: str, *, timeout: int = 900) -> str:
    """stdout以外のSSM出力は転送せず、失敗を固定codeに変換する。"""
    result = api.call(
        "ssm",
        "send_command",
        CommandResponse,
        DocumentName="AWS-RunShellScript",
        InstanceIds=[instance_id],
        TimeoutSeconds=timeout,
        Parameters={"commands": [script], "executionTimeout": [str(timeout)]},
        CloudWatchOutputConfig={"CloudWatchOutputEnabled": False},
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            observed = api.call(
                "ssm",
                "get_command_invocation",
                CommandInvocation,
                CommandId=result.command.identifier,
                InstanceId=instance_id,
            )
        except AwsCallError as error:
            if error.code != "InvocationDoesNotExist":
                raise
        else:
            if observed.status == "Success" and observed.response_code == 0:
                return observed.stdout
            if observed.status not in {"Pending", "InProgress", "Delayed"}:
                raise ValueError("aws_load_test_deployment_command_failed")
        time.sleep(2)
    raise ValueError("aws_load_test_deployment_command_timeout")


def install_host(
    api: AwsApi,
    plan: AwsRunPlan,
    stack: Stack,
    role: Literal["app", "runner"],
    body: bytes,
    repository: Path,
) -> None:
    key, digest = upload(api, plan, role, body, "tar.gz")
    arguments = (f"{plan.prefix}-{plan.account_id}", key, digest, str(plan.run_id), role)
    script = (repository / "infra/aws-load-test/deploy-host.sh").read_text()
    invocation = (
        "bash -s -- "
        + shlex.join(arguments)
        + " <<'GS_LOAD_TEST_DEPLOY_HOST'\n"
        + script
        + "\nGS_LOAD_TEST_DEPLOY_HOST"
    )
    command(
        api, output(stack, "AppInstanceId" if role == "app" else "RunnerInstanceId"), invocation
    )


def host_action(
    api: AwsApi,
    plan: AwsRunPlan,
    stack: Stack,
    role: Literal["app", "runner"],
    action: Literal["activate", "verify", "seal"],
    digest: str,
) -> str:
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("aws_load_test_deployment_digest_invalid")
    argv = (
        "/usr/sbin/runuser",
        "-u",
        "gsloadtest",
        "--",
        "/usr/bin/env",
        "HOME=/var/lib/gs-loadtest/home",
        "XDG_RUNTIME_DIR=/run/user/1001",
        "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus",
        "/opt/gs-loadtest/venv/bin/python",
        "-m",
        "quiz_app.load_test_aws.deployment_host",
        "--run-directory",
        f"/var/lib/gs-loadtest/{plan.run_id}",
        "--action",
        action,
        "--artifact-sha256",
        digest,
    )
    return command(
        api,
        output(stack, "AppInstanceId" if role == "app" else "RunnerInstanceId"),
        shlex.join(argv),
    )


class DiscoveredHost(ApiModel):
    hostname: HOSTNAME
    redis_addresses: tuple[str, ...]


def discover_topology(api: AwsApi, stack: Stack) -> HostTopology:
    """RDS/Redis endpointとapp側DNSを読み、手入力のprivate IPを不要にする。"""
    redis_endpoint = TypeAdapter(HOSTNAME).validate_python(output(stack, "RedisEndpoint"))
    script = (
        "import json,socket,sys; "
        "print(json.dumps({'hostname':socket.getfqdn(),'redis_addresses':sorted({"
        "row[4][0] for row in socket.getaddrinfo(sys.argv[1],6379,"
        "socket.AF_INET,socket.SOCK_STREAM)"
        "})}))"
    )
    observed = DiscoveredHost.model_validate_json(
        command(
            api,
            output(stack, "AppInstanceId"),
            shlex.join(("/usr/bin/python3", "-c", script, redis_endpoint)),
            timeout=30,
        )
    )
    if len(observed.redis_addresses) != 1:
        raise ValueError("aws_load_test_redis_unique_address_required")
    return HostTopology(
        app_hostname=observed.hostname,
        app_private_ip=output(stack, "AppPrivateIp"),
        runner_private_ip=output(stack, "RunnerPrivateIp"),
        redis_private_ip=observed.redis_addresses[0],
        database_endpoint=output(stack, "DatabaseEndpoint"),
        redis_endpoint=redis_endpoint,
    )
