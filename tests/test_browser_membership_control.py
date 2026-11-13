from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from test_browser_membership import COMMIT, FINGERPRINT, KEY, RUN_ID, fixture, metadata

from quiz_app.observability import browser_membership as membership
from quiz_app.observability.browser_membership_models import (
    BrowserEvent,
    BrowserManifest,
    BrowserRun,
    ManualParticipantAllowlist,
    MembershipReport,
    MembershipSnapshot,
    PlannedWorker,
    RecordingHealth,
    StoredAnswer,
)


def controlled_fixture(total: int) -> tuple[MembershipSnapshot, list[BrowserRun], BrowserManifest]:
    workers: list[PlannedWorker] = []
    runs: list[BrowserRun] = []
    snapshots: list[MembershipSnapshot] = []
    for first in range(1, total + 1, 100):
        count = min(100, total - first + 1)
        snapshot, run = fixture(count, first)
        run.events[-1].reason = "completed"
        run.events.append(
            BrowserEvent(
                event="participant_shutdown_finished",
                monotonicMs=101,
                lifecycleVersion=2,
                reason="completed",
            )
        )
        for sequence, event in enumerate(run.events):
            event.sequence = sequence
            if event.event == "click_planned":
                event.event = "manual_answer_clicked"
        worker_id = f"worker-{len(workers) + 1}"
        run.conditions.worker_id = worker_id
        workers.append(PlannedWorker(workerId=worker_id, count=count, ordinalStart=first))
        run.recording_health = RecordingHealth(
            state="closed",
            complete=True,
            attemptedRecords=len(run.events),
            acceptedRecords=len(run.events),
            writtenRecords=len(run.events),
            droppedRecords=0,
            failedRecords=0,
            pendingRecords=0,
        )
        snapshots.append(snapshot)
        runs.append(run)
    combined = snapshots[0].model_copy(deep=True)
    combined.participants = [key for snapshot in snapshots for key in snapshot.participants]
    combined.answers = [answer for snapshot in snapshots for answer in snapshot.answers]
    combined.metadata = metadata(total, total * 6)
    manifest = BrowserManifest(
        format="browser-control-manifest-v1",
        runId=RUN_ID,
        sourceCommit=COMMIT,
        totalVus=total,
        expectedRounds=6,
        manualParticipantCount=0,
        workers=workers,
    )
    return combined, runs, manifest


def add_manual_participant(snapshot: MembershipSnapshot) -> ManualParticipantAllowlist:
    key = membership.participant_identity(KEY, RUN_ID, "ZZZ")
    snapshot.participants.append(key)
    snapshot.answers.extend(StoredAnswer(participant_key=key, round_number=n) for n in range(1, 7))
    snapshot.metadata.participant_count += 1
    snapshot.metadata.answer_count += 6
    return ManualParticipantAllowlist(
        run_id=RUN_ID, identity_key_fingerprint=FINGERPRINT, participant_keys=[key]
    )


@pytest.mark.parametrize("total", [1, 50, 100, 300, 650])
def test_variable_run_uses_predeclared_machine_denominator(total: int) -> None:
    snapshot, runs, manifest = controlled_fixture(total)
    report = membership.build_report(snapshot, runs, manifest)
    assert report.decision == "PASS"
    assert report.expected_participants == total
    assert report.expected_pairs == total * 6
    assert all(comparison.distinct_pairs == total * 6 for comparison in report.comparisons.values())


def test_stopped_worker_and_unconnected_vu_do_not_shrink_denominator() -> None:
    snapshot, runs, manifest = controlled_fixture(150)
    runs[0].events = [event for event in runs[0].events if event.serial != 100]
    report = membership.build_report(snapshot, runs[:1], manifest)
    assert report.decision == "FAIL"
    assert report.expected_pairs == 900
    assert report.missing_workers == ["worker-2"]
    assert report.unmapped_browser_participants == list(range(100, 151))
    assert len(report.comparisons["clicks"].missing) == 306


def test_all_workers_missing_still_reports_every_planned_pair() -> None:
    snapshot, _runs, manifest = controlled_fixture(650)
    report = membership.build_report(snapshot, [], manifest)
    assert report.expected_pairs == 3900
    assert len(report.missing_workers) == 7
    assert len(report.comparisons["stored"].missing) == 3900
    assert report.decision == "FAIL"


@pytest.mark.parametrize(
    "marker",
    [
        "participant_run_started",
        "participant_measurement_started",
        "participant_run_finished",
        "participant_shutdown_finished",
    ],
)
def test_incomplete_worker_retains_denominator_and_cannot_pass(marker: str) -> None:
    snapshot, runs, manifest = controlled_fixture(50)
    runs[0].events = [event for event in runs[0].events if event.event != marker]
    report = membership.build_report(snapshot, runs, manifest)
    assert report.expected_pairs == 300
    assert report.incomplete_workers == ["worker-1"]
    assert report.decision == "FAIL"


def test_manual_participant_is_distinct_from_vus_and_unknowns_still_fail() -> None:
    snapshot, runs, manifest = controlled_fixture(50)
    allowlist = add_manual_participant(snapshot)
    manifest.manual_participant_count = 1
    report = membership.build_report(snapshot, runs, manifest, allowlist)
    assert report.decision == "PASS"
    assert report.expected_participants == 50
    assert report.expected_pairs == 300
    assert report.expected_manual_participants == report.observed_manual_participants == 1
    assert report.observed_manual_answers == 6
    assert report.unmapped_database_participants == report.unmapped_database_answers == 0
    unknown = membership.participant_identity(KEY, RUN_ID, "ZZY")
    snapshot.participants.append(unknown)
    snapshot.answers.append(StoredAnswer(participant_key=unknown, round_number=1))
    snapshot.metadata.participant_count += 1
    snapshot.metadata.answer_count += 1
    report = membership.build_report(snapshot, runs, manifest, allowlist)
    assert report.decision == "FAIL"
    assert report.unmapped_database_participants == report.unmapped_database_answers == 1


def test_absent_allowlist_does_not_exempt_manual_or_machine_rows() -> None:
    snapshot, runs, manifest = controlled_fixture(50)
    add_manual_participant(snapshot)
    manifest.manual_participant_count = 1
    report = membership.build_report(snapshot, runs, manifest)
    assert report.decision == "FAIL"
    assert report.missing_manual_participants == 1
    assert report.unmapped_database_participants == 1
    assert report.unmapped_database_answers == 6


def test_manual_key_cannot_be_used_to_exempt_machine_identity() -> None:
    snapshot, runs, manifest = controlled_fixture(50)
    manifest.manual_participant_count = 1
    allowlist = ManualParticipantAllowlist(
        run_id=RUN_ID,
        identity_key_fingerprint=FINGERPRINT,
        participant_keys=[snapshot.participants[0]],
    )
    report = membership.build_report(snapshot, runs, manifest, allowlist)
    assert report.decision == "FAIL"
    assert report.manual_identity_conflicts == 1


def test_expected_manual_participant_missing_from_database_fails() -> None:
    snapshot, runs, manifest = controlled_fixture(50)
    manifest.manual_participant_count = 1
    allowlist = ManualParticipantAllowlist(
        run_id=RUN_ID,
        identity_key_fingerprint=FINGERPRINT,
        participant_keys=[membership.participant_identity(KEY, RUN_ID, "ZZZ")],
    )
    report = membership.build_report(snapshot, runs, manifest, allowlist)
    assert report.decision == "FAIL"
    assert report.missing_manual_participants == 1


@pytest.mark.parametrize(
    "case",
    ["sum", "gap", "overlap", "worker", "limit", "fraction", "manual_limit", "worker_id", "format"],
)
def test_invalid_manifest_is_rejected_before_analysis(case: str) -> None:
    _snapshot, _runs, manifest = controlled_fixture(150)
    body = manifest.model_dump(by_alias=True)
    if case == "sum":
        body["totalVus"] = 149
    elif case == "gap":
        body["workers"][1]["ordinalStart"] = 102
    elif case == "overlap":
        body["workers"][1]["ordinalStart"] = 100
    elif case == "worker":
        body["workers"][1]["workerId"] = "worker-1"
    elif case == "fraction":
        body["totalVus"] = 150.1
    elif case == "manual_limit":
        body["manualParticipantCount"] = 101
    elif case == "worker_id":
        body["workers"][0]["workerId"] = "arbitrary-worker"
    elif case == "format":
        body.pop("format")
    else:
        body["totalVus"] = 651
    with pytest.raises(ValidationError):
        BrowserManifest.model_validate(body)


def test_manifest_binding_worker_binding_and_allowlist_key_binding_are_required() -> None:
    snapshot, runs, manifest = controlled_fixture(50)
    manifest.run_id = "other-run"
    with pytest.raises(ValueError, match="manifest_binding"):
        membership.build_report(snapshot, runs, manifest)
    manifest.run_id = RUN_ID
    runs[0].conditions.worker_id = "unknown"
    with pytest.raises(ValueError, match="worker_binding"):
        membership.build_report(snapshot, runs, manifest)
    runs[0].conditions.worker_id = "worker-1"
    allowlist = ManualParticipantAllowlist(
        run_id=RUN_ID, identity_key_fingerprint="f" * 64, participant_keys=[]
    )
    with pytest.raises(ValueError, match="allowlist_binding"):
        membership.build_report(snapshot, runs, manifest, allowlist)


@pytest.mark.parametrize(
    "case", ["missing", "open", "loss", "pending", "cardinality", "truncated", "sequence"]
)
def test_recording_loss_cannot_be_a_complete_membership_pass(case: str) -> None:
    snapshot, runs, manifest = controlled_fixture(50)
    health = runs[0].recording_health
    assert health is not None
    if case == "missing":
        runs[0].recording_health = None
    elif case == "open":
        health.state = "open"
    elif case == "loss":
        health.dropped_records = 1
    elif case == "pending":
        health.pending_records = 1
    elif case == "cardinality":
        health.written_records -= 1
    elif case == "truncated":
        runs[0].events.pop()
    else:
        runs[0].events[4].sequence = 123456
    report = membership.build_report(snapshot, runs, manifest)
    assert report.decision == "FAIL"
    assert report.recording_incomplete_workers == ["worker-1"]
    assert report.expected_pairs == 300


def test_manual_allowlist_file_is_private_anonymous_and_duplicate_safe(tmp_path: Path) -> None:
    labels = tmp_path / "labels.json"
    labels.write_text('{"labels":["ABC"]}')
    labels.chmod(0o600)
    allowlist = membership.manual_allowlist(labels, KEY, RUN_ID)
    assert allowlist.participant_keys == [membership.participant_identity(KEY, RUN_ID, "ABC")]
    assert "ABC" not in allowlist.model_dump_json()
    labels.chmod(0o644)
    with pytest.raises(ValueError, match="labels_file_invalid"):
        membership.manual_allowlist(labels, KEY, RUN_ID)
    labels.chmod(0o600)
    labels.write_text('{"labels":["ABC","ABC"]}')
    with pytest.raises(ValidationError, match="duplicate"):
        membership.manual_allowlist(labels, KEY, RUN_ID)


def test_offline_cli_with_no_worker_files_preserves_manifest_and_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, _runs, manifest = controlled_fixture(50)
    snapshot_path, manifest_path, output = (
        tmp_path / "snapshot.json",
        tmp_path / "manifest.json",
        tmp_path / "report.json",
    )
    snapshot_path.write_text(snapshot.model_dump_json())
    manifest_path.write_text(manifest.model_dump_json(by_alias=True))
    engine = MagicMock(side_effect=AssertionError("offline comparison cannot access DB"))
    monkeypatch.setattr(membership, "get_engine", engine)
    monkeypatch.setattr(membership.logfire, "configure", MagicMock())
    monkeypatch.setattr(membership.logfire, "info", MagicMock())
    assert (
        membership.main(
            [
                "analyze",
                "--snapshot",
                str(snapshot_path),
                "--manifest",
                str(manifest_path),
                "--output",
                str(output),
            ]
        )
        == 1
    )
    report = MembershipReport.model_validate_json(output.read_text())
    assert report.expected_pairs == 300
    assert report.missing_workers == ["worker-1"]
    assert set(report.input_sha256) == {"snapshot", "manifest"}
    engine.assert_not_called()


def test_command_plan_and_unanswered_dom_are_not_completed_answer_pairs() -> None:
    snapshot, runs, manifest = controlled_fixture(50)
    click = next(event for event in runs[0].events if event.event == "manual_answer_clicked")
    result = next(event for event in runs[0].events if event.event == "result_dom")
    click.event = "manual_command_accepted"
    result.reason = "unanswered"
    report = membership.build_report(snapshot, runs, manifest)
    assert report.decision == "FAIL"
    assert len(report.comparisons["clicks"].missing) == 1
    assert len(report.comparisons["results"].missing) == 1


def test_manual_allowlist_cli_and_variable_analysis_are_offline_and_seal_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, runs, manifest = controlled_fixture(50)
    add_manual_participant(snapshot)
    manifest.manual_participant_count = 1
    labels, key, allowlist = tmp_path / "labels.json", tmp_path / "key", tmp_path / "allowlist.json"
    labels.write_text('{"labels":["ZZZ"]}')
    labels.chmod(0o600)
    key.write_text(KEY.hex())
    key.chmod(0o600)
    engine = MagicMock(side_effect=AssertionError("offline commands cannot access DB"))
    monkeypatch.setattr(membership, "get_engine", engine)
    monkeypatch.setattr(membership.logfire, "configure", MagicMock())
    monkeypatch.setattr(membership.logfire, "info", MagicMock())
    assert (
        membership.main(
            [
                "manual-allowlist",
                "--run-id",
                RUN_ID,
                "--identity-key-file",
                str(key),
                "--labels-file",
                str(labels),
                "--output",
                str(allowlist),
            ]
        )
        == 0
    )
    assert "ZZZ" not in allowlist.read_text()
    snapshot_path, manifest_path, output = (
        tmp_path / "snapshot.json",
        tmp_path / "manifest.json",
        tmp_path / "report.json",
    )
    snapshot_path.write_text(snapshot.model_dump_json())
    manifest_path.write_text(manifest.model_dump_json(by_alias=True))
    conditions, events = tmp_path / "conditions.json", tmp_path / "events.jsonl"
    conditions.write_text(runs[0].conditions.model_dump_json(by_alias=True))
    events.write_text("\n".join(event.model_dump_json(by_alias=True) for event in runs[0].events))
    health = runs[0].recording_health
    assert health is not None
    (tmp_path / "evidence-health.json").write_text(health.model_dump_json(by_alias=True))
    assert (
        membership.main(
            [
                "analyze",
                "--snapshot",
                str(snapshot_path),
                "--manifest",
                str(manifest_path),
                "--manual-allowlist",
                str(allowlist),
                "--conditions",
                str(conditions),
                "--events",
                str(events),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    report = MembershipReport.model_validate_json(output.read_text())
    assert report.expected_pairs == 300
    assert report.observed_manual_participants == 1
    assert set(report.input_sha256) == {
        "snapshot",
        "manifest",
        "manual_allowlist",
        "conditions_0",
        "events_0",
        "health_0",
    }
    engine.assert_not_called()


@pytest.mark.parametrize(
    "reason", ["run_failed", "run_deadline", "signal", "participant_unavailable"]
)
def test_all_pairs_do_not_mask_abnormal_worker_shutdown(reason: str) -> None:
    snapshot, runs, manifest = controlled_fixture(50)
    ended = next(event for event in runs[0].events if event.event == "participant_run_finished")
    ended.reason = reason
    report = membership.build_report(snapshot, runs, manifest)
    assert all(comparison.distinct_pairs == 300 for comparison in report.comparisons.values())
    assert report.incomplete_workers == ["worker-1"]
    assert report.decision == "FAIL"


@pytest.mark.parametrize("reason", [None, "run_failed"])
def test_cleanup_failure_after_measurement_end_cannot_pass(reason: str | None) -> None:
    snapshot, runs, manifest = controlled_fixture(50)
    shutdown = next(
        event for event in runs[0].events if event.event == "participant_shutdown_finished"
    )
    shutdown.reason = reason
    report = membership.build_report(snapshot, runs, manifest)
    assert all(comparison.distinct_pairs == 300 for comparison in report.comparisons.values())
    assert report.incomplete_workers == ["worker-1"]
    assert report.decision == "FAIL"
