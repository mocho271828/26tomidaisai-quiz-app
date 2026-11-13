"""既存のstack待機を補足し、主要リソースの状態を低頻度で表示する。"""

import time

from botocore.exceptions import BotoCoreError
from pydantic import Field, ValidationError

from quiz_app.load_test_aws.aws import ApiModel, AwsApi, AwsCallError
from validation.aws_review.diagnostics import (
    RESOURCE_LABELS,
    ProgressContext,
    ProgressStep,
    ResourceName,
    ReviewPhase,
    StackKind,
    known_progress_state,
    progress,
    progress_unavailable,
)

RESOURCE_POLL_SECONDS = 60


class ResourceStatus(ApiModel):
    logical_id: str = Field(alias="LogicalResourceId")
    resource_type: str = Field(alias="ResourceType")
    status: str = Field(alias="ResourceStatus")


class ResourceStatuses(ApiModel):
    resources: tuple[ResourceStatus, ...] = Field(alias="StackResourceSummaries")


class StackProgress:
    def __init__(self, kind: StackKind, phase: ReviewPhase = "stack_wait") -> None:
        self.kind = kind
        self.phase = phase
        self._next_poll = 0.0
        self._is_disabled = False
        self._seen: dict[tuple[ProgressStep, ResourceName | None], str] = {}

    def report(
        self, state: str, *, step: ProgressStep = "stack", resource: ResourceName | None = None
    ) -> None:
        safe_state = known_progress_state(state)
        key = (step, resource)
        if self._seen.get(key) == safe_state:
            return
        progress(
            self.phase,
            context=ProgressContext(
                stack=self.kind, step=step, resource=resource, state=safe_state
            ),
        )
        self._seen[key] = safe_state

    def resources(self, api: AwsApi, stack_arn: str) -> None:
        """呼出元で所有を確認したstackだけを、60秒以上間隔を空けて照会する。"""
        now = time.monotonic()
        if self._is_disabled or now < self._next_poll:
            return
        # 既存のListStackResources権限を使い、イベント本文や物理IDは読込モデルから除外する。
        self._next_poll = now + RESOURCE_POLL_SECONDS
        try:
            for page in api.pages(
                "cloudformation", "list_stack_resources", ResourceStatuses, StackName=stack_arn
            ):
                for item in page.resources:
                    for name, (kind, resource_type, _label) in RESOURCE_LABELS.items():
                        if (item.logical_id, item.resource_type, self.kind) == (
                            name,
                            resource_type,
                            kind,
                        ):
                            self.report(item.status, resource=name)
                            break
        except (AwsCallError, ValidationError, BotoCoreError) as error:
            # 表示用APIだけを止める。配置・撤去の必須APIや完了条件は呼出元で継続する。
            self._is_disabled = True
            progress_unavailable(self.phase, self.kind, error)
