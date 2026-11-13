"""負荷試験の障害注入をmanifest・release・topologyへ固定する契約。"""

from __future__ import annotations

from datetime import datetime, timedelta
from hashlib import sha256
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

from quiz_app.load_test.controller_contracts import (
    ControllerContext,
    ControllerRefusal,
    canonical_json_bytes,
)
from quiz_app.load_test.manifest import LoadTestManifest
from quiz_app.load_test.verification_contracts import Sha256

FaultTarget = Literal["redis", "api-secondary"]
FaultAction = Literal["disable", "enable"]
FaultControllerFailureCode = Literal[
    "fault_action_failed",
    "fault_recovered_early",
    "fault_recovery_deadline_missed",
    "fault_recovery_fallback_applied",
    "fault_slot_missed",
    "fault_target_state_drift",
    "fault_timer_invalid",
]
FaultSlotNumber = Annotated[StrictInt, Field(ge=1, le=4)]
ReleaseId = Annotated[
    StrictStr,
    Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,63}$"),
]
ExecutionHost = Annotated[
    StrictStr,
    Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,251}[a-z0-9]$"),
]
DockerContext = Annotated[
    StrictStr,
    Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"),
]
SystemdUnitName = Annotated[
    StrictStr,
    Field(pattern=r"^[a-z0-9][a-z0-9_.@:-]{0,126}[a-z0-9]$"),
]
Endpoint = Annotated[StrictStr, Field(min_length=3, max_length=255)]


def fault_recovery_deadline(target: FaultTarget, scheduled_at: datetime) -> datetime:
    """target別の固定回復観測期限を返す。"""

    timeout_seconds = 15 if target == "redis" else 115
    return scheduled_at + timedelta(seconds=timeout_seconds)


def fault_disable_plan_deadline(scheduled_at: datetime) -> datetime:
    """固定slotを新規に計画できる5秒の遅延許容上限を返す。"""

    return scheduled_at + timedelta(seconds=5)


class StrictFaultControllerModel(BaseModel):
    """余剰fieldと暗黙変換を許さないimmutableな障害注入契約。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class FaultReleaseIdentity(StrictFaultControllerModel):
    """検証済みcandidate/rollback releaseの秘密を含まない同一性。"""

    release_id: ReleaseId
    release_manifest_sha256: Sha256
    source_commit: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{40}$")]
    rollback_release_id: ReleaseId
    rollback_manifest_sha256: Sha256
    execution_host: ExecutionHost
    docker_context: DockerContext

    @model_validator(mode="after")
    def validate_distinct_releases(self) -> Self:
        if self.release_id == self.rollback_release_id:
            raise ValueError("candidateとrollbackのrelease idは異なる必要があります")
        return self


class FaultTopologyIdentity(StrictFaultControllerModel):
    """検証済みRedis fault control/data pathの秘密を含まない同一性。"""

    configuration_sha256: Sha256
    docker_compose_version: Annotated[
        StrictStr,
        Field(pattern=r"^2\.[0-9]{1,3}\.[0-9]{1,3}$"),
    ]
    docker_engine_version: Annotated[
        StrictStr,
        Field(pattern=r"^[0-9]{2,3}\.[0-9]{1,3}\.[0-9]{1,3}$"),
    ]
    fault_control_network_sha256: Sha256
    toxiproxy_container_sha256: Sha256
    recovery_compose_sha256: Sha256
    probe_command_sha256: Sha256
    api_probe_command_sha256: Sha256
    proxy_name: Literal["gs-app-tomidaisai-aws-demo-redis"]
    control_origin: Literal["http://172.30.251.2:8474"]
    redis_hostname: Annotated[StrictStr, Field(min_length=3, max_length=253)]
    listen: Endpoint
    upstream: Endpoint


class FaultCommandIdentity(StrictFaultControllerModel):
    """一つのmanifest slotに対応する固定HTTP/Compose操作のdigest。"""

    slot_number: FaultSlotNumber
    target: FaultTarget
    action: FaultAction
    command_sha256: Sha256


class FaultControllerIdentity(StrictFaultControllerModel):
    """一つのfault journal全体から変更できない実行identity。"""

    context: ControllerContext
    release: FaultReleaseIdentity
    topology: FaultTopologyIdentity
    commands: tuple[
        FaultCommandIdentity,
        FaultCommandIdentity,
        FaultCommandIdentity,
        FaultCommandIdentity,
    ]

    @model_validator(mode="after")
    def validate_identity_relations(self) -> Self:
        if self.context.source_commit != self.release.source_commit:
            raise ValueError("manifestとreleaseのsource commitが一致しません")
        if tuple(command.slot_number for command in self.commands) != (1, 2, 3, 4):
            raise ValueError("command identityはmanifest slot順である必要があります")
        command_keys = {(command.target, command.action) for command in self.commands}
        if len(command_keys) != len(self.commands):
            raise ValueError("command identityのtarget/actionは重複できません")
        return self


class FaultTargetObservation(StrictFaultControllerModel):
    """adapterが正規化した可用状態と、状態変更では不変なtarget設定identity。"""

    target: FaultTarget
    enabled: StrictBool
    identity_sha256: Sha256


class FaultMutationResult(StrictFaultControllerModel):
    """一回の固定操作についてadapterが返す前後状態と再照合結果。"""

    before: FaultTargetObservation
    after: FaultTargetObservation
    reconciled: StrictBool

    @model_validator(mode="after")
    def validate_stable_target_identity(self) -> Self:
        if (
            self.before.target != self.after.target
            or self.before.identity_sha256 != self.after.identity_sha256
        ):
            raise ValueError("mutation前後でtarget identityを変更できません")
        return self


class RecoveryTimerPlan(StrictFaultControllerModel):
    """disable前に永続化する自動復旧timerの期待値。"""

    slot_number: FaultSlotNumber
    target: FaultTarget
    trigger_at: AwareDatetime
    unit_name: SystemdUnitName
    command_sha256: Sha256


class RecoveryTimerObservation(StrictFaultControllerModel):
    """systemd readbackから正規化したtimerの実状態identity。"""

    unit_name: SystemdUnitName
    trigger_at: AwareDatetime
    properties_sha256: Sha256


class RecoveryExecutionReceipt(StrictFaultControllerModel):
    """recovery serviceの正常終了後にhostが検証した実行receipt。"""

    unit_name: SystemdUnitName
    trigger_at: AwareDatetime
    recorded_at: AwareDatetime
    receipt_sha256: Sha256


def recovery_execution_receipt_sha256(
    plan: RecoveryTimerPlan,
    recorded_at: datetime,
) -> str:
    """receipt filesystem metadataをplanへ結合したdigestを返す。"""

    return sha256(
        canonical_json_bytes(
            {
                "format": "load-test-fault-recovery-ownership-v1",
                "recorded_at": recorded_at,
                "slot_number": plan.slot_number,
                "target": plan.target,
                "trigger_at": plan.trigger_at,
                "unit_name": plan.unit_name,
            }
        )
    ).hexdigest()


class FaultInitializedPayload(StrictFaultControllerModel):
    kind: Literal["fault_initialized"]
    identity: FaultControllerIdentity
    initial_observations: tuple[FaultTargetObservation, FaultTargetObservation]

    @model_validator(mode="after")
    def validate_initial_observations(self) -> Self:
        if tuple(value.target for value in self.initial_observations) != (
            "redis",
            "api-secondary",
        ) or not all(value.enabled for value in self.initial_observations):
            raise ValueError("初期観測は利用可能なRedis/API順である必要があります")
        return self


class RecoveryArmPlannedPayload(StrictFaultControllerModel):
    kind: Literal["recovery_arm_planned"]
    plan: RecoveryTimerPlan


class RecoveryArmedPayload(StrictFaultControllerModel):
    kind: Literal["recovery_armed"]
    plan: RecoveryTimerPlan
    observation: RecoveryTimerObservation

    @model_validator(mode="after")
    def validate_observation_matches_plan(self) -> Self:
        if (
            self.observation.unit_name != self.plan.unit_name
            or self.observation.trigger_at != self.plan.trigger_at
        ):
            raise ValueError("timer observationがplanと一致しません")
        return self


class FaultActionPlannedPayload(StrictFaultControllerModel):
    kind: Literal["fault_action_planned"]
    slot_number: FaultSlotNumber
    target: FaultTarget
    action: FaultAction
    scheduled_at: AwareDatetime
    command_sha256: Sha256
    expected_before: FaultTargetObservation

    @model_validator(mode="after")
    def validate_expected_target(self) -> Self:
        if self.expected_before.target != self.target:
            raise ValueError("pre-stateのtargetがactionと一致しません")
        if self.expected_before.enabled != (self.action == "disable"):
            raise ValueError("pre-stateがactionの期待状態と一致しません")
        return self


class FaultActionSucceededPayload(StrictFaultControllerModel):
    kind: Literal["fault_action_succeeded"]
    slot_number: FaultSlotNumber
    target: FaultTarget
    action: FaultAction
    planned_entry_sha256: Sha256
    observation: FaultTargetObservation
    observed_at: AwareDatetime
    reconciled: StrictBool
    recovery_receipt: RecoveryExecutionReceipt | None = None

    @model_validator(mode="after")
    def validate_observed_result(self) -> Self:
        if self.observation.target != self.target:
            raise ValueError("post-stateのtargetがactionと一致しません")
        if self.observation.enabled != (self.action == "enable"):
            raise ValueError("post-stateがactionの期待結果と一致しません")
        if (self.action == "enable") != (self.recovery_receipt is not None):
            raise ValueError("enable成功にはrecovery実行receiptが必要です")
        return self


class FaultControllerCompletedPayload(StrictFaultControllerModel):
    kind: Literal["completed"]
    final_action_entry_sha256: Sha256
    final_observations: tuple[FaultTargetObservation, FaultTargetObservation]
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_final_observations(self) -> Self:
        if tuple(observation.target for observation in self.final_observations) != (
            "redis",
            "api-secondary",
        ) or not all(observation.enabled for observation in self.final_observations):
            raise ValueError("完了時は両targetが利用可能である必要があります")
        return self


class FaultControllerFailedPayload(StrictFaultControllerModel):
    """正式runを失格にするterminal failure。入力値や例外本文は保持しない。"""

    kind: Literal["fault_controller_failed"]
    code: FaultControllerFailureCode
    slot_number: FaultSlotNumber | None
    target: FaultTarget | None


class FaultControllerStatus(StrictFaultControllerModel):
    """CLI stdoutへ出す秘密を含まないfault controller進捗。"""

    status: Literal["waiting", "finalizing", "completed", "failed"]
    next_slot: FaultSlotNumber | None
    journal_head_sha256: Sha256
    failure_code: FaultControllerFailureCode | None

    @model_validator(mode="after")
    def validate_status_fields(self) -> Self:
        if self.status == "waiting":
            if self.next_slot is None or self.failure_code is not None:
                raise ValueError("waiting statusにはnext slotだけが必要です")
        elif self.status in {"finalizing", "completed"}:
            if self.next_slot is not None or self.failure_code is not None:
                raise ValueError("terminal移行中・完了statusにslot・失敗fieldは指定できません")
        elif self.next_slot is not None or self.failure_code is None:
            raise ValueError("failed statusにはfailure codeだけが必要です")
        return self


FaultJournalPayload = Annotated[
    FaultInitializedPayload
    | RecoveryArmPlannedPayload
    | RecoveryArmedPayload
    | FaultActionPlannedPayload
    | FaultActionSucceededPayload
    | FaultControllerCompletedPayload
    | FaultControllerFailedPayload,
    Field(discriminator="kind"),
]


class FaultControllerJournalRecord(StrictFaultControllerModel):
    format: Literal["load-test-fault-controller-journal-v1"]
    sequence: StrictInt = Field(ge=1)
    previous_sha256: Sha256 | None
    recorded_at: AwareDatetime
    run_id: UUID
    event_id: UUID
    identity_sha256: Sha256
    payload: FaultJournalPayload
    entry_sha256: Sha256


def fault_recovery_offset_for_round(
    manifest: LoadTestManifest,
    round_number: int,
) -> int | None:
    """障害Roundのconditionから、同じtargetを復旧する唯一のoffsetを返す。"""

    if isinstance(round_number, bool) or not 1 <= round_number <= 10:
        raise ControllerRefusal("controller_round_invalid")
    matching_rounds = tuple(
        round_ for round_ in manifest.scenario.rounds if round_.number == round_number
    )
    if len(matching_rounds) != 1:
        raise ControllerRefusal("controller_round_invalid")
    target_by_condition: dict[str, FaultTarget] = {
        "redis-unavailable": "redis",
        "api-secondary-unavailable": "api-secondary",
    }
    target = target_by_condition.get(matching_rounds[0].condition)
    if target is None:
        return None
    recovery_actions = tuple(
        action
        for action in manifest.scenario.fault_schedule
        if action.target == target and action.action == "enable"
    )
    if len(recovery_actions) != 1:
        raise ControllerRefusal("controller_fault_schedule_invalid")
    return recovery_actions[0].offset_seconds
