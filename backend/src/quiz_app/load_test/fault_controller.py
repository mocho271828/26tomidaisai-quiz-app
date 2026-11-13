"""固定4 slotの障害注入と事前arm済み復旧timerを進行する。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

from quiz_app.load_test.controller_contracts import ControllerRefusal, manifest_context
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
    FaultMutationResult,
    FaultTarget,
    FaultTargetObservation,
    RecoveryArmedPayload,
    RecoveryArmPlannedPayload,
    RecoveryExecutionReceipt,
    RecoveryTimerObservation,
    RecoveryTimerPlan,
    fault_disable_plan_deadline,
    fault_recovery_deadline,
)
from quiz_app.load_test.fault_controller_journal import FaultControllerJournal
from quiz_app.load_test.fault_controller_state import (
    FaultProgress,
    FaultSlot,
    baseline_observation,
    build_fault_controller_status,
    build_fault_slots,
    build_timer_plan,
    initial_observation,
    reduce_fault_records,
    require_timer_observation,
)
from quiz_app.load_test.fault_host_config import (
    FaultMutationOutcomeUncertainError,
    FaultMutationPreconditionError,
    FaultTargetRecoveryPendingError,
)
from quiz_app.load_test.manifest import LoadTestManifest

MINIMUM_ARM_LEAD = timedelta(seconds=30)
RECOVERY_POLL_INTERVAL_SECONDS = 1.0


class _TerminalFailure(ControllerRefusal):
    """journalへ正式失格を書いた後だけrun境界でstatusへ変換する。"""


class FaultClock(Protocol):
    def now(self) -> datetime: ...

    def sleep(self, seconds: float) -> None: ...


class FaultHost(Protocol):
    def preflight(self) -> object: ...

    def recovery_preflight(self) -> None: ...

    def observe_target(self, target: FaultTarget) -> FaultTargetObservation: ...

    def apply_action(
        self,
        target: FaultTarget,
        action: FaultAction,
        expected_before: FaultTargetObservation,
    ) -> FaultMutationResult: ...

    def claim_recovery_ownership(self, plan: RecoveryTimerPlan) -> bool: ...

    def revoke_recovery_ownership(self, plan: RecoveryTimerPlan) -> None: ...

    def read_recovery_receipt(
        self,
        plan: RecoveryTimerPlan,
        observed_at: datetime,
    ) -> RecoveryExecutionReceipt | None: ...

    def arm_recovery_timer(
        self,
        plan: RecoveryTimerPlan,
    ) -> RecoveryTimerObservation: ...

    def read_recovery_timer(
        self,
        plan: RecoveryTimerPlan,
    ) -> RecoveryTimerObservation: ...


class SystemFaultClock:
    """UTC wall clockを使い、timer発火まで短いsleepで待つ。"""

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def sleep(seconds: float) -> None:
        import time

        time.sleep(seconds)


class LoadTestFaultController:
    """manifest由来の障害だけを、復旧timerを先にarmして実行する。"""

    def __init__(
        self,
        manifest: LoadTestManifest,
        identity: FaultControllerIdentity,
        journal: FaultControllerJournal,
        host: FaultHost,
        *,
        clock: FaultClock | None = None,
    ) -> None:
        self._manifest = manifest
        self._identity = identity
        self._journal = journal
        self._host = host
        self._clock = clock if clock is not None else SystemFaultClock()
        if manifest_context(manifest) != identity.context:
            raise ControllerRefusal("fault_manifest_identity_mismatch")
        self._slots = build_fault_slots(manifest, identity)

    def confirmation_prompt(self) -> str:
        return (
            f"CONFIRM {self._identity.context.run_id} FAULT-SCHEDULE "
            "REDIS API-SECONDARY AUTO-RECOVERY"
        )

    def run(self, confirmation_phrase: str) -> FaultControllerStatus:
        if confirmation_phrase != self.confirmation_prompt():
            raise ControllerRefusal("fault_confirmation_mismatch")
        progress = self._progress()
        if progress.failed is not None:
            self._restore_terminal_failure(progress)
            return self._status(progress)
        if progress.completed is not None:
            self._host.recovery_preflight()
            return self._status(progress)
        try:
            return self._run_active(progress)
        except _TerminalFailure:
            failed_progress = self._progress()
            if failed_progress.failed is None:
                raise ControllerRefusal("fault_journal_state_invalid") from None
            self._restore_terminal_failure(failed_progress)
            return self._status(failed_progress)

    def status(self) -> FaultControllerStatus:
        return self._status(self._progress())

    def _run_active(self, progress: FaultProgress) -> FaultControllerStatus:
        self._host.preflight()
        if not progress.initialized:
            self._initialize()
            progress = self._progress()
        if not progress.arm_plans:
            self._validate_initial_state(progress)
        self._arm_all_recoveries(progress)

        for slot in self._slots:
            progress = self._progress()
            if slot.number in progress.successes:
                continue
            if slot.action == "disable":
                self._execute_disable(slot, progress)
            else:
                self._observe_recovery(slot, progress)

        progress = self._progress()
        final = progress.successes.get(4)
        if final is None:
            raise ControllerRefusal("fault_journal_state_invalid")
        self._host.recovery_preflight()
        observations = (
            self._require_live_state("redis", True, progress),
            self._require_live_state("api-secondary", True, progress),
        )
        self._append(
            FaultControllerCompletedPayload(
                kind="completed",
                final_action_entry_sha256=final.entry_sha256,
                final_observations=observations,
                completed_at=self._now(),
            )
        )
        return self._status(self._progress())

    def _initialize(self) -> None:
        observations = tuple(
            self._host.observe_target(target) for target in ("redis", "api-secondary")
        )
        if tuple(value.target for value in observations) != (
            "redis",
            "api-secondary",
        ) or not all(value.enabled for value in observations):
            raise ControllerRefusal("fault_target_state_drift")
        self._append(
            FaultInitializedPayload(
                kind="fault_initialized",
                identity=self._identity,
                initial_observations=observations,
            )
        )

    def _validate_initial_state(self, progress: FaultProgress) -> None:
        now = self._now()
        first_disable_at = self._slots[0].scheduled_at
        if now > first_disable_at - MINIMUM_ARM_LEAD:
            self._fail("fault_slot_missed", self._slots[0])
        for target in ("redis", "api-secondary"):
            observation = self._host.observe_target(target)
            if observation != initial_observation(target, progress):
                self._fail("fault_target_state_drift", None, target=target)

    def _arm_all_recoveries(self, progress: FaultProgress) -> None:
        for slot in self._slots:
            if slot.action != "enable":
                continue
            plan = build_timer_plan(slot, self._identity)
            arm_check_at = self._now()
            if (
                slot.number not in progress.armed
                and arm_check_at >= plan.trigger_at - MINIMUM_ARM_LEAD
            ):
                self._fail("fault_slot_missed", slot)
            if slot.number not in progress.arm_plans:
                self._append(
                    RecoveryArmPlannedPayload(kind="recovery_arm_planned", plan=plan),
                    recorded_at=arm_check_at,
                )
                progress = self._progress()
            if slot.number not in progress.armed:
                observation = self._host.arm_recovery_timer(plan)
                require_timer_observation(plan, observation)
                armed_at = self._now()
                if armed_at >= plan.trigger_at:
                    self._fail("fault_slot_missed", slot)
                self._append(
                    RecoveryArmedPayload(
                        kind="recovery_armed",
                        plan=plan,
                        observation=observation,
                    ),
                    recorded_at=armed_at,
                )
                progress = self._progress()
            elif self._now() < plan.trigger_at:
                self._verify_armed_timer(slot, plan, progress)

    def _execute_disable(self, slot: FaultSlot, progress: FaultProgress) -> None:
        recovery_slot = self._slots[slot.number]
        recovery_plan = build_timer_plan(recovery_slot, self._identity)
        self._wait_until(slot.scheduled_at)
        now = self._now()
        planned_record = progress.action_plans.get(slot.number)
        if planned_record is None and now > fault_disable_plan_deadline(slot.scheduled_at):
            self._fail("fault_slot_missed", slot)
        if planned_record is not None and now >= recovery_slot.scheduled_at:
            if not isinstance(planned_record.payload, FaultActionPlannedPayload):
                raise ControllerRefusal("fault_journal_state_invalid")
            self._restore_target(slot.target, planned_record.payload.expected_before)
            self._fail("fault_slot_missed", slot)
        self._host.preflight()
        self._verify_armed_timer(recovery_slot, recovery_plan, progress)
        self._require_concurrent_target_available(slot, progress)

        if planned_record is None:
            before = self._host.observe_target(slot.target)
            if before != initial_observation(slot.target, progress):
                self._fail("fault_target_state_drift", slot)
            planned_at = self._now()
            if (
                planned_at > fault_disable_plan_deadline(slot.scheduled_at)
                or planned_at >= recovery_slot.scheduled_at
            ):
                self._fail("fault_slot_missed", slot)
            planned_record = self._append(
                FaultActionPlannedPayload(
                    kind="fault_action_planned",
                    slot_number=slot.number,
                    target=slot.target,
                    action=slot.action,
                    scheduled_at=slot.scheduled_at,
                    command_sha256=slot.command_sha256,
                    expected_before=before,
                ),
                recorded_at=planned_at,
            )
        if not isinstance(planned_record.payload, FaultActionPlannedPayload):
            raise ControllerRefusal("fault_journal_state_invalid")
        current = self._host.observe_target(slot.target)
        baseline = planned_record.payload.expected_before
        if current.target != slot.target or current.identity_sha256 != baseline.identity_sha256:
            # resume時の旧markerが置換後targetを変更しないよう先に無効化する。
            self._host.revoke_recovery_ownership(recovery_plan)
            self._fail("fault_target_state_drift", slot)
        decision_at = self._now()
        if decision_at >= recovery_slot.scheduled_at:
            if not current.enabled:
                self._restore_target(slot.target, baseline)
            self._fail("fault_slot_missed", slot)
        if current.enabled and decision_at > fault_disable_plan_deadline(slot.scheduled_at):
            self._fail("fault_slot_missed", slot)
        newly_claimed = self._host.claim_recovery_ownership(recovery_plan)
        if not current.enabled and newly_claimed:
            # action-plan後に外部が停止したtargetをcontroller所有と誤認しない。
            self._host.revoke_recovery_ownership(recovery_plan)
            self._fail("fault_target_state_drift", slot)
        reconciled = not current.enabled
        if reconciled:
            after = current
        else:
            # journal fsyncと対象probeの後にも再確認し、disable直前の最後の外部観測にする。
            self._require_concurrent_target_available(slot, progress)
            try:
                result = self._host.apply_action(slot.target, "disable", baseline)
            except FaultMutationOutcomeUncertainError:
                # timeout後にrequestが遅延完了しても自動復旧できるよう保持する。
                raise
            except FaultMutationPreconditionError:
                self._handle_precondition_failure(
                    slot,
                    recovery_plan,
                    baseline,
                    newly_claimed=newly_claimed,
                )
                raise
            except (RuntimeError, ValueError):
                self._revoke_ownership_if_unapplied(slot, recovery_plan, baseline)
                raise
            after = result.after
            reconciled = result.reconciled
        if (
            after.target != slot.target
            or after.enabled
            or after.identity_sha256 != baseline.identity_sha256
        ):
            # result返却時点で異常が確定しているため、置換targetをfuture timerが
            # 変更しないよう、identityを問わずmarkerを先に無効化する。
            self._host.revoke_recovery_ownership(recovery_plan)
            if after.target != slot.target or after.identity_sha256 != baseline.identity_sha256:
                self._fail("fault_target_state_drift", slot)
            self._fail("fault_action_failed", slot)
        observed_at = self._now()
        if observed_at >= recovery_slot.scheduled_at:
            self._restore_target(slot.target, baseline)
            self._fail("fault_slot_missed", slot)
        self._append(
            FaultActionSucceededPayload(
                kind="fault_action_succeeded",
                slot_number=slot.number,
                target=slot.target,
                action="disable",
                planned_entry_sha256=planned_record.entry_sha256,
                observation=after,
                observed_at=observed_at,
                reconciled=reconciled,
            ),
            recorded_at=observed_at,
        )

    def _observe_recovery(self, slot: FaultSlot, progress: FaultProgress) -> None:
        plan_record = progress.arm_plans.get(slot.number)
        if plan_record is None:
            raise ControllerRefusal("fault_journal_state_invalid")
        baseline = baseline_observation(slot, progress)
        self._require_disabled_until_recovery(slot, baseline)
        deadline = fault_recovery_deadline(slot.target, slot.scheduled_at)
        if self._now() > deadline:
            self._restore_after_missed_recovery(slot, baseline)
        while self._now() <= deadline:
            checked_at = self._now()
            receipt = self._host.read_recovery_receipt(
                build_timer_plan(slot, self._identity),
                checked_at,
            )
            candidate = self._observe_recovery_candidate(slot, baseline, deadline)
            if candidate is None:
                continue
            observation, observed_at = candidate
            if observation.enabled and receipt is not None:
                self._host.recovery_preflight()
                candidate = self._observe_recovery_candidate(slot, baseline, deadline)
                if candidate is None:
                    continue
                observation, observed_at = candidate
                if not observation.enabled:
                    self._clock.sleep(RECOVERY_POLL_INTERVAL_SECONDS)
                    continue
                self._append(
                    FaultActionSucceededPayload(
                        kind="fault_action_succeeded",
                        slot_number=slot.number,
                        target=slot.target,
                        action="enable",
                        planned_entry_sha256=plan_record.entry_sha256,
                        observation=observation,
                        observed_at=observed_at,
                        reconciled=False,
                        recovery_receipt=receipt,
                    )
                )
                return
            self._clock.sleep(RECOVERY_POLL_INTERVAL_SECONDS)
        self._restore_after_missed_recovery(slot, baseline)

    def _observe_recovery_candidate(
        self,
        slot: FaultSlot,
        baseline: FaultTargetObservation,
        deadline: datetime,
    ) -> tuple[FaultTargetObservation, datetime] | None:
        """過渡状態だけを再試行し、identityと回復期限を一箇所で検証する。"""

        try:
            observation = self._host.observe_target(slot.target)
        except FaultTargetRecoveryPendingError as pending:
            if pending.target != slot.target or pending.identity_sha256 != baseline.identity_sha256:
                self._fail("fault_target_state_drift", slot)
            if self._now() >= deadline:
                self._restore_after_missed_recovery(slot, baseline)
            self._clock.sleep(RECOVERY_POLL_INTERVAL_SECONDS)
            return None
        observed_at = self._now()
        if (
            observation.target != slot.target
            or observation.identity_sha256 != baseline.identity_sha256
        ):
            self._fail("fault_target_state_drift", slot)
        if observed_at > deadline:
            self._restore_after_missed_recovery(slot, baseline)
        return observation, observed_at

    def _require_disabled_until_recovery(
        self,
        slot: FaultSlot,
        baseline: FaultTargetObservation,
    ) -> None:
        """trigger前の早期復旧を見逃さず、指定outage時間を維持する。"""

        while self._now() < slot.scheduled_at:
            observation = self._host.observe_target(slot.target)
            observed_at = self._now()
            if (
                observation.target != slot.target
                or observation.identity_sha256 != baseline.identity_sha256
            ):
                self._fail("fault_target_state_drift", slot)
            if observation.enabled and observed_at < slot.scheduled_at:
                self._fail("fault_recovered_early", slot)
            if observed_at >= slot.scheduled_at:
                return
            remaining = (slot.scheduled_at - observed_at).total_seconds()
            self._clock.sleep(min(RECOVERY_POLL_INTERVAL_SECONDS, remaining))

    def _revoke_ownership_if_unapplied(
        self,
        slot: FaultSlot,
        plan: RecoveryTimerPlan,
        baseline: FaultTargetObservation,
    ) -> None:
        """request前例外後、未変更またはidentity driftならtimerを止める。"""

        try:
            current = self._host.observe_target(slot.target)
        except (RuntimeError, ValueError):
            return
        if (
            current.target != slot.target
            or current.identity_sha256 != baseline.identity_sha256
            or current.enabled
        ):
            self._host.revoke_recovery_ownership(plan)

    def _handle_precondition_failure(
        self,
        slot: FaultSlot,
        plan: RecoveryTimerPlan,
        baseline: FaultTargetObservation,
        *,
        newly_claimed: bool,
    ) -> None:
        """新規claimは解放し、旧claimは先行requestの回復余地を残す。"""

        if newly_claimed:
            self._host.revoke_recovery_ownership(plan)
            return
        try:
            current = self._host.observe_target(slot.target)
        except (RuntimeError, ValueError):
            return
        if current.target != slot.target or current.identity_sha256 != baseline.identity_sha256:
            self._host.revoke_recovery_ownership(plan)

    def _restore_after_missed_recovery(
        self,
        slot: FaultSlot,
        baseline: FaultTargetObservation,
    ) -> None:
        # 先に正式runを失格へ固定し、fallback後のcrashで成功へ昇格させない。
        self._append(
            FaultControllerFailedPayload(
                kind="fault_controller_failed",
                code="fault_recovery_deadline_missed",
                slot_number=slot.number,
                target=slot.target,
            )
        )
        self._restore_target(slot.target, baseline)
        raise _TerminalFailure("fault_recovery_deadline_missed")

    def _restore_terminal_failure(self, progress: FaultProgress) -> None:
        """期限超過失格とfallbackの間で落ちても、次回起動で安全復旧する。"""

        failed_record = progress.failed
        if failed_record is None or not isinstance(
            failed_record.payload, FaultControllerFailedPayload
        ):
            raise ControllerRefusal("fault_journal_state_invalid")
        failure = failed_record.payload
        if failure.slot_number is None or failure.target is None:
            if failure.code == "fault_recovery_deadline_missed":
                raise ControllerRefusal("fault_journal_state_invalid")
            return
        slot = self._slots[failure.slot_number - 1]
        is_concurrent_target_drift = (
            failure.code == "fault_target_state_drift" and slot.target != failure.target
        )
        if slot.target != failure.target and not is_concurrent_target_drift:
            raise ControllerRefusal("fault_journal_state_invalid")
        disable_slot_number = slot.number if slot.action == "disable" else slot.number - 1
        if disable_slot_number not in progress.action_plans:
            if failure.code == "fault_recovery_deadline_missed":
                raise ControllerRefusal("fault_journal_state_invalid")
            return
        recovery_slot = self._slots[disable_slot_number]
        recovery_plan = build_timer_plan(recovery_slot, self._identity)
        if failure.code == "fault_target_state_drift" and not is_concurrent_target_drift:
            # identity不明の対象は変更せず、future timerだけをfail-closedで止める。
            self._host.revoke_recovery_ownership(recovery_plan)
            return
        baseline = baseline_observation(slot, progress)
        if disable_slot_number not in progress.successes:
            # action planだけが残る既存claimは、送達済みdisableの結果が未確定な
            # 可能性がある。現在enabledでもtimerまで保持し、遅延停止を回復させる。
            self._host.recovery_preflight()
            current = self._observe_target_for_restore(slot.target, baseline)
            if current.target != slot.target or current.identity_sha256 != baseline.identity_sha256:
                self._host.revoke_recovery_ownership(recovery_plan)
                raise ControllerRefusal("fault_target_state_drift")
            if current.enabled:
                newly_claimed = self._host.claim_recovery_ownership(recovery_plan)
                if newly_claimed:
                    self._host.revoke_recovery_ownership(recovery_plan)
                return
        self._restore_target(slot.target, baseline)
        self._host.revoke_recovery_ownership(recovery_plan)

    def _require_concurrent_target_available(
        self,
        slot: FaultSlot,
        progress: FaultProgress,
    ) -> None:
        """対象を止める直前に、もう一方の障害対象が利用可能か確認する。"""

        other_target: FaultTarget = "api-secondary" if slot.target == "redis" else "redis"
        observation = self._host.observe_target(other_target)
        if observation != initial_observation(other_target, progress):
            self._fail("fault_target_state_drift", slot, target=other_target)

    def _restore_target(
        self,
        target: FaultTarget,
        baseline: FaultTargetObservation,
    ) -> None:
        self._host.recovery_preflight()
        current = self._observe_target_for_restore(target, baseline)
        if current.target != target or current.identity_sha256 != baseline.identity_sha256:
            raise ControllerRefusal("fault_target_state_drift")
        if current.enabled:
            return
        try:
            restored = self._host.apply_action(target, "enable", current).after
        except FaultTargetRecoveryPendingError as pending:
            if pending.target != target or pending.identity_sha256 != baseline.identity_sha256:
                raise ControllerRefusal("fault_target_state_drift") from None
            restored = self._observe_target_for_restore(target, baseline)
        if (
            restored.target != target
            or not restored.enabled
            or restored.identity_sha256 != baseline.identity_sha256
        ):
            raise ControllerRefusal("fault_action_failed")

    def _observe_target_for_restore(
        self,
        target: FaultTarget,
        baseline: FaultTargetObservation,
    ) -> FaultTargetObservation:
        deadline = fault_recovery_deadline(target, self._now())
        while True:
            try:
                return self._host.observe_target(target)
            except FaultTargetRecoveryPendingError as pending:
                if pending.target != target or pending.identity_sha256 != baseline.identity_sha256:
                    raise ControllerRefusal("fault_target_state_drift") from None
                now = self._now()
                if now >= deadline:
                    raise ControllerRefusal("fault_action_failed") from None
                remaining = (deadline - now).total_seconds()
                self._clock.sleep(min(RECOVERY_POLL_INTERVAL_SECONDS, remaining))

    def _require_live_state(
        self,
        target: FaultTarget,
        enabled: bool,
        progress: FaultProgress,
    ) -> FaultTargetObservation:
        observation = self._host.observe_target(target)
        recovery_number = 2 if target == "redis" else 4
        baseline = baseline_observation(self._slots[recovery_number - 1], progress)
        if (
            observation.target != target
            or observation.enabled is not enabled
            or observation.identity_sha256 != baseline.identity_sha256
        ):
            self._fail("fault_target_state_drift", None, target=target)
        return observation

    def _verify_armed_timer(
        self,
        slot: FaultSlot,
        plan: RecoveryTimerPlan,
        progress: FaultProgress,
    ) -> None:
        armed_record = progress.armed.get(slot.number)
        if armed_record is None or not isinstance(armed_record.payload, RecoveryArmedPayload):
            raise ControllerRefusal("fault_journal_state_invalid")
        current = self._host.read_recovery_timer(plan)
        require_timer_observation(plan, current)
        if current != armed_record.payload.observation:
            self._fail("fault_timer_invalid", slot)

    def _wait_until(self, target: datetime) -> None:
        while True:
            remaining = (target - self._now()).total_seconds()
            if remaining <= 0:
                return
            self._clock.sleep(min(1.0, remaining))

    def _now(self) -> datetime:
        value = self._clock.now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ControllerRefusal("fault_clock_invalid")
        return value.astimezone(UTC)

    def _append(
        self,
        payload: FaultInitializedPayload
        | RecoveryArmPlannedPayload
        | RecoveryArmedPayload
        | FaultActionPlannedPayload
        | FaultActionSucceededPayload
        | FaultControllerCompletedPayload
        | FaultControllerFailedPayload,
        *,
        recorded_at: datetime | None = None,
    ) -> FaultControllerJournalRecord:
        return self._journal.append(
            payload,
            recorded_at=self._now() if recorded_at is None else recorded_at,
        )

    def _fail(
        self,
        code: str,
        slot: FaultSlot | None,
        *,
        target: FaultTarget | None = None,
    ) -> None:
        self._append(
            FaultControllerFailedPayload(
                kind="fault_controller_failed",
                code=code,
                slot_number=slot.number if slot is not None else None,
                target=target if target is not None else (slot.target if slot else None),
            )
        )
        raise _TerminalFailure(code)

    def _progress(self) -> FaultProgress:
        return reduce_fault_records(self._journal.records, self._identity, self._slots)

    def _status(self, progress: FaultProgress) -> FaultControllerStatus:
        return build_fault_controller_status(
            progress,
            self._journal.head_sha256,
        )
