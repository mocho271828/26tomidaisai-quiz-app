"""固定SSM手順で専用hostへ検証済みsourceを配置する。"""

import base64
import shlex
import time
from pathlib import Path
from typing import Literal

from pydantic import Field

from quiz_app.load_test_aws.aws import ApiModel, AwsApi, AwsCallError, EmptyResponse
from validation.aws_review.host_contracts import HostInput
from validation.aws_review.remote_diagnostics import report_host_failure
from validation.aws_review.resource_progress import StackProgress


class Command(ApiModel):
    identifier: str = Field(alias="CommandId", pattern=r"^[0-9a-f-]{36}$")


class SentCommand(ApiModel):
    command: Command = Field(alias="Command")


class Invocation(ApiModel):
    status: str = Field(alias="Status")
    response_code: int = Field(alias="ResponseCode")
    standard_error: str = Field(default="", alias="StandardErrorContent", repr=False, exclude=True)
    standard_output: str = Field(
        default="", alias="StandardOutputContent", repr=False, exclude=True
    )


class ManagedInstance(ApiModel):
    identifier: str = Field(alias="InstanceId")
    status: str = Field(alias="PingStatus")


class ManagedInstances(ApiModel):
    instances: tuple[ManagedInstance, ...] = Field(alias="InstanceInformationList")


def host_commands(root: Path, config: HostInput, bucket: str, bundle_sha: str) -> str:
    """SSM本文には非秘密の配置情報と固定scriptだけを渡す。"""
    payload = base64.b64encode(config.model_dump_json().encode()).decode()
    prepare = base64.b64encode(
        (root / "scripts/aws-rehearsal-host-prepare.sh").read_bytes()
    ).decode()
    arguments = shlex.join(
        (
            bucket,
            f"{config.demo_id}/source.bundle",
            bundle_sha,
            config.source_commit,
            config.api_image,
        )
    )
    uri = shlex.quote(f"s3://{bucket}/{config.demo_id}/source.bundle")
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
exec 9>/run/lock/gs-review-deploy.lock
flock --nonblock 9
if systemctl cat gs-quiz-monitor.service >/dev/null 2>&1; then
  systemctl stop gs-quiz-monitor.service
fi
if [[ ! -f /etc/gs-app-tomidaisai/review-host-prepared ||
      ! -d /opt/gs-app-tomidaisai/.git ||
      ! -x /usr/local/bin/uv || ! -s /etc/gs-app-tomidaisai/rds-ca.pem ]]; then
  if [[ '{config.deployment_mode}' == update ]]; then
    echo review_update_requires_prepared_host >&2
    exit 2
  fi
  # create だけがhostの初回準備を行う。準備済みhostではcreate/updateともDBを初期化する。
  rm -rf /opt/gs-app-tomidaisai
  printf '%s' '{prepare}' | base64 --decode >/root/gs-review-host-prepare.sh
  bash /root/gs-review-host-prepare.sh {arguments}
  rm /root/gs-review-host-prepare.sh
  touch /etc/gs-app-tomidaisai/review-host-prepared
else
  aws --region ap-northeast-1 s3 cp {uri} /opt/review-source.bundle --only-show-errors
  printf '%s  %s\\n' '{bundle_sha}' /opt/review-source.bundle | sha256sum --check --status
  repo=/opt/gs-app-tomidaisai
  git -C "$repo" -c safe.directory="$repo" fetch \\
    /opt/review-source.bundle '{config.source_commit}'
  git -C "$repo" -c safe.directory="$repo" reset --hard '{config.source_commit}'
  chown -R ubuntu:ubuntu /opt/gs-app-tomidaisai
  rm /opt/review-source.bundle
  sudo -H -u ubuntu uv sync --project /opt/gs-app-tomidaisai/backend \\
    --python /usr/bin/python3 --frozen --no-dev
fi
# preflightはpullせず両imageのlabelを検証するため、毎回digest指定で先に取得する。
registry_config=$(mktemp -d)
trap 'rm -rf "$registry_config"' EXIT
aws --region ap-northeast-1 ecr get-login-password | \\
  docker --config "$registry_config" login --username AWS --password-stdin \\
    '{config.account_id}.dkr.ecr.ap-northeast-1.amazonaws.com'
docker --config "$registry_config" pull '{config.api_image}'
docker --config "$registry_config" pull '{config.gateway_image}'
install -d -o ubuntu -g ubuntu -m 0700 /etc/gs-app-tomidaisai
printf '%s' '{payload}' | base64 --decode >/etc/gs-app-tomidaisai/review-input.json
chown ubuntu:ubuntu /etc/gs-app-tomidaisai/review-input.json
chmod 0600 /etc/gs-app-tomidaisai/review-input.json
cd /opt/gs-app-tomidaisai
# root所有のbytecodeを生成すると、installer自身のsource所有者検証に失敗する。
/opt/gs-app-tomidaisai/backend/.venv/bin/python -B -m validation.aws_review.monitoring_install \\
  --release-input /etc/gs-app-tomidaisai/review-input.json
systemctl daemon-reload
sudo -H -u ubuntu uv run --no-sync --project backend python -m validation.aws_review.host \\
  --release-input /etc/gs-app-tomidaisai/review-input.json
systemctl enable gs-quiz-monitor.service
systemctl start gs-quiz-monitor.service
"""


def deploy_host(api: AwsApi, config: HostInput, script: str) -> None:
    run_host_command(api, config.instance_id, script)


def run_host_command(
    api: AwsApi,
    instance_id: str,
    script: str,
    *,
    operation: Literal["deploy", "reset-event", "event-status"] = "deploy",
) -> str:
    """SSM実行を有界待機する。成功stdoutは呼出側の検証用で、ここでは公開しない。"""
    execution_timeout, failure_code, comment = {
        "deploy": (2400, "review_host_deployment", "Deploy dedicated quiz review environment"),
        "reset-event": (
            600,
            "review_host_event_reset",
            "Reset event data in dedicated quiz review environment",
        ),
        "event-status": (
            120,
            "review_host_event_status",
            "Read event identity in dedicated quiz review environment",
        ),
    }[operation]
    command_step = "event_status_command" if operation == "event-status" else "ssm_command"
    reporter = StackProgress("workload", "ssm_wait")
    reporter.report("WAITING", step="ssm_connection", resource="AppInstance")
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        result = api.call(
            "ssm",
            "describe_instance_information",
            ManagedInstances,
            Filters=[{"Key": "InstanceIds", "Values": [instance_id]}],
        )
        for item in result.instances:
            if item.identifier == instance_id:
                reporter.report(item.status, step="ssm_connection", resource="AppInstance")
        if any(
            item.identifier == instance_id and item.status == "Online" for item in result.instances
        ):
            break
        time.sleep(10)
    else:
        raise TimeoutError("review_ssm_host_offline")
    sent = api.call(
        "ssm",
        "send_command",
        SentCommand,
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={
            "commands": ["bash -c " + shlex.quote(script)],
            "executionTimeout": [str(execution_timeout)],
        },
        TimeoutSeconds=120,
        Comment=comment,
    )
    deadline = time.monotonic() + execution_timeout + 180
    reporter.report("WAITING", step=command_step, resource="AppInstance")
    while time.monotonic() < deadline:
        try:
            result = api.call(
                "ssm",
                "get_command_invocation",
                Invocation,
                CommandId=sent.command.identifier,
                InstanceId=instance_id,
            )
        except AwsCallError as error:
            if error.code != "InvocationDoesNotExist":
                raise
        else:
            reporter.report(result.status, step=command_step, resource="AppInstance")
            if result.status == "Success" and result.response_code == 0:
                return result.standard_output
            if result.status not in {"Pending", "InProgress", "Delayed"}:
                report_host_failure(result.standard_error)
                raise ValueError(f"{failure_code}_failed")
        time.sleep(15)
    api.call(
        "ssm",
        "cancel_command",
        EmptyResponse,
        CommandId=sent.command.identifier,
        InstanceIds=[instance_id],
    )
    raise TimeoutError(f"{failure_code}_timeout")
