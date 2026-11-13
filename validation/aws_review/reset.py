"""既存の専用資源だけを照合し、配置済みcodeで開催データを初期化する。"""

from pydantic import BaseModel, ConfigDict, Field

from quiz_app.load_test_aws.aws import AwsApi, require_account
from validation.aws_review.models import ResetInput
from validation.aws_review.remote import run_host_command
from validation.aws_review.reset_remote import reset_host_commands
from validation.aws_review.stacks import get_stack


class ResetHost(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    instance_id: str = Field(pattern=r"^i-[a-f0-9]{17}$")


def reset_event(api: AwsApi, config: ResetInput) -> None:
    """AWS構成・source・imageを変更せず、所有確認済みEC2だけへ固定手順を送る。"""
    require_account(api, config.account_id)
    host: ResetHost | None = None
    for kind in ("control", "workload", "edge"):
        stack = get_stack(api, config, kind)
        if stack is None or stack.status not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
            raise ValueError("review_reset_requires_ready_existing_stacks")
        if kind == "workload":
            manual_review = [p.value for p in stack.parameters if p.key == "ManualReview"]
            if manual_review != ["true"]:
                raise ValueError("review_reset_requires_manual_review_environment")
            host = ResetHost(instance_id=stack.output("AppInstanceId"))
    if host is None:
        raise ValueError("review_reset_host_missing")
    run_host_command(api, host.instance_id, reset_host_commands(config), operation="reset-event")
