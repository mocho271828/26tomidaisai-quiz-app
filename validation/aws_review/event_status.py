"""既存配置のEvent情報だけをSSM経由で取得し、初期化入力をSummaryへ示す。"""

import base64
import shlex
from datetime import UTC
from pathlib import Path

from pydantic import ValidationError

from quiz_app.load_test_aws.aws import AwsApi, require_account
from validation.aws_review.event_status_host import EventStatusRequest, EventStatusResult
from validation.aws_review.models import EventStatusInput
from validation.aws_review.remote import run_host_command
from validation.aws_review.stacks import get_stack


def event_status_commands(request: EventStatusRequest) -> str:
    """検証済みcheckoutの読取コードをメモリで実行し、配置sourceには書き込まない。"""
    source = Path(__file__).with_name("event_status_host.py").read_bytes()
    payload = base64.b64encode(source).decode("ascii")
    program = shlex.quote(
        "import base64; "
        f"exec(compile(base64.b64decode('{payload}', validate=True), "
        "'validation/aws_review/event_status_host.py', 'exec'))"
    )
    arguments = shlex.quote(request.model_dump_json())
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
exec 9>/run/lock/gs-review-deploy.lock
flock --nonblock 9
repo=/opt/gs-app-tomidaisai
python="$repo/backend/.venv/bin/python"
if [[ ! -f /etc/gs-app-tomidaisai/review-host-prepared ||
      ! -d "$repo/.git" || ! -x "$python" ||
      ! -f "$repo/validation/aws_review/host_reset.py" ]]; then
  echo review_event_status_requires_prepared_host >&2
  exit 2
fi
cd "$repo"
sudo -H -u ubuntu "$python" -B -c {program} --request-json {arguments}
"""


def get_event_status(api: AwsApi, config: EventStatusInput) -> EventStatusResult:
    """専用stackの所有確認後、単一hostの検証済み照会結果だけを返す。"""
    require_account(api, config.account_id)
    request: EventStatusRequest | None = None
    for kind in ("control", "workload", "edge"):
        stack = get_stack(api, config, kind)
        if stack is None or stack.status not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
            raise ValueError("review_event_status_requires_ready_existing_stacks")
        if kind == "workload":
            if [p.value for p in stack.parameters if p.key == "ManualReview"] != ["true"]:
                raise ValueError("review_event_status_requires_manual_review_environment")
            request = EventStatusRequest(
                account_id=config.account_id,
                demo_id=config.demo_id,
                region=config.region,
                instance_id=stack.output("AppInstanceId"),
            )
    if request is None:
        raise ValueError("review_event_status_host_missing")
    output = run_host_command(
        api, request.instance_id, event_status_commands(request), operation="event-status"
    )
    # SSMの生stdoutや余分なfieldをSummaryへ転送しない。
    if len(output) > 4096 or len(output.strip().splitlines()) != 1:
        raise ValueError("review_event_status_response_invalid")
    try:
        return EventStatusResult.model_validate_json(output)
    except ValidationError:
        raise ValueError("review_event_status_response_invalid") from None


def status_summary(result: EventStatusResult, workflow_commit: str) -> str:
    """Resetの入力名を添え、観測値と配置commitの状態を区別する。"""
    summary = (
        "\n## Event ID・version\n\n"
        "AWS Review Reset Event の入力欄へ、次の値をコピーしてください。\n\n"
        "| 入力欄 | 値 |\n| --- | --- |\n"
        f"| `expected_event_id` | `{result.event_id}` |\n"
        f"| `expected_event_version` | `{result.event_version}` |\n\n"
        f"- Event状態 (`event_state`): `{result.event_state.value}`\n"
        f"- 観測時刻 (`observed_at`, UTC): `{result.observed_at.astimezone(UTC).isoformat()}`\n"
        f"- 配置commit (`source_commit`): `{result.source_commit}`\n\n"
        "進行操作・初期化後はversionが変わるため、このworkflowで取り直してください。"
        "この確認では初期化を実行していません。\n"
    )
    if result.source_commit != workflow_commit:
        summary += (
            "\n**配置commitがこのworkflowのmainと異なります。** "
            "AWS Review Deployの`update`はDBを全初期化するため、"
            "現在のEvent ID・versionを使ってReset Eventへ進まないでください。\n"
        )
    return summary
