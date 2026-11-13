from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from quiz_app.load_test import fault_controller_journal as journal_module
from quiz_app.load_test.controller_contracts import (
    ControllerContext,
    ControllerRefusal,
    canonical_json_bytes,
)
from quiz_app.load_test.fault_controller_contracts import (
    FaultActionPlannedPayload,
    FaultCommandIdentity,
    FaultControllerCompletedPayload,
    FaultControllerFailedPayload,
    FaultControllerIdentity,
    FaultControllerStatus,
    FaultInitializedPayload,
    FaultMutationResult,
    FaultReleaseIdentity,
    FaultTargetObservation,
    FaultTopologyIdentity,
    RecoveryArmedPayload,
    RecoveryArmPlannedPayload,
    RecoveryTimerObservation,
    RecoveryTimerPlan,
    fault_recovery_offset_for_round,
)
from quiz_app.load_test.fault_controller_journal import FaultControllerJournal
from quiz_app.load_test.manifest import LoadTestManifest, build_load_test_manifest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RUN_ID = UUID("10000000-0000-4000-8000-000000000001")
EVENT_ID = UUID("10000000-0000-4000-8000-000000000002")
RECORDED_AT = datetime(2026, 9, 3, 1, 2, 3, tzinfo=UTC)
IMAGE = f"registry.example.test/quiz/api@sha256:{'1' * 64}"


def _secure_directory(base: Path) -> Path:
    directory = base / "fault-controller-artifacts"
    directory.mkdir(mode=0o700, parents=True)
    directory.chmod(0o700)
    return directory


def _manifest() -> LoadTestManifest:
    return build_load_test_manifest(
        run_id=RUN_ID,
        event_id=EVENT_ID,
        event_slug="load-test-2026-09-03",
        scheduled_start_at=RECORDED_AT + timedelta(hours=1),
        public_origin="https://quiz.aws.internal",
        source_commit="a" * 40,
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
        created_at=RECORDED_AT,
    )


def _context() -> ControllerContext:
    return ControllerContext(
        run_id=RUN_ID,
        event_id=EVENT_ID,
        event_slug="load-test-2026-09-03",
        public_origin="https://quiz.aws.internal",
        source_commit="a" * 40,
        schema_revision="schema-v1",
        scheduled_start_at=RECORDED_AT + timedelta(hours=1),
        manifest_sha256="b" * 64,
        scenario_sha256="c" * 64,
    )


def _identity(
    *,
    command_digest: str = "4",
    api_probe_digest: str = "8",
) -> FaultControllerIdentity:
    operations = (
        (1, "redis", "disable", "1"),
        (2, "redis", "enable", "2"),
        (3, "api-secondary", "disable", "3"),
        (4, "api-secondary", "enable", command_digest),
    )
    return FaultControllerIdentity(
        context=_context(),
        release=FaultReleaseIdentity(
            release_id="load-test-candidate",
            release_manifest_sha256="d" * 64,
            source_commit="a" * 40,
            rollback_release_id="load-test-rollback",
            rollback_manifest_sha256="e" * 64,
            execution_host="quiz.example.internal",
            docker_context="default",
        ),
        topology=FaultTopologyIdentity(
            configuration_sha256="f" * 64,
            docker_compose_version="2.39.4",
            docker_engine_version="28.0.1",
            fault_control_network_sha256="8" * 64,
            toxiproxy_container_sha256="7" * 64,
            probe_command_sha256="e" * 64,
            api_probe_command_sha256=api_probe_digest * 64,
            recovery_compose_sha256="9" * 64,
            proxy_name="gs-app-tomidaisai-aws-demo-redis",
            control_origin="http://172.30.251.2:8474",
            redis_hostname="redis.example.internal",
            listen="172.30.250.2:6379",
            upstream="10.0.1.20:6379",
        ),
        commands=tuple(
            FaultCommandIdentity(
                slot_number=slot,
                target=target,
                action=action,
                command_sha256=digest * 64,
            )
            for slot, target, action, digest in operations
        ),
    )


def _initialized(identity: FaultControllerIdentity) -> FaultInitializedPayload:
    return FaultInitializedPayload(
        kind="fault_initialized",
        identity=identity,
        initial_observations=(
            FaultTargetObservation(
                target="redis",
                enabled=True,
                identity_sha256="1" * 64,
            ),
            FaultTargetObservation(
                target="api-secondary",
                enabled=True,
                identity_sha256="2" * 64,
            ),
        ),
    )


def _timer_plan() -> RecoveryTimerPlan:
    return RecoveryTimerPlan(
        slot_number=2,
        target="redis",
        trigger_at=RECORDED_AT + timedelta(minutes=5),
        unit_name="load-test-redis-recovery.timer",
        command_sha256="2" * 64,
    )


def _append_initialized(path: Path, identity: FaultControllerIdentity) -> None:
    with FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity) as journal:
        journal.append(_initialized(identity), recorded_at=RECORDED_AT)


def _json_records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="ascii").splitlines()]


def _write_records(path: Path, records: list[dict[str, object]]) -> None:
    path.write_bytes(b"\n".join(canonical_json_bytes(record) for record in records) + b"\n")
    path.chmod(0o600)


def _rehash(record: dict[str, object]) -> None:
    unsigned = {key: value for key, value in record.items() if key != "entry_sha256"}
    record["entry_sha256"] = sha256(canonical_json_bytes(unsigned)).hexdigest()


def test_fault_contracts_are_strict_frozen_and_relation_checked() -> None:
    observation = FaultTargetObservation(
        target="redis",
        enabled=True,
        identity_sha256="a" * 64,
    )
    with pytest.raises(ValidationError):
        FaultTargetObservation.model_validate(
            {
                "target": "redis",
                "enabled": 1,
                "identity_sha256": "a" * 64,
            }
        )
    with pytest.raises(ValidationError):
        FaultTargetObservation.model_validate(
            {
                **observation.model_dump(),
                "credential": "must-not-be-accepted",
            }
        )
    with pytest.raises(ValidationError):
        observation.enabled = False
    with pytest.raises(ValidationError):
        FaultActionPlannedPayload(
            kind="fault_action_planned",
            slot_number=1,
            target="redis",
            action="disable",
            scheduled_at=RECORDED_AT,
            command_sha256="1" * 64,
            expected_before=FaultTargetObservation(
                target="api-secondary",
                enabled=True,
                identity_sha256="b" * 64,
            ),
        )


def test_recovery_timer_observation_must_match_plan() -> None:
    plan = _timer_plan()
    with pytest.raises(ValidationError):
        RecoveryArmedPayload(
            kind="recovery_armed",
            plan=plan,
            observation=RecoveryTimerObservation(
                unit_name=plan.unit_name,
                trigger_at=plan.trigger_at + timedelta(seconds=1),
                properties_sha256="9" * 64,
            ),
        )


def test_mutation_result_requires_stable_target_identity() -> None:
    before = FaultTargetObservation(
        target="redis",
        enabled=True,
        identity_sha256="a" * 64,
    )
    with pytest.raises(ValidationError):
        FaultMutationResult(
            before=before,
            after=FaultTargetObservation(
                target="redis",
                enabled=False,
                identity_sha256="b" * 64,
            ),
            reconciled=False,
        )


def test_terminal_status_and_failure_are_secret_free_and_cross_validated() -> None:
    failure = FaultControllerFailedPayload(
        kind="fault_controller_failed",
        code="fault_recovery_fallback_applied",
        slot_number=2,
        target="redis",
    )
    status = FaultControllerStatus(
        status="failed",
        next_slot=None,
        journal_head_sha256="a" * 64,
        failure_code=failure.code,
    )
    assert status.failure_code == "fault_recovery_fallback_applied"
    assert (
        FaultControllerStatus(
            status="finalizing",
            next_slot=None,
            journal_head_sha256="a" * 64,
            failure_code=None,
        ).status
        == "finalizing"
    )
    with pytest.raises(ValidationError):
        FaultControllerStatus(
            status="completed",
            next_slot=4,
            journal_head_sha256="a" * 64,
            failure_code=None,
        )


def test_fault_recovery_offset_is_derived_from_round_condition_and_manifest() -> None:
    manifest = _manifest()

    assert fault_recovery_offset_for_round(manifest, 5) == 2580
    assert fault_recovery_offset_for_round(manifest, 7) == 3300
    assert fault_recovery_offset_for_round(manifest, 1) is None
    with pytest.raises(ControllerRefusal, match="^controller_round_invalid$"):
        fault_recovery_offset_for_round(manifest, 0)


def test_journal_is_0600_hash_chained_and_survives_reopen(tmp_path: Path) -> None:
    identity = _identity()
    path = _secure_directory(tmp_path) / "fault-controller.journal"
    plan = _timer_plan()

    with FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity) as journal:
        first = journal.append(_initialized(identity), recorded_at=RECORDED_AT)
        second = journal.append(
            RecoveryArmPlannedPayload(kind="recovery_arm_planned", plan=plan),
            recorded_at=RECORDED_AT + timedelta(seconds=1),
        )
        assert second.previous_sha256 == first.entry_sha256
        assert journal.head_sha256 == second.entry_sha256
        assert journal.records == (first, second)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity) as reopened:
        assert reopened.records == (first, second)


def test_journal_rejects_reversed_time_before_write(tmp_path: Path) -> None:
    identity = _identity()
    path = _secure_directory(tmp_path) / "fault-controller.journal"

    with FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity) as journal:
        first = journal.append(_initialized(identity), recorded_at=RECORDED_AT)
        with pytest.raises(
            ControllerRefusal,
            match="^fault_controller_journal_time_reversed$",
        ):
            journal.append(
                RecoveryArmPlannedPayload(
                    kind="recovery_arm_planned",
                    plan=_timer_plan(),
                ),
                recorded_at=RECORDED_AT - timedelta(microseconds=1),
            )
        assert journal.records == (first,)

    with FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity) as reopened:
        assert reopened.records == (first,)


def test_journal_rejects_naive_time_before_comparing_records(tmp_path: Path) -> None:
    identity = _identity()
    path = _secure_directory(tmp_path) / "fault-controller.journal"

    with FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity) as journal:
        first = journal.append(_initialized(identity), recorded_at=RECORDED_AT)
        with pytest.raises(
            ControllerRefusal,
            match="^fault_controller_journal_time_invalid$",
        ):
            journal.append(
                RecoveryArmPlannedPayload(
                    kind="recovery_arm_planned",
                    plan=_timer_plan(),
                ),
                recorded_at=RECORDED_AT.replace(tzinfo=None),
            )
        assert journal.records == (first,)


@pytest.mark.parametrize("mode", [0o750, 0o777])
def test_journal_requires_external_directory_exactly_0700(
    tmp_path: Path,
    mode: int,
) -> None:
    directory = _secure_directory(tmp_path)
    directory.chmod(mode)

    with (
        pytest.raises(
            ControllerRefusal,
            match="^fault_controller_journal_directory_invalid$",
        ),
        FaultControllerJournal(
            directory / "fault-controller.journal",
            REPOSITORY_ROOT,
            identity=_identity(),
        ),
    ):
        pass


def test_journal_rejects_existing_file_not_0600(tmp_path: Path) -> None:
    directory = _secure_directory(tmp_path)
    path = directory / "fault-controller.journal"
    path.touch(mode=0o600)
    path.chmod(0o640)

    with (
        pytest.raises(ControllerRefusal, match="^fault_controller_journal_invalid$"),
        FaultControllerJournal(path, REPOSITORY_ROOT, identity=_identity()),
    ):
        pass


def test_journal_open_uses_nofollow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = _secure_directory(tmp_path)
    path = directory / "fault-controller.journal"
    real_open = os.open
    journal_flags: list[int] = []

    def recording_open(
        path_argument: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if str(path_argument) == path.name:
            journal_flags.append(flags)
        return real_open(path_argument, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(journal_module.os, "open", recording_open)
    with FaultControllerJournal(path, REPOSITORY_ROOT, identity=_identity()):
        pass

    assert journal_flags[0] & getattr(os, "O_NOFOLLOW", 0)
    assert journal_flags[0] & os.O_EXCL


def test_journal_rejects_symlink_and_hardlink(tmp_path: Path) -> None:
    identity = _identity()
    for link_kind in ("symlink", "hardlink"):
        directory = _secure_directory(tmp_path / link_kind)
        target = tmp_path / f"{link_kind}-target"
        target.touch(mode=0o600)
        target.chmod(0o600)
        path = directory / "fault-controller.journal"
        if link_kind == "symlink":
            path.symlink_to(target)
        else:
            os.link(target, path)
        with (
            pytest.raises(ControllerRefusal, match="^fault_controller_journal_invalid$"),
            FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity),
        ):
            pass


def test_journal_requires_initialized_identity_as_first_record(tmp_path: Path) -> None:
    identity = _identity()
    path = _secure_directory(tmp_path) / "fault-controller.journal"

    with (
        FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity) as journal,
        pytest.raises(
            ControllerRefusal,
            match="^fault_controller_journal_identity_required$",
        ),
    ):
        journal.append(
            RecoveryArmPlannedPayload(
                kind="recovery_arm_planned",
                plan=_timer_plan(),
            )
        )


def test_journal_rejects_expected_identity_change_on_reopen(tmp_path: Path) -> None:
    path = _secure_directory(tmp_path) / "fault-controller.journal"
    _append_initialized(path, _identity())

    with (
        pytest.raises(
            ControllerRefusal,
            match="^fault_controller_journal_chain_invalid$",
        ),
        FaultControllerJournal(
            path,
            REPOSITORY_ROOT,
            identity=_identity(api_probe_digest="9"),
        ),
    ):
        pass


def test_journal_rejects_partial_final_line(tmp_path: Path) -> None:
    identity = _identity()
    path = _secure_directory(tmp_path) / "fault-controller.journal"
    _append_initialized(path, identity)
    path.write_bytes(path.read_bytes().removesuffix(b"\n"))

    with (
        pytest.raises(ControllerRefusal, match="^fault_controller_journal_invalid$"),
        FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity),
    ):
        pass


def test_journal_io_completes_partial_reads_and_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    path = _secure_directory(tmp_path) / "fault-controller.journal"
    real_read = os.read
    real_write = os.write
    read_calls = 0
    write_calls = 0

    def partial_read(descriptor: int, amount: int) -> bytes:
        nonlocal read_calls
        read_calls += 1
        return real_read(descriptor, min(amount, 13))

    def partial_write(descriptor: int, body: bytes) -> int:
        nonlocal write_calls
        write_calls += 1
        return real_write(descriptor, body[:17])

    monkeypatch.setattr(os, "read", partial_read)
    monkeypatch.setattr(os, "write", partial_write)
    _append_initialized(path, identity)
    with FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity) as journal:
        assert len(journal.records) == 1

    assert read_calls > 2
    assert write_calls > 2


def test_journal_rejects_entry_tamper(tmp_path: Path) -> None:
    identity = _identity()
    path = _secure_directory(tmp_path) / "fault-controller.journal"
    _append_initialized(path, identity)
    records = _json_records(path)
    records[0]["entry_sha256"] = "0" * 64
    _write_records(path, records)

    with (
        pytest.raises(
            ControllerRefusal,
            match="^fault_controller_journal_chain_invalid$",
        ),
        FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity),
    ):
        pass


def test_journal_rejects_initialized_identity_tamper_with_recomputed_hash(
    tmp_path: Path,
) -> None:
    identity = _identity()
    path = _secure_directory(tmp_path) / "fault-controller.journal"
    _append_initialized(path, identity)
    records = _json_records(path)
    payload = records[0]["payload"]
    assert isinstance(payload, dict)
    payload_identity = payload["identity"]
    assert isinstance(payload_identity, dict)
    topology = payload_identity["topology"]
    assert isinstance(topology, dict)
    topology["configuration_sha256"] = "0" * 64
    _rehash(records[0])
    _write_records(path, records)

    with (
        pytest.raises(
            ControllerRefusal,
            match="^fault_controller_journal_identity_invalid$",
        ),
        FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity),
    ):
        pass


def test_journal_process_lifetime_lock_refuses_second_writer(tmp_path: Path) -> None:
    identity = _identity()
    path = _secure_directory(tmp_path) / "fault-controller.journal"

    with (
        FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity),
        pytest.raises(
            ControllerRefusal,
            match="^fault_controller_journal_locked$",
        ),
        FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity),
    ):
        pass

    with FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity):
        pass


def test_append_fsyncs_before_record_becomes_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    path = _secure_directory(tmp_path) / "fault-controller.journal"
    real_fsync = os.fsync
    synced_descriptors: list[int] = []

    def recording_fsync(descriptor: int) -> None:
        synced_descriptors.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(journal_module.os, "fsync", recording_fsync)
    with FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity) as journal:
        synced_descriptors.clear()
        descriptor = journal._descriptor
        record = journal.append(_initialized(identity), recorded_at=RECORDED_AT)
        assert synced_descriptors == [descriptor]
        assert journal.records == (record,)


def test_append_detects_path_replacement_after_open(tmp_path: Path) -> None:
    identity = _identity()
    directory = _secure_directory(tmp_path)
    path = directory / "fault-controller.journal"
    replacement = directory / "replacement"

    with FaultControllerJournal(path, REPOSITORY_ROOT, identity=identity) as journal:
        replacement.touch(mode=0o600)
        replacement.chmod(0o600)
        os.replace(replacement, path)
        with pytest.raises(
            ControllerRefusal,
            match="^fault_controller_journal_invalid$",
        ):
            journal.append(_initialized(identity), recorded_at=RECORDED_AT)


def test_completed_payload_keeps_only_normalized_observations() -> None:
    payload = FaultControllerCompletedPayload(
        kind="completed",
        final_action_entry_sha256="a" * 64,
        final_observations=(
            FaultTargetObservation(
                target="redis",
                enabled=True,
                identity_sha256="b" * 64,
            ),
            FaultTargetObservation(
                target="api-secondary",
                enabled=True,
                identity_sha256="c" * 64,
            ),
        ),
        completed_at=RECORDED_AT,
    )

    assert set(payload.model_dump()) == {
        "kind",
        "final_action_entry_sha256",
        "final_observations",
        "completed_at",
    }
