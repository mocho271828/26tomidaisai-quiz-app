"""障害注入journalをmanifest由来の進行状態へ還元する。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

from quiz_app.load_test.controller_contracts import ControllerRefusal
from quiz_app.load_test.fault_controller_contracts import (
    FaultAction,
    FaultActionPlannedPayload,
    FaultActionSucceededPayload,
    FaultControllerCompletedPayload,
    FaultControllerFailedPayload,
    FaultControllerIdentity,
    FaultControllerJournalRecord,
    FaultControllerStatus,
    FaultInitializedPayload,
    FaultTarget,
    FaultTargetObservation,
    RecoveryArmedPayload,
    RecoveryArmPlannedPayload,
    RecoveryTimerObservation,
    RecoveryTimerPlan,
    fault_disable_plan_deadline,
    fault_recovery_deadline,
    recovery_execution_receipt_sha256,
)
from quiz_app.load_test.manifest import LoadTestManifest


@dataclass(frozen=True, slots=True)
class FaultSlot:
    number: int
    target: FaultTarget
    action: FaultAction
    scheduled_at: datetime
    command_sha256: str


@dataclass(slots=True)
class FaultProgress:
    initialized: FaultControllerJournalRecord | None
    arm_plans: dict[int, FaultControllerJournalRecord]
    armed: dict[int, FaultControllerJournalRecord]
    action_plans: dict[int, FaultControllerJournalRecord]
    successes: dict[int, FaultControllerJournalRecord]
    completed: FaultControllerJournalRecord | None
    failed: FaultControllerJournalRecord | None


def build_fault_slots(
    manifest: LoadTestManifest,
    identity: FaultControllerIdentity,
) -> tuple[FaultSlot, FaultSlot, FaultSlot, FaultSlot]:
    actions = manifest.scenario.fault_schedule
    if len(actions) != 4 or len(identity.commands) != 4:
        raise ControllerRefusal("fault_schedule_invalid")
    slots: list[FaultSlot] = []
    pairs = zip(actions, identity.commands, strict=True)
    for number, (action, command) in enumerate(pairs, start=1):
        if (
            command.slot_number != number
            or command.target != action.target
            or command.action != action.action
        ):
            raise ControllerRefusal("fault_command_identity_mismatch")
        slots.append(
            FaultSlot(
                number=number,
                target=action.target,
                action=action.action,
                scheduled_at=manifest.scheduled_start_at + timedelta(seconds=action.offset_seconds),
                command_sha256=command.command_sha256,
            )
        )
    return cast(tuple[FaultSlot, FaultSlot, FaultSlot, FaultSlot], tuple(slots))


def build_timer_plan(slot: FaultSlot, identity: FaultControllerIdentity) -> RecoveryTimerPlan:
    if slot.action != "enable":
        raise ControllerRefusal("fault_timer_target_invalid")
    return RecoveryTimerPlan(
        slot_number=slot.number,
        target=slot.target,
        trigger_at=slot.scheduled_at,
        unit_name=f"gs-loadtest-{identity.context.run_id.hex}-{slot.target}-recover",
        command_sha256=slot.command_sha256,
    )


def require_timer_observation(
    plan: RecoveryTimerPlan,
    observation: RecoveryTimerObservation,
) -> None:
    if observation.unit_name != plan.unit_name or observation.trigger_at != plan.trigger_at:
        raise ControllerRefusal("fault_timer_invalid")


def _planned_record_for_success(
    slot: FaultSlot,
    progress: FaultProgress,
) -> FaultControllerJournalRecord:
    record = (
        progress.arm_plans.get(slot.number)
        if slot.action == "enable"
        else progress.action_plans.get(slot.number)
    )
    if record is None:
        raise ControllerRefusal("fault_journal_state_invalid")
    return record


def baseline_observation(
    slot: FaultSlot,
    progress: FaultProgress,
) -> FaultTargetObservation:
    return initial_observation(slot.target, progress)


def initial_observation(
    target: FaultTarget,
    progress: FaultProgress,
) -> FaultTargetObservation:
    record = progress.initialized
    if record is None or not isinstance(record.payload, FaultInitializedPayload):
        raise ControllerRefusal("fault_journal_state_invalid")
    return next(
        observation
        for observation in record.payload.initial_observations
        if observation.target == target
    )


def _require_failure_reachable(
    record: FaultControllerJournalRecord,
    payload: FaultControllerFailedPayload,
    progress: FaultProgress,
    slots: tuple[FaultSlot, FaultSlot, FaultSlot, FaultSlot],
) -> None:
    """terminal failureが、直前までの進行状態から実際に到達可能か検証する。"""

    success_count = len(progress.successes)
    if set(progress.successes) != set(range(1, success_count + 1)):
        raise ControllerRefusal("fault_journal_state_invalid")

    if payload.slot_number is None:
        is_initial_drift = (
            payload.code == "fault_target_state_drift"
            and payload.target is not None
            and not progress.arm_plans
            and not progress.armed
            and not progress.action_plans
            and not progress.successes
        )
        is_final_drift = (
            payload.code == "fault_target_state_drift"
            and payload.target is not None
            and success_count == len(slots)
        )
        if not (is_initial_drift or is_final_drift):
            raise ControllerRefusal("fault_journal_state_invalid")
        return

    slot = slots[payload.slot_number - 1]
    is_concurrent_target_drift = (
        payload.code == "fault_target_state_drift"
        and slot.action == "disable"
        and payload.target is not None
        and payload.target != slot.target
    )
    if payload.target != slot.target and not is_concurrent_target_drift:
        raise ControllerRefusal("fault_journal_state_invalid")

    is_current_slot = success_count == slot.number - 1
    both_recoveries_armed = set(progress.armed) == {2, 4}
    if payload.code == "fault_slot_missed":
        is_initial_slot = (
            slot.number == 1
            and not progress.arm_plans
            and not progress.armed
            and not progress.action_plans
            and not progress.successes
            and record.recorded_at > slot.scheduled_at - timedelta(seconds=30)
        )
        is_recovery_arm = (
            slot.action == "enable"
            and not progress.action_plans
            and not progress.successes
            and record.recorded_at >= slot.scheduled_at - timedelta(seconds=30)
        )
        is_active_disable = (
            slot.action == "disable"
            and is_current_slot
            and both_recoveries_armed
            and record.recorded_at >= slot.scheduled_at
        )
        if not (is_initial_slot or is_recovery_arm or is_active_disable):
            raise ControllerRefusal("fault_journal_state_invalid")
        return

    if payload.code == "fault_timer_invalid":
        if (
            slot.action != "enable"
            or slot.number not in progress.armed
            or success_count >= slot.number
        ):
            raise ControllerRefusal("fault_journal_state_invalid")
        return

    if payload.code == "fault_target_state_drift":
        if (
            not is_current_slot
            or not both_recoveries_armed
            or record.recorded_at < slot.scheduled_at
        ):
            raise ControllerRefusal("fault_journal_state_invalid")
        return

    if payload.code == "fault_action_failed":
        if (
            slot.action != "disable"
            or not is_current_slot
            or not both_recoveries_armed
            or slot.number not in progress.action_plans
            or record.recorded_at < slot.scheduled_at
        ):
            raise ControllerRefusal("fault_journal_state_invalid")
        return

    if payload.code == "fault_recovered_early":
        preceding_disable = slot.number - 1
        if (
            slot.action != "enable"
            or not is_current_slot
            or not both_recoveries_armed
            or preceding_disable not in progress.action_plans
            or preceding_disable not in progress.successes
            or record.recorded_at >= slot.scheduled_at
        ):
            raise ControllerRefusal("fault_journal_state_invalid")
        return

    if payload.code in {
        "fault_recovery_deadline_missed",
        "fault_recovery_fallback_applied",
    }:
        preceding_disable = slot.number - 1
        if (
            slot.action != "enable"
            or not is_current_slot
            or not both_recoveries_armed
            or preceding_disable not in progress.action_plans
            or preceding_disable not in progress.successes
            or record.recorded_at < fault_recovery_deadline(slot.target, slot.scheduled_at)
        ):
            raise ControllerRefusal("fault_journal_state_invalid")
        return

    raise ControllerRefusal("fault_journal_state_invalid")


def reduce_fault_records(
    records: Sequence[FaultControllerJournalRecord],
    identity: FaultControllerIdentity,
    slots: tuple[FaultSlot, FaultSlot, FaultSlot, FaultSlot],
) -> FaultProgress:
    """journalの意味順序を検証し、crash resume用の進行状態を返す。"""

    progress = FaultProgress(None, {}, {}, {}, {}, None, None)
    slot_by_number = {slot.number: slot for slot in slots}
    terminal_seen = False
    previous_recorded_at: datetime | None = None

    for record in records:
        timestamp_reversed = (
            previous_recorded_at is not None and record.recorded_at < previous_recorded_at
        )
        if terminal_seen or timestamp_reversed:
            raise ControllerRefusal("fault_journal_state_invalid")
        previous_recorded_at = record.recorded_at
        payload = record.payload
        if isinstance(payload, FaultInitializedPayload):
            if progress.initialized or record.sequence != 1 or payload.identity != identity:
                raise ControllerRefusal("fault_journal_identity_mismatch")
            progress.initialized = record
            continue
        if not progress.initialized:
            raise ControllerRefusal("fault_journal_state_invalid")
        if isinstance(payload, RecoveryArmPlannedPayload):
            plan = payload.plan
            slot = slot_by_number.get(plan.slot_number)
            if (
                slot is None
                or slot.action != "enable"
                or plan != build_timer_plan(slot, identity)
                or plan.slot_number in progress.arm_plans
                or progress.action_plans
                or progress.successes
                or record.recorded_at >= plan.trigger_at
            ):
                raise ControllerRefusal("fault_journal_state_invalid")
            earlier_recoveries = tuple(
                candidate.number
                for candidate in slots
                if candidate.action == "enable" and candidate.number < slot.number
            )
            if any(number not in progress.armed for number in earlier_recoveries):
                raise ControllerRefusal("fault_journal_state_invalid")
            progress.arm_plans[slot.number] = record
            continue
        if isinstance(payload, RecoveryArmedPayload):
            plan = payload.plan
            planned = progress.arm_plans.get(plan.slot_number)
            planned_payload = planned.payload if planned is not None else None
            if (
                planned is None
                or plan.slot_number in progress.armed
                or not isinstance(planned_payload, RecoveryArmPlannedPayload)
                or planned_payload.plan != plan
                or record.recorded_at >= plan.trigger_at
            ):
                raise ControllerRefusal("fault_journal_state_invalid")
            require_timer_observation(plan, payload.observation)
            progress.armed[plan.slot_number] = record
            continue
        if isinstance(payload, FaultActionPlannedPayload):
            slot = slot_by_number.get(payload.slot_number)
            if (
                slot is None
                or slot.action != "disable"
                or payload.target != slot.target
                or payload.action != slot.action
                or payload.scheduled_at != slot.scheduled_at
                or payload.command_sha256 != slot.command_sha256
                or not payload.expected_before.enabled
                or payload.expected_before.target != slot.target
                or payload.expected_before != initial_observation(slot.target, progress)
                or payload.slot_number in progress.action_plans
                or set(progress.armed) != {2, 4}
                or set(progress.successes) != set(range(1, slot.number))
                or record.recorded_at < slot.scheduled_at
                or record.recorded_at > fault_disable_plan_deadline(slot.scheduled_at)
            ):
                raise ControllerRefusal("fault_journal_state_invalid")
            progress.action_plans[slot.number] = record
            continue
        if isinstance(payload, FaultActionSucceededPayload):
            slot = slot_by_number.get(payload.slot_number)
            if slot is None or payload.target != slot.target or payload.action != slot.action:
                raise ControllerRefusal("fault_journal_state_invalid")
            planned = _planned_record_for_success(slot, progress)
            baseline = baseline_observation(slot, progress)
            if (
                payload.slot_number in progress.successes
                or payload.planned_entry_sha256 != planned.entry_sha256
                or set(progress.successes) != set(range(1, slot.number))
                or payload.observation.identity_sha256 != baseline.identity_sha256
                or payload.observed_at < slot.scheduled_at
                or payload.observed_at > record.recorded_at
            ):
                raise ControllerRefusal("fault_journal_state_invalid")
            if slot.action == "disable":
                if payload.observed_at >= slot_by_number[slot.number + 1].scheduled_at:
                    raise ControllerRefusal("fault_journal_state_invalid")
            else:
                receipt = payload.recovery_receipt
                plan = build_timer_plan(slot, identity)
                if (
                    payload.observed_at > fault_recovery_deadline(slot.target, slot.scheduled_at)
                    or receipt is None
                    or receipt.unit_name != plan.unit_name
                    or receipt.trigger_at != plan.trigger_at
                    or receipt.recorded_at < slot.scheduled_at
                    or receipt.recorded_at > payload.observed_at
                    or receipt.receipt_sha256
                    != recovery_execution_receipt_sha256(plan, receipt.recorded_at)
                ):
                    raise ControllerRefusal("fault_journal_state_invalid")
            progress.successes[slot.number] = record
            continue
        if isinstance(payload, FaultControllerCompletedPayload):
            final = progress.successes.get(4)
            final_payload = final.payload if final is not None else None
            if (
                final is None
                or not isinstance(final_payload, FaultActionSucceededPayload)
                or payload.final_action_entry_sha256 != final.entry_sha256
                or payload.completed_at < final.recorded_at
                or payload.completed_at < final_payload.observed_at
                or payload.completed_at > record.recorded_at
            ):
                raise ControllerRefusal("fault_journal_state_invalid")
            if (
                tuple(observation.target for observation in payload.final_observations)
                != ("redis", "api-secondary")
                or not all(observation.enabled for observation in payload.final_observations)
                or payload.completed_at < slots[3].scheduled_at
            ):
                raise ControllerRefusal("fault_journal_state_invalid")
            for observation, recovery_slot in zip(
                payload.final_observations,
                (slots[1], slots[3]),
                strict=True,
            ):
                if (
                    observation.identity_sha256
                    != baseline_observation(recovery_slot, progress).identity_sha256
                ):
                    raise ControllerRefusal("fault_journal_state_invalid")
            progress.completed = record
            terminal_seen = True
            continue
        if isinstance(payload, FaultControllerFailedPayload):
            _require_failure_reachable(record, payload, progress, slots)
            progress.failed = record
            terminal_seen = True
            continue
        raise ControllerRefusal("fault_journal_state_invalid")
    return progress


def build_fault_controller_status(
    progress: FaultProgress,
    journal_head_sha256: str | None,
) -> FaultControllerStatus:
    """検証済み進行状態を秘密非保持のCLI statusへ射影する。"""

    if journal_head_sha256 is None:
        raise ControllerRefusal("fault_journal_state_invalid")
    if progress.failed is not None:
        payload = progress.failed.payload
        if not isinstance(payload, FaultControllerFailedPayload):
            raise ControllerRefusal("fault_journal_state_invalid")
        return FaultControllerStatus(
            status="failed",
            next_slot=None,
            journal_head_sha256=journal_head_sha256,
            failure_code=payload.code,
        )
    if progress.completed is not None:
        status = "completed"
    elif len(progress.successes) == 4:
        status = "finalizing"
    else:
        next_slot = next(number for number in range(1, 5) if number not in progress.successes)
        return FaultControllerStatus(
            status="waiting",
            next_slot=next_slot,
            journal_head_sha256=journal_head_sha256,
            failure_code=None,
        )
    return FaultControllerStatus(
        status=status,
        next_slot=None,
        journal_head_sha256=journal_head_sha256,
        failure_code=None,
    )
