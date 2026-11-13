from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest

from quiz_app.load_test.controller_contracts import (
    ControllerRefusal,
    canonical_json_bytes,
    manifest_context,
)
from quiz_app.load_test.fault_controller import LoadTestFaultController
from quiz_app.load_test.fault_controller_contracts import (
    FaultAction,
    FaultActionPlannedPayload,
    FaultActionSucceededPayload,
    FaultCommandIdentity,
    FaultControllerFailedPayload,
    FaultControllerIdentity,
    FaultControllerJournalRecord,
    FaultInitializedPayload,
    FaultMutationResult,
    FaultReleaseIdentity,
    FaultTarget,
    FaultTargetObservation,
    FaultTopologyIdentity,
    RecoveryArmedPayload,
    RecoveryArmPlannedPayload,
    RecoveryExecutionReceipt,
    RecoveryTimerObservation,
    RecoveryTimerPlan,
    fault_recovery_deadline,
    recovery_execution_receipt_sha256,
)
from quiz_app.load_test.fault_controller_journal import FaultControllerJournal
from quiz_app.load_test.fault_controller_state import (
    build_fault_slots,
    build_timer_plan,
)
from quiz_app.load_test.fault_host_config import (
    FaultMutationOutcomeUncertainError,
    FaultMutationPreconditionError,
    FaultTargetRecoveryPendingError,
)
from quiz_app.load_test.manifest import LoadTestManifest, build_load_test_manifest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RUN_ID = UUID("81111111-1111-4111-8111-111111111111")
EVENT_ID = UUID("82222222-2222-4222-8222-222222222222")
SOURCE_COMMIT = "a" * 40
IMAGE = f"registry.aws.internal/quiz/api@sha256:{'1' * 64}"
STARTED_AT = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)


def _manifest() -> LoadTestManifest:
    return build_load_test_manifest(
        run_id=RUN_ID,
        event_id=EVENT_ID,
        event_slug="load-test-2026-09-08",
        scheduled_start_at=STARTED_AT,
        public_origin="https://quiz.aws.internal",
        source_commit=SOURCE_COMMIT,
        api_image=IMAGE,
        gateway_image=IMAGE,
        generator_image=IMAGE,
        scoring_policy_version="scoring-v1",
        scoring_base_points=10_000,
        scoring_initial_speed_bonus=500,
        scoring_speed_bucket_ms=100,
        scoring_speed_decrement_per_bucket=10,
        scoring_standard_duration_ms=10_000,
        scoring_is_approved=True,
        display_name_policy_version="display-v1",
        display_notice_version="notice-v1",
        display_max_code_points=20,
        display_reject_patterns_json="[]",
        created_at=STARTED_AT - timedelta(hours=1),
    )


def _identity(manifest: LoadTestManifest) -> FaultControllerIdentity:
    commands = tuple(
        FaultCommandIdentity(
            slot_number=number,
            target=action.target,
            action=action.action,
            command_sha256=sha256(f"{number}:{action.target}:{action.action}".encode()).hexdigest(),
        )
        for number, action in enumerate(manifest.scenario.fault_schedule, start=1)
    )
    return FaultControllerIdentity(
        context=manifest_context(manifest),
        release=FaultReleaseIdentity(
            release_id="candidate-2026-09-08",
            release_manifest_sha256="b" * 64,
            source_commit=SOURCE_COMMIT,
            rollback_release_id="rollback-2026-09-08",
            rollback_manifest_sha256="c" * 64,
            execution_host="load.aws.internal",
            docker_context="default",
        ),
        topology=FaultTopologyIdentity(
            configuration_sha256="d" * 64,
            docker_compose_version="2.39.4",
            docker_engine_version="28.0.1",
            fault_control_network_sha256="6" * 64,
            toxiproxy_container_sha256="5" * 64,
            probe_command_sha256="e" * 64,
            api_probe_command_sha256="7" * 64,
            recovery_compose_sha256="f" * 64,
            proxy_name="gs-app-tomidaisai-aws-demo-redis",
            control_origin="http://172.30.251.2:8474",
            redis_hostname="redis.aws.internal",
            listen="172.30.250.2:6380",
            upstream="10.0.2.15:6380",
        ),
        commands=commands,
    )


def _initial_observations() -> tuple[FaultTargetObservation, FaultTargetObservation]:
    return (
        FaultTargetObservation(
            target="redis",
            enabled=True,
            identity_sha256=sha256(b"identity:redis").hexdigest(),
        ),
        FaultTargetObservation(
            target="api-secondary",
            enabled=True,
            identity_sha256=sha256(b"identity:api-secondary").hexdigest(),
        ),
    )


@dataclass(slots=True)
class FakeClock:
    value: datetime

    def now(self) -> datetime:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class SimulatedCrash(RuntimeError):
    pass


@dataclass(slots=True)
class FakeHost:
    clock: FakeClock
    fire_timers: bool = True
    skip_timer_slots: set[int] = field(default_factory=set)
    write_receipts: bool = True
    defer_receipt_until_observation: FaultTarget | None = None
    crash_before_target: FaultTarget | None = None
    crash_after_target: FaultTarget | None = None
    crash_on_next_observation: FaultTarget | None = None
    crash_on_due_observation: FaultTarget | None = None
    crash_before_action: tuple[FaultTarget, FaultAction] | None = None
    outcome_uncertain_before_action: tuple[FaultTarget, FaultAction] | None = None
    precondition_disable_before_action: tuple[FaultTarget, FaultAction] | None = None
    identity_drift_after_action: tuple[FaultTarget, FaultAction] | None = None
    pending_after_action: tuple[FaultTarget, FaultAction, int] | None = None
    delay_due_observation: tuple[FaultTarget, float] | None = None
    timer_read_delay_seconds: float | None = None
    timer_read_delays_seconds: list[float] = field(default_factory=list)
    arm_delay_seconds: float | None = None
    delay_before_mutation: tuple[FaultTarget, FaultAction, float] | None = None
    unavailable_at: tuple[FaultTarget, datetime] | None = None
    unavailable_on_observation: tuple[FaultTarget, int] | None = None
    recover_early_at: tuple[FaultTarget, datetime] | None = None
    identity_drift_at: tuple[FaultTarget, datetime] | None = None
    corrupt_timer_read: bool = False
    fail_preflight: bool = False
    fail_recovery_preflight: bool = False
    fail_recovery_preflight_on_call: int | None = None
    identity_drift_on_recovery_preflight: tuple[int, FaultTarget] | None = None
    recovery_preflight_delays_seconds: list[float] = field(default_factory=list)
    reject_elapsed_timer_read: bool = False
    calls: list[str] = field(default_factory=list)
    states: dict[FaultTarget, bool] = field(
        default_factory=lambda: {"redis": True, "api-secondary": True}
    )
    timers: dict[int, RecoveryTimerPlan] = field(default_factory=dict)
    fired_timers: set[int] = field(default_factory=set)
    owned_recoveries: set[int] = field(default_factory=set)
    receipts: dict[int, RecoveryExecutionReceipt] = field(default_factory=dict)
    pending_receipts: set[int] = field(default_factory=set)
    identity_overrides: dict[FaultTarget, str] = field(default_factory=dict)
    recovery_pending_counts: dict[FaultTarget, int] = field(default_factory=dict)
    pending_at: tuple[FaultTarget, datetime, int] | None = None
    observation_counts: dict[FaultTarget, int] = field(
        default_factory=lambda: {"redis": 0, "api-secondary": 0}
    )
    recovery_preflight_count: int = 0

    @staticmethod
    def _identity(target: FaultTarget) -> str:
        return sha256(f"identity:{target}".encode()).hexdigest()

    def _fire_due_timers(self) -> None:
        if self.unavailable_at is not None and self.clock.now() >= self.unavailable_at[1]:
            self.states[self.unavailable_at[0]] = False
            self.unavailable_at = None
        if self.recover_early_at is not None and self.clock.now() >= self.recover_early_at[1]:
            self.states[self.recover_early_at[0]] = True
            self.recover_early_at = None
        if self.identity_drift_at is not None and self.clock.now() >= self.identity_drift_at[1]:
            self.identity_overrides[self.identity_drift_at[0]] = "8" * 64
            self.identity_drift_at = None
        if self.pending_at is not None and self.clock.now() >= self.pending_at[1]:
            target, _, count = self.pending_at
            self.recovery_pending_counts[target] = count
            self.pending_at = None
        if not self.fire_timers:
            return
        for slot_number, plan in self.timers.items():
            if slot_number not in self.fired_timers and self.clock.now() >= plan.trigger_at:
                self.fired_timers.add(slot_number)
                if slot_number in self.skip_timer_slots:
                    continue
                if slot_number not in self.owned_recoveries:
                    continue
                self.states[plan.target] = True
                if self.write_receipts:
                    if (
                        self.defer_receipt_until_observation == plan.target
                        or self.recovery_pending_counts.get(plan.target, 0) > 0
                    ):
                        self.pending_receipts.add(slot_number)
                    else:
                        self._record_receipt(slot_number, plan)

    def _record_receipt(self, slot_number: int, plan: RecoveryTimerPlan) -> None:
        recorded_at = self.clock.now()
        self.receipts[slot_number] = RecoveryExecutionReceipt(
            unit_name=plan.unit_name,
            trigger_at=plan.trigger_at,
            recorded_at=recorded_at,
            receipt_sha256=recovery_execution_receipt_sha256(plan, recorded_at),
        )

    def preflight(self) -> None:
        if self.fail_preflight:
            raise SimulatedCrash
        self.calls.append("preflight")

    def recovery_preflight(self) -> None:
        self.recovery_preflight_count += 1
        call_number = self.recovery_preflight_count
        if self.recovery_preflight_delays_seconds:
            self.clock.sleep(self.recovery_preflight_delays_seconds.pop(0))
        if self.identity_drift_on_recovery_preflight == (
            call_number,
            "redis",
        ):
            self.identity_overrides["redis"] = "8" * 64
        elif self.identity_drift_on_recovery_preflight == (
            call_number,
            "api-secondary",
        ):
            self.identity_overrides["api-secondary"] = "8" * 64
        if self.fail_recovery_preflight or self.fail_recovery_preflight_on_call == call_number:
            raise SimulatedCrash
        self.calls.append("recovery-preflight")

    def observe_target(self, target: FaultTarget) -> FaultTargetObservation:
        self._fire_due_timers()
        self.observation_counts[target] += 1
        if self.unavailable_on_observation == (target, self.observation_counts[target]):
            self.states[target] = False
            self.unavailable_on_observation = None
        if self.crash_on_next_observation == target:
            self.crash_on_next_observation = None
            raise SimulatedCrash
        pending_count = self.recovery_pending_counts.get(target, 0)
        if pending_count > 0 and any(
            plan.target == target and slot_number in self.fired_timers
            for slot_number, plan in self.timers.items()
        ):
            self.recovery_pending_counts[target] = pending_count - 1
            if pending_count == 1:
                self.states[target] = True
            raise FaultTargetRecoveryPendingError(
                "fault_host_api_recovery_pending",
                target=target,
                identity_sha256=self.identity_overrides.get(target, self._identity(target)),
            )
        matching_timer = next(
            (plan for plan in self.timers.values() if plan.target == target),
            None,
        )
        if (
            self.crash_on_due_observation == target
            and matching_timer is not None
            and self.clock.now() >= matching_timer.trigger_at
        ):
            self.crash_on_due_observation = None
            raise SimulatedCrash
        if (
            self.delay_due_observation is not None
            and self.delay_due_observation[0] == target
            and matching_timer is not None
            and self.clock.now() >= matching_timer.trigger_at
        ):
            _, delay_seconds = self.delay_due_observation
            self.delay_due_observation = None
            self.clock.sleep(delay_seconds)
        self.calls.append(f"observe:{target}:{self.states[target]}")
        observation = FaultTargetObservation(
            target=target,
            enabled=self.states[target],
            identity_sha256=self.identity_overrides.get(target, self._identity(target)),
        )
        for slot_number in tuple(self.pending_receipts):
            plan = self.timers[slot_number]
            if plan.target == target:
                self._record_receipt(slot_number, plan)
                self.pending_receipts.remove(slot_number)
        return observation

    def apply_action(
        self,
        target: FaultTarget,
        action: FaultAction,
        expected_before: FaultTargetObservation,
    ) -> FaultMutationResult:
        if self.precondition_disable_before_action == (target, action):
            self.precondition_disable_before_action = None
            self.states[target] = False
            raise FaultMutationPreconditionError("fault_host_state_mismatch")
        if self.crash_before_action == (target, action):
            self.crash_before_action = None
            raise SimulatedCrash
        if self.crash_before_target == target:
            self.crash_before_target = None
            raise SimulatedCrash
        before = self.observe_target(target)
        if before != expected_before:
            raise ValueError("fault_host_state_mismatch")
        if self.outcome_uncertain_before_action == (target, action):
            self.outcome_uncertain_before_action = None
            self.unavailable_at = (target, self.clock.now() + timedelta(seconds=1))
            raise FaultMutationOutcomeUncertainError("fault_host_command_timeout")
        if self.delay_before_mutation is not None and self.delay_before_mutation[0:2] == (
            target,
            action,
        ):
            _, _, delay_seconds = self.delay_before_mutation
            self.delay_before_mutation = None
            self.clock.sleep(delay_seconds)
            self._fire_due_timers()
        self.states[target] = action == "enable"
        self.calls.append(f"apply:{target}:{action}")
        if self.pending_after_action is not None and self.pending_after_action[0:2] == (
            target,
            action,
        ):
            _, _, count = self.pending_after_action
            self.pending_after_action = None
            self.recovery_pending_counts[target] = count
            recovery_slot_number = 2 if target == "redis" else 4
            self.fired_timers.add(recovery_slot_number)
            raise FaultTargetRecoveryPendingError(
                "fault_host_api_recovery_pending",
                target=target,
                identity_sha256=self.identity_overrides.get(target, self._identity(target)),
            )
        has_identity_drift = self.identity_drift_after_action == (target, action)
        if has_identity_drift:
            self.identity_drift_after_action = None
            self.identity_overrides[target] = "8" * 64
        if self.crash_after_target == target:
            self.crash_after_target = None
            raise SimulatedCrash
        after = self.observe_target(target)
        if has_identity_drift:
            # 壊れたProtocol実装がvalidated contractを迂回した場合もcontroller境界で拒否する。
            return FaultMutationResult.model_construct(
                before=before,
                after=after,
                reconciled=False,
            )
        return FaultMutationResult(before=before, after=after, reconciled=False)

    def claim_recovery_ownership(self, plan: RecoveryTimerPlan) -> bool:
        self.calls.append(f"claim:{plan.slot_number}")
        if plan.slot_number in self.owned_recoveries:
            return False
        self.owned_recoveries.add(plan.slot_number)
        return True

    def revoke_recovery_ownership(self, plan: RecoveryTimerPlan) -> None:
        self.calls.append(f"revoke:{plan.slot_number}")
        self.owned_recoveries.discard(plan.slot_number)

    def read_recovery_receipt(
        self,
        plan: RecoveryTimerPlan,
        observed_at: datetime,
    ) -> RecoveryExecutionReceipt | None:
        del observed_at
        self.calls.append(f"receipt:{plan.slot_number}")
        self._fire_due_timers()
        return self.receipts.get(plan.slot_number)

    def arm_recovery_timer(self, plan: RecoveryTimerPlan) -> RecoveryTimerObservation:
        self.calls.append(f"arm:{plan.slot_number}")
        self.timers[plan.slot_number] = plan
        if self.arm_delay_seconds is not None:
            delay_seconds = self.arm_delay_seconds
            self.arm_delay_seconds = None
            self.clock.sleep(delay_seconds)
        return self._timer_observation(plan)

    def read_recovery_timer(self, plan: RecoveryTimerPlan) -> RecoveryTimerObservation:
        if self.reject_elapsed_timer_read and self.clock.now() >= plan.trigger_at:
            raise AssertionError("elapsed timer must not be read")
        self.calls.append(f"read:{plan.slot_number}")
        assert self.timers[plan.slot_number] == plan
        if self.timer_read_delays_seconds:
            self.clock.sleep(self.timer_read_delays_seconds.pop(0))
        elif self.timer_read_delay_seconds is not None:
            delay_seconds = self.timer_read_delay_seconds
            self.timer_read_delay_seconds = None
            self.clock.sleep(delay_seconds)
        if self.corrupt_timer_read:
            return RecoveryTimerObservation(
                unit_name=plan.unit_name,
                trigger_at=plan.trigger_at,
                properties_sha256="f" * 64,
            )
        return self._timer_observation(plan)

    @staticmethod
    def _timer_observation(plan: RecoveryTimerPlan) -> RecoveryTimerObservation:
        return RecoveryTimerObservation(
            unit_name=plan.unit_name,
            trigger_at=plan.trigger_at,
            properties_sha256=sha256(plan.model_dump_json().encode()).hexdigest(),
        )


def _journal_path(tmp_path: Path) -> Path:
    directory = tmp_path / "fault-journal"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return directory / "journal.jsonl"


def _append_armed_recovery(
    journal: FaultControllerJournal,
    manifest: LoadTestManifest,
    identity: FaultControllerIdentity,
    host: FakeHost,
    slot_number: int,
) -> RecoveryTimerPlan:
    slot = build_fault_slots(manifest, identity)[slot_number - 1]
    plan = build_timer_plan(slot, identity)
    journal.append(
        RecoveryArmPlannedPayload(kind="recovery_arm_planned", plan=plan),
        recorded_at=STARTED_AT,
    )
    host.timers[slot_number] = plan
    journal.append(
        RecoveryArmedPayload(
            kind="recovery_armed",
            plan=plan,
            observation=host._timer_observation(plan),
        ),
        recorded_at=STARTED_AT,
    )
    return plan


def _rewrite_last_record_payload_time(
    path: Path,
    field: str,
    value: datetime,
) -> None:
    lines = path.read_bytes().splitlines()
    record = json.loads(lines[-1])
    record["payload"][field] = value.isoformat().replace("+00:00", "Z")
    provisional = FaultControllerJournalRecord.model_validate_json(
        canonical_json_bytes({**record, "entry_sha256": "0" * 64})
    )
    unsigned = provisional.model_dump(mode="json", exclude={"entry_sha256"})
    record["entry_sha256"] = sha256(canonical_json_bytes(unsigned)).hexdigest()
    canonical = FaultControllerJournalRecord.model_validate_json(canonical_json_bytes(record))
    lines[-1] = canonical_json_bytes(canonical)
    path.write_bytes(b"\n".join(lines) + b"\n")


def _run(
    manifest: LoadTestManifest,
    identity: FaultControllerIdentity,
    host: FakeHost,
    journal_path: Path,
) -> tuple[object, tuple[str, ...]]:
    with FaultControllerJournal(
        journal_path,
        REPOSITORY_ROOT,
        identity=identity,
    ) as journal:
        controller = LoadTestFaultController(
            manifest,
            identity,
            journal,
            host,
            clock=host.clock,
        )
        status = controller.run(controller.confirmation_prompt())
        kinds = tuple(record.payload.kind for record in journal.records)
    return status, kinds


def test_full_schedule_arms_both_recoveries_before_first_disable(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(FakeClock(STARTED_AT))

    status, kinds = _run(manifest, identity, host, _journal_path(tmp_path))

    assert status.status == "completed"
    assert kinds == (
        "fault_initialized",
        "recovery_arm_planned",
        "recovery_armed",
        "recovery_arm_planned",
        "recovery_armed",
        "fault_action_planned",
        "fault_action_succeeded",
        "fault_action_succeeded",
        "fault_action_planned",
        "fault_action_succeeded",
        "fault_action_succeeded",
        "completed",
    )
    assert host.calls.index("arm:2") < host.calls.index("apply:redis:disable")
    assert host.calls.index("arm:4") < host.calls.index("apply:redis:disable")
    assert "apply:redis:enable" not in host.calls
    assert "apply:api-secondary:enable" not in host.calls
    assert host.states == {"redis": True, "api-secondary": True}


def test_resume_from_four_successes_reports_finalizing_then_completes(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(FakeClock(STARTED_AT))
    path = _journal_path(tmp_path)
    completed, _ = _run(manifest, identity, host, path)
    assert completed.status == "completed"

    records = path.read_bytes().splitlines()
    assert json.loads(records[-1])["payload"]["kind"] == "completed"
    path.write_bytes(b"\n".join(records[:-1]) + b"\n")
    path.chmod(0o600)

    with FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity) as journal:
        controller = LoadTestFaultController(
            manifest,
            identity,
            journal,
            host,
            clock=host.clock,
        )
        assert controller.status().status == "finalizing"

    resumed, kinds = _run(manifest, identity, host, path)
    assert resumed.status == "completed"
    assert kinds.count("completed") == 1


def test_completed_resume_requires_recovery_host_preflight(tmp_path: Path) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(FakeClock(STARTED_AT))
    path = _journal_path(tmp_path)
    completed, _ = _run(manifest, identity, host, path)
    assert completed.status == "completed"
    host.fail_recovery_preflight = True

    with pytest.raises(SimulatedCrash):
        _run(manifest, identity, host, path)


def test_crash_after_disable_is_reconciled_without_second_mutation(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    path = _journal_path(tmp_path)
    host = FakeHost(FakeClock(STARTED_AT), crash_after_target="redis")

    with pytest.raises(SimulatedCrash):
        _run(manifest, identity, host, path)
    assert host.states["redis"] is False

    status, _ = _run(manifest, identity, host, path)

    assert status.status == "completed"
    assert host.calls.count("apply:redis:disable") == 1
    with FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity) as journal:
        redis_success = next(
            record.payload
            for record in journal.records
            if record.payload.kind == "fault_action_succeeded" and record.payload.slot_number == 1
        )
        assert redis_success.reconciled is True


def test_crash_after_initial_identity_rechecks_both_targets_before_arm(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    path = _journal_path(tmp_path)
    host = FakeHost(FakeClock(STARTED_AT), crash_on_next_observation="redis")

    with pytest.raises(SimulatedCrash):
        _run(manifest, identity, host, path)
    assert not host.timers

    status, kinds = _run(manifest, identity, host, path)

    assert status.status == "completed"
    assert kinds.count("fault_initialized") == 1
    assert host.calls.index("observe:api-secondary:True") < host.calls.index("arm:2")


def test_late_resume_before_unapplied_disable_never_injects_fault(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    path = _journal_path(tmp_path)
    host = FakeHost(FakeClock(STARTED_AT), crash_before_target="redis")

    with pytest.raises(SimulatedCrash):
        _run(manifest, identity, host, path)
    host.clock.sleep(6)

    status, _ = _run(manifest, identity, host, path)

    assert status.status == "failed"
    assert status.failure_code == "fault_slot_missed"
    assert host.states["redis"] is True
    assert "apply:redis:disable" not in host.calls


def test_slow_pre_disable_readback_cannot_inject_after_tolerance(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(FakeClock(STARTED_AT), timer_read_delay_seconds=6.0)

    status, _ = _run(manifest, identity, host, _journal_path(tmp_path))

    assert status.status == "failed"
    assert status.failure_code == "fault_slot_missed"
    assert host.states["redis"] is True
    assert "apply:redis:disable" not in host.calls


def test_disable_refuses_when_other_fault_target_is_unavailable(tmp_path: Path) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    first_disable = manifest.scenario.fault_schedule[0]
    host = FakeHost(
        FakeClock(STARTED_AT),
        unavailable_at=(
            "api-secondary",
            STARTED_AT + timedelta(seconds=first_disable.offset_seconds),
        ),
    )

    status, _ = _run(manifest, identity, host, _journal_path(tmp_path))

    assert status.status == "failed"
    assert status.failure_code == "fault_target_state_drift"
    assert host.states == {"redis": True, "api-secondary": False}
    assert "apply:redis:disable" not in host.calls


def test_disable_rechecks_other_target_after_plan_fsync(tmp_path: Path) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(
        FakeClock(STARTED_AT),
        # 初期確認、slot前確認は通し、mutation直前の再確認だけで停止させる。
        unavailable_on_observation=("api-secondary", 4),
    )

    status, kinds = _run(manifest, identity, host, _journal_path(tmp_path))

    assert status.status == "failed"
    assert status.failure_code == "fault_target_state_drift"
    assert host.states == {"redis": True, "api-secondary": False}
    assert "fault_action_planned" in kinds
    assert "apply:redis:disable" not in host.calls


def test_prearmed_timer_without_ownership_cannot_enable_external_stop(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    first_disable = manifest.scenario.fault_schedule[0]
    first_recovery = manifest.scenario.fault_schedule[1]
    host = FakeHost(
        FakeClock(STARTED_AT),
        unavailable_at=(
            "api-secondary",
            STARTED_AT + timedelta(seconds=first_disable.offset_seconds),
        ),
    )

    status, _ = _run(manifest, identity, host, _journal_path(tmp_path))
    assert status.status == "failed"
    assert 2 not in host.owned_recoveries

    host.states["redis"] = False
    host.clock.value = STARTED_AT + timedelta(seconds=first_recovery.offset_seconds)
    host._fire_due_timers()

    assert 2 in host.fired_timers
    assert host.states["redis"] is False
    assert 2 not in host.receipts


def test_new_claim_rejects_target_stopped_after_action_plan(tmp_path: Path) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(
        FakeClock(STARTED_AT),
        # initialとplan前はenabled、plan fsync後の最終probeで外部停止。
        unavailable_on_observation=("redis", 4),
    )

    status, _ = _run(manifest, identity, host, _journal_path(tmp_path))

    assert status.status == "failed"
    assert status.failure_code == "fault_target_state_drift"
    assert host.states["redis"] is False
    assert "apply:redis:disable" not in host.calls
    assert 2 not in host.owned_recoveries


def test_action_exception_revoke_claim_when_target_is_still_enabled(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(
        FakeClock(STARTED_AT),
        crash_before_action=("redis", "disable"),
    )

    with pytest.raises(SimulatedCrash):
        _run(manifest, identity, host, _journal_path(tmp_path))

    assert host.states["redis"] is True
    assert 2 not in host.owned_recoveries


def test_outcome_uncertain_keeps_recovery_for_delayed_disable(tmp_path: Path) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    first_recovery = manifest.scenario.fault_schedule[1]
    host = FakeHost(
        FakeClock(STARTED_AT),
        outcome_uncertain_before_action=("redis", "disable"),
    )

    with pytest.raises(FaultMutationOutcomeUncertainError):
        _run(manifest, identity, host, _journal_path(tmp_path))
    assert 2 in host.owned_recoveries

    host.clock.value = STARTED_AT + timedelta(seconds=first_recovery.offset_seconds)
    host._fire_due_timers()

    assert host.states["redis"] is True
    assert 2 in host.receipts


def test_terminal_resume_keeps_existing_claim_for_delayed_disable(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    recovery = manifest.scenario.fault_schedule[1]
    path = _journal_path(tmp_path)
    host = FakeHost(
        FakeClock(STARTED_AT),
        outcome_uncertain_before_action=("redis", "disable"),
    )

    with pytest.raises(FaultMutationOutcomeUncertainError):
        _run(manifest, identity, host, path)
    assert 2 in host.owned_recoveries

    # 遅延requestがまだ届いていない状態でdeadline後にresumeしても、既存claimを
    # 消さず、後着disableをtimerが回復できる状態を維持する。
    host.unavailable_at = None
    host.fire_timers = False
    host.clock.value = fault_recovery_deadline(
        "redis",
        STARTED_AT + timedelta(seconds=recovery.offset_seconds),
    ) + timedelta(seconds=1)
    status, _ = _run(manifest, identity, host, path)

    assert status.status == "failed"
    assert status.failure_code == "fault_slot_missed"
    assert 2 in host.owned_recoveries

    host.states["redis"] = False
    host.fire_timers = True
    host._fire_due_timers()
    assert host.states["redis"] is True
    assert 2 in host.receipts


def test_new_claim_precondition_failure_revokes_external_disable_ownership(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(
        FakeClock(STARTED_AT),
        precondition_disable_before_action=("redis", "disable"),
    )

    with pytest.raises(FaultMutationPreconditionError):
        _run(manifest, identity, host, _journal_path(tmp_path))

    assert host.states["redis"] is False
    assert 2 not in host.owned_recoveries


def test_resume_precondition_keeps_prior_uncertain_recovery_claim(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    recovery = manifest.scenario.fault_schedule[1]
    path = _journal_path(tmp_path)
    host = FakeHost(
        FakeClock(STARTED_AT),
        outcome_uncertain_before_action=("redis", "disable"),
    )
    with pytest.raises(FaultMutationOutcomeUncertainError):
        _run(manifest, identity, host, path)
    host.unavailable_at = None
    host.precondition_disable_before_action = ("redis", "disable")

    with pytest.raises(FaultMutationPreconditionError):
        _run(manifest, identity, host, path)
    assert host.states["redis"] is False
    assert 2 in host.owned_recoveries

    host.clock.value = STARTED_AT + timedelta(seconds=recovery.offset_seconds)
    host._fire_due_timers()
    assert host.states["redis"] is True


def test_existing_claim_allows_crash_reconciliation_of_disabled_target(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    path = _journal_path(tmp_path)
    host = FakeHost(FakeClock(STARTED_AT), crash_after_target="redis")

    with pytest.raises(SimulatedCrash):
        _run(manifest, identity, host, path)
    assert 2 in host.owned_recoveries

    status, _ = _run(manifest, identity, host, path)

    assert status.status == "completed"
    assert host.calls.count("apply:redis:disable") == 1
    assert 2 in host.owned_recoveries
    assert 2 in host.receipts


def test_identity_drift_after_claim_revokes_future_recovery(tmp_path: Path) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    path = _journal_path(tmp_path)
    host = FakeHost(FakeClock(STARTED_AT), crash_after_target="redis")

    with pytest.raises(SimulatedCrash):
        _run(manifest, identity, host, path)
    assert host.states["redis"] is False
    assert 2 in host.owned_recoveries
    host.identity_overrides["redis"] = "8" * 64

    status, _ = _run(manifest, identity, host, path)

    assert status.status == "failed"
    assert status.failure_code == "fault_target_state_drift"
    assert host.states["redis"] is False
    assert 2 not in host.owned_recoveries


def test_identity_drift_in_disable_result_revokes_future_recovery(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(
        FakeClock(STARTED_AT),
        identity_drift_after_action=("redis", "disable"),
    )

    status, _ = _run(manifest, identity, host, _journal_path(tmp_path))

    assert status.status == "failed"
    assert status.failure_code == "fault_target_state_drift"
    assert host.states["redis"] is False
    assert 2 not in host.owned_recoveries


def test_api_recreation_after_initial_observation_is_terminal_drift(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    api_disable = manifest.scenario.fault_schedule[2]
    host = FakeHost(
        FakeClock(STARTED_AT),
        identity_drift_at=(
            "api-secondary",
            STARTED_AT + timedelta(seconds=api_disable.offset_seconds),
        ),
    )

    status, _ = _run(manifest, identity, host, _journal_path(tmp_path))

    assert status.status == "failed"
    assert status.failure_code == "fault_target_state_drift"
    assert "apply:api-secondary:disable" not in host.calls


def test_concurrent_target_failure_restores_crashed_disable(tmp_path: Path) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    path = _journal_path(tmp_path)
    host = FakeHost(FakeClock(STARTED_AT), crash_after_target="redis")

    with pytest.raises(SimulatedCrash):
        _run(manifest, identity, host, path)
    assert host.states["redis"] is False
    host.states["api-secondary"] = False

    status, _ = _run(manifest, identity, host, path)

    assert status.status == "failed"
    assert status.failure_code == "fault_target_state_drift"
    assert host.states == {"redis": True, "api-secondary": False}
    assert host.calls.count("apply:redis:disable") == 1
    assert host.calls.count("apply:redis:enable") == 1


def test_timer_arm_finishing_at_trigger_records_failure_not_late_arm(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    first_recovery = manifest.scenario.fault_schedule[1]
    host = FakeHost(
        FakeClock(STARTED_AT),
        arm_delay_seconds=float(first_recovery.offset_seconds),
    )

    status, kinds = _run(manifest, identity, host, _journal_path(tmp_path))

    assert status.status == "failed"
    assert status.failure_code == "fault_slot_missed"
    assert kinds[-1] == "fault_controller_failed"
    assert "recovery_armed" not in kinds
    assert "apply:redis:disable" not in host.calls


def test_disable_finishing_after_recovery_restores_and_fails(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    disable = manifest.scenario.fault_schedule[0]
    recovery = manifest.scenario.fault_schedule[1]
    delay = float(recovery.offset_seconds - disable.offset_seconds + 1)
    host = FakeHost(
        FakeClock(STARTED_AT),
        delay_before_mutation=("redis", "disable", delay),
    )

    status, kinds = _run(manifest, identity, host, _journal_path(tmp_path))

    assert status.status == "failed"
    assert status.failure_code == "fault_slot_missed"
    assert host.states["redis"] is True
    assert kinds.count("fault_action_succeeded") == 0


def test_crash_after_timer_fires_is_reconciled_inside_recovery_deadline(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    path = _journal_path(tmp_path)
    host = FakeHost(FakeClock(STARTED_AT), crash_on_due_observation="redis")

    with pytest.raises(SimulatedCrash):
        _run(manifest, identity, host, path)
    assert host.states["redis"] is True

    status, _ = _run(manifest, identity, host, path)

    assert status.status == "completed"
    assert host.calls.count("apply:redis:disable") == 1


def test_recovery_success_requires_runtime_preflight_before_journaling(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    path = _journal_path(tmp_path)
    host = FakeHost(
        FakeClock(STARTED_AT),
        fail_recovery_preflight_on_call=1,
    )

    with pytest.raises(SimulatedCrash):
        _run(manifest, identity, host, path)

    with FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity) as journal:
        success_slots = [
            record.payload.slot_number
            for record in journal.records
            if record.payload.kind == "fault_action_succeeded"
        ]
        assert success_slots == [1]
        assert all(record.payload.kind != "completed" for record in journal.records)


def test_recovery_success_rechecks_identity_after_runtime_preflight(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    path = _journal_path(tmp_path)
    host = FakeHost(
        FakeClock(STARTED_AT),
        identity_drift_on_recovery_preflight=(1, "redis"),
    )

    status, kinds = _run(manifest, identity, host, path)

    assert status.status == "failed"
    assert status.failure_code == "fault_target_state_drift"
    assert kinds.count("fault_action_succeeded") == 1
    assert "completed" not in kinds


def test_recovery_success_rechecks_deadline_after_runtime_preflight(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(
        FakeClock(STARTED_AT),
        recovery_preflight_delays_seconds=[16.0],
    )

    status, kinds = _run(manifest, identity, host, _journal_path(tmp_path))

    assert status.status == "failed"
    assert status.failure_code == "fault_recovery_deadline_missed"
    assert kinds.count("fault_action_succeeded") == 1
    assert "completed" not in kinds


def test_completed_requires_runtime_preflight_before_journaling(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    path = _journal_path(tmp_path)
    host = FakeHost(
        FakeClock(STARTED_AT),
        fail_recovery_preflight_on_call=3,
    )

    with pytest.raises(SimulatedCrash):
        _run(manifest, identity, host, path)

    with FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity) as journal:
        assert (
            sum(record.payload.kind == "fault_action_succeeded" for record in journal.records) == 4
        )
        assert all(record.payload.kind != "completed" for record in journal.records)


def test_completed_rechecks_targets_after_runtime_preflight(tmp_path: Path) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(
        FakeClock(STARTED_AT),
        identity_drift_on_recovery_preflight=(3, "redis"),
    )

    status, kinds = _run(manifest, identity, host, _journal_path(tmp_path))

    assert status.status == "failed"
    assert status.failure_code == "fault_target_state_drift"
    assert kinds.count("fault_action_succeeded") == 4
    assert "completed" not in kinds


def test_receipt_created_between_receipt_check_and_target_probe_retries(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(
        FakeClock(STARTED_AT),
        defer_receipt_until_observation="redis",
    )

    status, _ = _run(manifest, identity, host, _journal_path(tmp_path))

    assert status.status == "completed"
    assert host.calls.count("receipt:2") >= 2


def test_api_recovery_starting_state_is_retried_until_receipt_and_healthy(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(
        FakeClock(STARTED_AT),
        recovery_pending_counts={"api-secondary": 2},
    )

    status, _ = _run(manifest, identity, host, _journal_path(tmp_path))

    assert status.status == "completed"
    assert host.calls.count("receipt:4") >= 3


def test_recreated_api_in_starting_state_is_terminal_identity_drift(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    api_recovery = manifest.scenario.fault_schedule[3]
    trigger = STARTED_AT + timedelta(seconds=api_recovery.offset_seconds)
    host = FakeHost(
        FakeClock(STARTED_AT),
        recovery_pending_counts={"api-secondary": 1},
        identity_drift_at=("api-secondary", trigger),
    )

    status, _ = _run(manifest, identity, host, _journal_path(tmp_path))

    assert status.status == "failed"
    assert status.failure_code == "fault_target_state_drift"
    assert 4 not in host.owned_recoveries


def test_fallback_waits_for_same_identity_api_starting_to_become_healthy(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    api_recovery = manifest.scenario.fault_schedule[3]
    trigger = STARTED_AT + timedelta(seconds=api_recovery.offset_seconds)
    host = FakeHost(
        FakeClock(STARTED_AT),
        skip_timer_slots={4},
        pending_at=(
            "api-secondary",
            fault_recovery_deadline("api-secondary", trigger),
            3,
        ),
    )

    status, _ = _run(manifest, identity, host, _journal_path(tmp_path))

    assert status.status == "failed"
    assert status.failure_code == "fault_recovery_deadline_missed"
    assert host.states["api-secondary"] is True
    assert 4 not in host.owned_recoveries


def test_fallback_waits_when_manual_api_restore_returns_starting(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(
        FakeClock(STARTED_AT),
        skip_timer_slots={4},
        pending_after_action=("api-secondary", "enable", 3),
    )

    status, _ = _run(manifest, identity, host, _journal_path(tmp_path))

    assert status.status == "failed"
    assert status.failure_code == "fault_recovery_deadline_missed"
    assert host.states["api-secondary"] is True
    assert host.recovery_pending_counts["api-secondary"] == 0
    assert 4 not in host.owned_recoveries


def test_recovery_before_scheduled_trigger_is_terminal_and_revokes_marker(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    recovery = manifest.scenario.fault_schedule[1]
    host = FakeHost(
        FakeClock(STARTED_AT),
        recover_early_at=(
            "redis",
            STARTED_AT + timedelta(seconds=recovery.offset_seconds - 5),
        ),
    )

    status, _ = _run(manifest, identity, host, _journal_path(tmp_path))

    assert status.status == "failed"
    assert status.failure_code == "fault_recovered_early"
    assert host.states["redis"] is True
    assert 2 not in host.owned_recoveries
    assert 2 not in host.receipts


def test_enabled_target_without_success_receipt_cannot_pass_recovery(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(FakeClock(STARTED_AT), write_receipts=False)

    status, _ = _run(manifest, identity, host, _journal_path(tmp_path))

    assert status.status == "failed"
    assert status.failure_code == "fault_recovery_deadline_missed"
    assert host.states["redis"] is True
    assert 2 not in host.owned_recoveries
    assert 2 not in host.receipts


def test_recovery_timeout_restores_target_but_marks_formal_run_failed(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(FakeClock(STARTED_AT), fire_timers=False)
    path = _journal_path(tmp_path)

    status, _ = _run(manifest, identity, host, path)

    assert status.status == "failed"
    assert status.failure_code == "fault_recovery_deadline_missed"
    assert host.states["redis"] is True
    assert "apply:redis:enable" in host.calls
    with FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity) as journal:
        controller = LoadTestFaultController(
            manifest,
            identity,
            journal,
            host,
            clock=host.clock,
        )
        status = controller.status()
        assert status.status == "failed"
        assert status.failure_code == "fault_recovery_deadline_missed"
    resumed, _ = _run(manifest, identity, host, path)
    assert resumed == status


def test_active_recovery_refuses_wrong_host_before_fallback_mutation(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(
        FakeClock(STARTED_AT),
        fire_timers=False,
        fail_recovery_preflight=True,
    )

    with pytest.raises(SimulatedCrash):
        _run(manifest, identity, host, _journal_path(tmp_path))

    assert host.states["redis"] is False
    assert "apply:redis:enable" not in host.calls


def test_resume_after_failure_before_fallback_retries_safe_recovery(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    path = _journal_path(tmp_path)
    host = FakeHost(
        FakeClock(STARTED_AT),
        fire_timers=False,
        crash_before_action=("redis", "enable"),
    )

    with pytest.raises(SimulatedCrash):
        _run(manifest, identity, host, path)
    assert host.states["redis"] is False
    preflight_calls = host.calls.count("preflight")
    host.fail_preflight = True

    status, _ = _run(manifest, identity, host, path)

    assert status.status == "failed"
    assert status.failure_code == "fault_recovery_deadline_missed"
    assert host.states["redis"] is True
    assert host.calls.count("apply:redis:enable") == 1
    assert host.calls.count("preflight") == preflight_calls


def test_terminal_resume_refuses_wrong_recovery_host_before_mutation(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    path = _journal_path(tmp_path)
    host = FakeHost(
        FakeClock(STARTED_AT),
        fire_timers=False,
        crash_before_action=("redis", "enable"),
    )

    with pytest.raises(SimulatedCrash):
        _run(manifest, identity, host, path)
    assert host.states["redis"] is False
    host.fail_recovery_preflight = True

    with pytest.raises(SimulatedCrash):
        _run(manifest, identity, host, path)

    assert host.states["redis"] is False
    assert "apply:redis:enable" not in host.calls


def test_resume_after_recovery_time_restores_without_elapsed_timer_read(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    path = _journal_path(tmp_path)
    host = FakeHost(
        FakeClock(STARTED_AT),
        fire_timers=False,
        crash_after_target="redis",
    )

    with pytest.raises(SimulatedCrash):
        _run(manifest, identity, host, path)
    assert host.states["redis"] is False
    read_calls = host.calls.count("read:2")
    recovery_offset = manifest.scenario.fault_schedule[1].offset_seconds
    host.clock.value = STARTED_AT + timedelta(seconds=recovery_offset)
    host.reject_elapsed_timer_read = True

    status, _ = _run(manifest, identity, host, path)

    assert status.status == "failed"
    assert status.failure_code == "fault_slot_missed"
    assert host.states["redis"] is True
    assert host.calls.count("read:2") == read_calls


def test_expired_disable_resume_refuses_wrong_host_before_restore(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    path = _journal_path(tmp_path)
    host = FakeHost(
        FakeClock(STARTED_AT),
        fire_timers=False,
        crash_after_target="redis",
    )

    with pytest.raises(SimulatedCrash):
        _run(manifest, identity, host, path)
    recovery_offset = manifest.scenario.fault_schedule[1].offset_seconds
    host.clock.value = STARTED_AT + timedelta(seconds=recovery_offset)
    host.fail_recovery_preflight = True

    with pytest.raises(SimulatedCrash):
        _run(manifest, identity, host, path)

    assert host.states["redis"] is False
    assert "apply:redis:enable" not in host.calls


def test_timer_read_crossing_recovery_refuses_wrong_host_before_restore(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    path = _journal_path(tmp_path)
    host = FakeHost(
        FakeClock(STARTED_AT),
        fire_timers=False,
        crash_after_target="redis",
    )

    with pytest.raises(SimulatedCrash):
        _run(manifest, identity, host, path)
    recovery_offset = manifest.scenario.fault_schedule[1].offset_seconds
    host.clock.value = STARTED_AT + timedelta(seconds=recovery_offset - 1)
    host.timer_read_delays_seconds = [0.0, 0.0, 2.0]
    host.fail_recovery_preflight = True

    with pytest.raises(SimulatedCrash):
        _run(manifest, identity, host, path)

    assert host.states["redis"] is False
    assert "apply:redis:enable" not in host.calls


def test_probe_finishing_after_recovery_deadline_cannot_pass(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(
        FakeClock(STARTED_AT),
        delay_due_observation=("redis", 16.0),
    )

    status, _ = _run(manifest, identity, host, _journal_path(tmp_path))

    assert status.status == "failed"
    assert status.failure_code == "fault_recovery_deadline_missed"
    assert host.states["redis"] is True


def test_timer_property_drift_is_terminal_before_disable(tmp_path: Path) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(FakeClock(STARTED_AT), corrupt_timer_read=True)
    path = _journal_path(tmp_path)

    status, _ = _run(manifest, identity, host, path)

    assert status.status == "failed"
    assert status.failure_code == "fault_timer_invalid"
    assert "apply:redis:disable" not in host.calls
    with FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity) as journal:
        failed = journal.records[-1].payload
        assert failed.kind == "fault_controller_failed"
        assert failed.code == "fault_timer_invalid"


def test_timer_property_drift_after_slow_read_remains_valid_terminal(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    recovery_offset = manifest.scenario.fault_schedule[1].offset_seconds
    disable_offset = manifest.scenario.fault_schedule[0].offset_seconds
    host = FakeHost(
        FakeClock(STARTED_AT),
        timer_read_delay_seconds=float(recovery_offset - disable_offset),
        corrupt_timer_read=True,
    )

    status, _ = _run(manifest, identity, host, _journal_path(tmp_path))

    assert status.status == "failed"
    assert status.failure_code == "fault_timer_invalid"
    assert host.states["redis"] is True


@pytest.mark.parametrize("late_record", ["plan", "armed"])
def test_reducer_rejects_recovery_arm_record_at_trigger(
    tmp_path: Path,
    late_record: str,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(FakeClock(STARTED_AT))
    path = _journal_path(tmp_path)
    slot = build_fault_slots(manifest, identity)[1]
    plan = build_timer_plan(slot, identity)

    with FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity) as journal:
        journal.append(
            FaultInitializedPayload(
                kind="fault_initialized",
                identity=identity,
                initial_observations=_initial_observations(),
            ),
            recorded_at=STARTED_AT,
        )
        journal.append(
            RecoveryArmPlannedPayload(kind="recovery_arm_planned", plan=plan),
            recorded_at=plan.trigger_at if late_record == "plan" else STARTED_AT,
        )
        if late_record == "armed":
            journal.append(
                RecoveryArmedPayload(
                    kind="recovery_armed",
                    plan=plan,
                    observation=host._timer_observation(plan),
                ),
                recorded_at=plan.trigger_at,
            )
        controller = LoadTestFaultController(
            manifest,
            identity,
            journal,
            host,
            clock=host.clock,
        )
        with pytest.raises(ControllerRefusal, match="fault_journal_state_invalid"):
            controller.status()


def test_reducer_rejects_disable_plan_after_late_tolerance(tmp_path: Path) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(FakeClock(STARTED_AT))
    path = _journal_path(tmp_path)
    first_slot = build_fault_slots(manifest, identity)[0]

    with FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity) as journal:
        journal.append(
            FaultInitializedPayload(
                kind="fault_initialized",
                identity=identity,
                initial_observations=_initial_observations(),
            ),
            recorded_at=STARTED_AT,
        )
        _append_armed_recovery(journal, manifest, identity, host, 2)
        _append_armed_recovery(journal, manifest, identity, host, 4)
        journal.append(
            FaultActionPlannedPayload(
                kind="fault_action_planned",
                slot_number=1,
                target="redis",
                action="disable",
                scheduled_at=first_slot.scheduled_at,
                command_sha256=first_slot.command_sha256,
                expected_before=host.observe_target("redis"),
            ),
            recorded_at=first_slot.scheduled_at + timedelta(seconds=6),
        )
        controller = LoadTestFaultController(
            manifest,
            identity,
            journal,
            host,
            clock=host.clock,
        )
        with pytest.raises(ControllerRefusal, match="fault_journal_state_invalid"):
            controller.status()


def test_reducer_rejects_success_observed_after_record_time(tmp_path: Path) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(FakeClock(STARTED_AT), crash_after_target="redis")
    path = _journal_path(tmp_path)

    with pytest.raises(SimulatedCrash):
        _run(manifest, identity, host, path)

    with FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity) as journal:
        planned = next(
            record for record in journal.records if record.payload.kind == "fault_action_planned"
        )
        recorded_at = host.clock.now() + timedelta(seconds=1)
        journal.append(
            FaultActionSucceededPayload(
                kind="fault_action_succeeded",
                slot_number=1,
                target="redis",
                action="disable",
                planned_entry_sha256=planned.entry_sha256,
                observation=host.observe_target("redis"),
                observed_at=recorded_at + timedelta(seconds=1),
                reconciled=True,
            ),
            recorded_at=recorded_at,
        )
        controller = LoadTestFaultController(
            manifest,
            identity,
            journal,
            host,
            clock=host.clock,
        )
        with pytest.raises(ControllerRefusal, match="fault_journal_state_invalid"):
            controller.status()


def test_reducer_rejects_recovery_success_after_deadline(tmp_path: Path) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(FakeClock(STARTED_AT), crash_on_due_observation="redis")
    path = _journal_path(tmp_path)

    with pytest.raises(SimulatedCrash):
        _run(manifest, identity, host, path)

    recovery_slot = build_fault_slots(manifest, identity)[1]
    observed_at = fault_recovery_deadline("redis", recovery_slot.scheduled_at) + timedelta(
        microseconds=1
    )
    with FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity) as journal:
        planned = next(
            record
            for record in journal.records
            if record.payload.kind == "recovery_arm_planned"
            and record.payload.plan.slot_number == 2
        )
        journal.append(
            FaultActionSucceededPayload(
                kind="fault_action_succeeded",
                slot_number=2,
                target="redis",
                action="enable",
                planned_entry_sha256=planned.entry_sha256,
                observation=host.observe_target("redis"),
                observed_at=observed_at,
                reconciled=True,
                recovery_receipt=RecoveryExecutionReceipt(
                    unit_name=planned.payload.plan.unit_name,
                    trigger_at=planned.payload.plan.trigger_at,
                    recorded_at=observed_at,
                    receipt_sha256=recovery_execution_receipt_sha256(
                        planned.payload.plan,
                        observed_at,
                    ),
                ),
            ),
            recorded_at=observed_at,
        )
        controller = LoadTestFaultController(
            manifest,
            identity,
            journal,
            host,
            clock=host.clock,
        )
        with pytest.raises(ControllerRefusal, match="fault_journal_state_invalid"):
            controller.status()


def test_reducer_rejects_recovery_receipt_digest_mismatch(tmp_path: Path) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(FakeClock(STARTED_AT), crash_on_due_observation="redis")
    path = _journal_path(tmp_path)

    with pytest.raises(SimulatedCrash):
        _run(manifest, identity, host, path)

    observed_at = host.clock.now()
    with FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity) as journal:
        planned = next(
            record
            for record in journal.records
            if record.payload.kind == "recovery_arm_planned"
            and record.payload.plan.slot_number == 2
        )
        journal.append(
            FaultActionSucceededPayload(
                kind="fault_action_succeeded",
                slot_number=2,
                target="redis",
                action="enable",
                planned_entry_sha256=planned.entry_sha256,
                observation=host.observe_target("redis"),
                observed_at=observed_at,
                reconciled=False,
                recovery_receipt=RecoveryExecutionReceipt(
                    unit_name=planned.payload.plan.unit_name,
                    trigger_at=planned.payload.plan.trigger_at,
                    recorded_at=observed_at,
                    receipt_sha256="9" * 64,
                ),
            ),
            recorded_at=observed_at,
        )
        controller = LoadTestFaultController(
            manifest,
            identity,
            journal,
            host,
            clock=host.clock,
        )

        with pytest.raises(ControllerRefusal, match="fault_journal_state_invalid"):
            controller.status()


def test_reducer_rejects_completed_time_before_final_success(tmp_path: Path) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(FakeClock(STARTED_AT))
    path = _journal_path(tmp_path)
    status, _ = _run(manifest, identity, host, path)
    assert status.status == "completed"
    _rewrite_last_record_payload_time(path, "completed_at", STARTED_AT)

    with FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity) as journal:
        controller = LoadTestFaultController(
            manifest,
            identity,
            journal,
            host,
            clock=host.clock,
        )
        with pytest.raises(ControllerRefusal, match="fault_journal_state_invalid"):
            controller.status()


def test_reducer_rejects_failure_for_unreached_future_slot(tmp_path: Path) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(FakeClock(STARTED_AT), crash_on_due_observation="redis")
    path = _journal_path(tmp_path)

    with pytest.raises(SimulatedCrash):
        _run(manifest, identity, host, path)

    with FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity) as journal:
        journal.append(
            FaultControllerFailedPayload(
                kind="fault_controller_failed",
                code="fault_target_state_drift",
                slot_number=4,
                target="api-secondary",
            ),
            recorded_at=host.clock.now(),
        )
        controller = LoadTestFaultController(
            manifest,
            identity,
            journal,
            host,
            clock=host.clock,
        )
        with pytest.raises(ControllerRefusal, match="fault_journal_state_invalid"):
            controller.status()


def test_reducer_rejects_recovery_failure_before_deadline(tmp_path: Path) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(FakeClock(STARTED_AT), crash_on_due_observation="redis")
    path = _journal_path(tmp_path)

    with pytest.raises(SimulatedCrash):
        _run(manifest, identity, host, path)

    with FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity) as journal:
        journal.append(
            FaultControllerFailedPayload(
                kind="fault_controller_failed",
                code="fault_recovery_deadline_missed",
                slot_number=2,
                target="redis",
            ),
            recorded_at=host.clock.now(),
        )
        controller = LoadTestFaultController(
            manifest,
            identity,
            journal,
            host,
            clock=host.clock,
        )
        with pytest.raises(ControllerRefusal, match="fault_journal_state_invalid"):
            controller.status()


@pytest.mark.parametrize("include_second_plan", [False, True])
def test_resume_after_unfinished_timer_deadline_records_terminal_failure(
    tmp_path: Path,
    include_second_plan: bool,
) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(FakeClock(STARTED_AT))
    path = _journal_path(tmp_path)

    with FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity) as journal:
        journal.append(
            FaultInitializedPayload(
                kind="fault_initialized",
                identity=identity,
                initial_observations=_initial_observations(),
            ),
            recorded_at=STARTED_AT,
        )
        _append_armed_recovery(journal, manifest, identity, host, 2)
        second_plan = build_timer_plan(build_fault_slots(manifest, identity)[3], identity)
        if include_second_plan:
            journal.append(
                RecoveryArmPlannedPayload(
                    kind="recovery_arm_planned",
                    plan=second_plan,
                ),
                recorded_at=STARTED_AT,
            )

    host.clock.value = second_plan.trigger_at
    status, kinds = _run(manifest, identity, host, path)

    assert status.status == "failed"
    assert status.failure_code == "fault_slot_missed"
    assert "arm:4" not in host.calls
    assert kinds.count("recovery_arm_planned") == (2 if include_second_plan else 1)


def test_semantically_out_of_order_record_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest()
    identity = _identity(manifest)
    host = FakeHost(FakeClock(STARTED_AT))
    path = _journal_path(tmp_path)
    first_fault = manifest.scenario.fault_schedule[0]

    with FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity) as journal:
        journal.append(
            FaultInitializedPayload(
                kind="fault_initialized",
                identity=identity,
                initial_observations=_initial_observations(),
            )
        )
        journal.append(
            FaultActionPlannedPayload(
                kind="fault_action_planned",
                slot_number=1,
                target=first_fault.target,
                action=first_fault.action,
                scheduled_at=STARTED_AT + timedelta(seconds=first_fault.offset_seconds),
                command_sha256=identity.commands[0].command_sha256,
                expected_before=host.observe_target("redis"),
            )
        )
        controller = LoadTestFaultController(
            manifest,
            identity,
            journal,
            host,
            clock=host.clock,
        )
        with pytest.raises(ControllerRefusal, match="fault_journal_state_invalid"):
            controller.status()
