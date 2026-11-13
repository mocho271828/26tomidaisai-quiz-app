import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.sql.elements import TextClause

from quiz_app.observability import browser_membership as membership
from quiz_app.observability.browser_membership_models import (
    AnswerObservation,
    BrowserConditions,
    BrowserEvent,
    BrowserRun,
    Cohort,
    MembershipSnapshot,
    SnapshotMetadata,
    StoredAnswer,
)

KEY = bytes([7]) * 32
RUN_ID = "run-a"
COMMIT = "a" * 40
FINGERPRINT = "e33ec955466fb3015db2e654f649bdab0b8b64ba24bb38d8b6008aeb39945761"


def metadata(participants: int = 100, answers: int = 600) -> SnapshotMetadata:
    return SnapshotMetadata(
        transaction_read_only=True,
        isolation_level="repeatable read",
        event_count=1,
        round_count=6,
        void_history=0,
        unmatched_answers=0,
        participant_count=participants,
        answer_count=answers,
        captured_at=datetime.now(UTC),
    )


def label(serial: int) -> str:
    return f"A{chr(65 + (serial - 1) // 26)}{chr(65 + (serial - 1) % 26)}"


def fixture(count: int = 100, ordinal_start: int = 1) -> tuple[MembershipSnapshot, BrowserRun]:
    keys = [
        membership.participant_identity(KEY, RUN_ID, label(serial))
        for serial in range(ordinal_start, ordinal_start + count)
    ]
    events = [
        BrowserEvent(
            event="participant_run_started",
            monotonicMs=0,
            lifecycleVersion=2,
            cohort=Cohort(count=count, ordinalStart=ordinal_start),
        ),
        BrowserEvent(event="participant_measurement_started", monotonicMs=10, lifecycleVersion=2),
    ]
    answers: list[StoredAnswer] = []
    for serial, key in enumerate(keys, ordinal_start):
        events.append(
            BrowserEvent(
                event="participant_identity",
                monotonicMs=20,
                serial=serial,
                participantKey=key,
                identityKeyFingerprint=FINGERPRINT,
            )
        )
        for number in range(1, 7):
            answers.append(StoredAnswer(participant_key=key, round_number=number))
            events.extend(
                BrowserEvent(event=name, monotonicMs=30 + number, serial=serial, round=number)
                for name in ("click_planned", "accepted_dom", "result_dom")
            )
            events.append(
                BrowserEvent(
                    event="ui_observation",
                    monotonicMs=30 + number,
                    serial=serial,
                    telemetry=AnswerObservation(
                        kind="answer", phase="sending", round_number=number
                    ),
                )
            )
    events.append(
        BrowserEvent(event="participant_run_finished", monotonicMs=100, lifecycleVersion=2)
    )
    snapshot = MembershipSnapshot(
        run_id=RUN_ID,
        source_commit=COMMIT,
        identity_key_fingerprint=FINGERPRINT,
        metadata=metadata(count, count * 6),
        participants=keys,
        answers=answers,
    )
    run = BrowserRun(
        conditions=BrowserConditions(
            runId=RUN_ID, sourceCommit=COMMIT, count=count, ordinalStart=ordinal_start
        ),
        events=events,
    )
    return snapshot, run


def test_identity_vector_matches_typescript_and_changes_with_each_binding() -> None:
    assert membership.identity_fingerprint(KEY, RUN_ID) == FINGERPRINT
    assert membership.participant_identity(KEY, RUN_ID, "ABC") == (
        "c87dae6928ae911a360cc7297186a80900b367fc9e148e2c81edd5613a328d1f"
    )
    assert membership.participant_identity(KEY, RUN_ID, "ABC") != membership.participant_identity(
        KEY, "run-b", "ABC"
    )
    with pytest.raises(ValueError, match="membership_identity_invalid"):
        membership.participant_identity(KEY, RUN_ID, "ABC\n")


def test_all_600_individual_pairs_match_without_disclosing_labels() -> None:
    snapshot, run = fixture()
    report = membership.build_report(snapshot, [run])
    assert report.decision == "PASS"
    assert report.all_expected_membership_verified
    assert report.expected_pairs == 600
    assert report.mapped_participants == 100
    assert all(value.distinct_pairs == 600 for value in report.comparisons.values())
    assert "AAA" not in snapshot.model_dump_json()
    assert "AAA" not in report.model_dump_json()


def test_equal_counts_do_not_mask_different_individual_answers() -> None:
    snapshot, run = fixture()
    snapshot.answers[0] = snapshot.answers[1].model_copy(deep=True)
    report = membership.build_report(snapshot, [run])
    assert report.decision == "FAIL"
    assert report.comparisons["stored"].observed_rows == 600
    assert [(pair.serial, pair.round) for pair in report.comparisons["stored"].missing] == [(1, 1)]
    assert report.comparisons["stored"].duplicates[0].occurrences == 2
    assert [(pair.serial, pair.round) for pair in report.accepted_without_stored] == [(1, 1)]


def test_unconnected_participant_stays_in_600_pair_denominator() -> None:
    snapshot, run = fixture()
    run.events = [event for event in run.events if event.serial != 100]
    report = membership.build_report(snapshot, [run])
    assert report.decision == "FAIL"
    assert report.expected_pairs == 600
    assert report.unmapped_browser_participants == [100]
    assert report.unmapped_database_answers == 6
    assert len(report.comparisons["stored"].missing) == 6
    assert len(report.comparisons["clicks"].missing) == 6


def test_duplicate_identity_and_shared_identity_are_never_guessed() -> None:
    snapshot, run = fixture()
    identity = next(event for event in run.events if event.event == "participant_identity")
    run.events.append(identity.model_copy(deep=True))
    second = next(
        event for event in run.events if event.event == "participant_identity" and event.serial == 2
    )
    second.participant_key = identity.participant_key
    report = membership.build_report(snapshot, [run])
    assert report.decision == "FAIL"
    assert report.duplicate_identity_observations == 1
    assert report.shared_identity_keys == 1
    assert report.unmapped_browser_participants == [1, 2]


def test_wrong_key_and_outside_cohort_identity_are_reported_as_failures() -> None:
    snapshot, run = fixture()
    identity = next(event for event in run.events if event.event == "participant_identity")
    identity.identity_key_fingerprint = "b" * 64
    run.events.append(identity.model_copy(update={"serial": 101}))
    report = membership.build_report(snapshot, [run])
    assert report.decision == "FAIL"
    assert report.rejected_identity_observations == 1
    assert report.outside_cohort_identity_observations == 1
    assert report.unmapped_browser_participants == [1]


def test_shutdown_events_do_not_create_attempts_or_duplicate_receipts() -> None:
    snapshot, run = fixture()
    run.events.extend(
        [
            BrowserEvent(event="accepted_dom", monotonicMs=101, serial=1, round=1),
            BrowserEvent(event="click_planned", monotonicMs=101, serial=101, round=1),
        ]
    )
    report = membership.build_report(snapshot, [run])
    assert report.decision == "PASS"
    assert report.comparisons["accepted"].observed_rows == 600
    assert report.excluded_outside_measurement_events == 3


def test_zero_rows_do_not_pass_even_if_empty_sets_are_equal() -> None:
    snapshot, run = fixture()
    snapshot.participants = []
    snapshot.answers = []
    snapshot.metadata = metadata(0, 0)
    run.events = [event for event in run.events if event.serial is None]
    report = membership.build_report(snapshot, [run])
    assert report.decision == "FAIL"
    assert len(report.unmapped_browser_participants) == 100
    assert all(len(value.missing) == 600 for value in report.comparisons.values())


@pytest.mark.parametrize(
    "marker",
    ["participant_run_started", "participant_measurement_started", "participant_run_finished"],
)
def test_missing_lifecycle_marker_refuses_verification(marker: str) -> None:
    snapshot, run = fixture()
    run.events = [event for event in run.events if event.event != marker]
    with pytest.raises(ValueError, match="membership_measurement_markers_invalid"):
        membership.build_report(snapshot, [run])


def test_declared_cohort_cannot_shrink_the_expected_denominator() -> None:
    snapshot, run = fixture()
    run.conditions.count = 99
    run.events[0].cohort = Cohort(count=99, ordinalStart=1)
    with pytest.raises(ValueError, match="membership_requires_fixed_100_participants"):
        membership.build_report(snapshot, [run])


def test_disjoint_generator_cohorts_cover_exactly_100_participants() -> None:
    snapshot, run = fixture()
    runs: list[BrowserRun] = []
    for first, count in ((1, 40), (41, 60)):
        part = run.model_copy(deep=True)
        part.conditions.count = count
        part.conditions.ordinal_start = first
        part.events = [
            event
            for event in part.events
            if event.serial is None or first <= event.serial < first + count
        ]
        part.events[0].cohort = Cohort(count=count, ordinalStart=first)
        runs.append(part)
    assert membership.build_report(snapshot, runs).decision == "PASS"
    # 他cohortのidentityを誤った生成機fileへ移しても、集合一致で免責しない。
    moved = next(event for event in runs[1].events if event.event == "participant_identity")
    runs[1].events.remove(moved)
    runs[0].events.append(moved)
    report = membership.build_report(snapshot, runs)
    assert report.outside_cohort_observations == 1
    assert report.decision == "FAIL"


def test_overlapping_generator_cohorts_are_rejected() -> None:
    snapshot, run = fixture()
    with pytest.raises(ValueError, match="membership_cohort_overlap"):
        membership.build_report(snapshot, [run, run])


def test_run_binding_mismatch_and_answers_without_participant_are_refused() -> None:
    snapshot, run = fixture()
    snapshot.participants[0] = "c" * 64
    with pytest.raises(ValueError, match="membership_snapshot_answer_without_participant"):
        membership.build_report(snapshot, [run])
    run.conditions.run_id = "another-run"
    with pytest.raises(ValueError, match="membership_run_binding_mismatch"):
        membership.build_report(snapshot, [run])


@pytest.mark.parametrize(
    ("field", "value"),
    [("event_count", 2), ("round_count", 7), ("void_history", 1), ("transaction_read_only", False)],
)
def test_ambiguous_database_history_is_rejected(field: str, value: int | bool) -> None:
    data = metadata().model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        SnapshotMetadata.model_validate(data)


@pytest.mark.parametrize("round_count", [0, 1, 3, 5])
def test_partial_run_keeps_all_600_expected_pairs(round_count: int) -> None:
    snapshot, run = fixture()
    snapshot.answers = [answer for answer in snapshot.answers if answer.round_number <= round_count]
    snapshot.metadata.round_count = round_count
    snapshot.metadata.answer_count = len(snapshot.answers)
    run.events = [
        event
        for event in run.events
        if (event.round is None or event.round <= round_count)
        and (
            event.telemetry is None
            or event.telemetry.round_number is None
            or event.telemetry.round_number <= round_count
        )
    ]

    report = membership.build_report(snapshot, [run])

    assert report.decision == "FAIL"
    assert not report.all_expected_membership_verified
    assert report.expected_rounds == 6
    assert report.expected_pairs == 600
    assert report.observed_database_rounds == round_count
    for comparison in report.comparisons.values():
        assert comparison.observed_rows == round_count * 100
        assert len(comparison.missing) == (6 - round_count) * 100
    assert "途中中止を全6問実施として扱わない" in report.scope


def test_answer_for_uncreated_database_round_is_rejected() -> None:
    snapshot, run = fixture()
    snapshot.metadata.round_count = 5
    with pytest.raises(ValueError, match="membership_snapshot_answer_without_round"):
        membership.build_report(snapshot, [run])


def test_database_probe_uses_one_read_only_repeatable_transaction() -> None:
    engine = MagicMock(spec=Engine)
    connection = engine.connect.return_value.execution_options.return_value.__enter__.return_value
    calls: list[str] = []

    def execute(statement: TextClause) -> MagicMock:
        sql = str(statement)
        calls.append(sql)
        result = MagicMock()
        if sql == membership.METADATA_SQL:
            result.mappings.return_value.one.return_value = metadata(1, 1).model_dump()
        elif sql == membership.PARTICIPANTS_SQL:
            result.mappings.return_value.__iter__.return_value = iter([{"public_label": "ABC"}])
        elif sql == membership.ANSWERS_SQL:
            result.mappings.return_value.__iter__.return_value = iter(
                [{"public_label": "ABC", "round_number": 1}]
            )
        return result

    connection.execute.side_effect = execute
    snapshot = membership.read_snapshot(engine, KEY, RUN_ID, COMMIT)
    engine.connect.return_value.execution_options.assert_called_once_with(
        isolation_level="REPEATABLE READ"
    )
    connection.begin.assert_called_once()
    assert calls[0] == "SET TRANSACTION READ ONLY"
    assert "statement_timeout" in calls[1]
    assert calls.index(membership.METADATA_SQL) < calls.index(membership.PARTICIPANTS_SQL)
    assert snapshot.participants == [membership.participant_identity(KEY, RUN_ID, "ABC")]
    assert "ABC" not in snapshot.model_dump_json()


def test_key_file_requires_exact_private_regular_owned_file(tmp_path: Path) -> None:
    path = tmp_path / "identity.key"
    path.write_text(KEY.hex() + "\n")
    path.chmod(0o600)
    assert membership.read_identity_key(path) == KEY
    path.chmod(0o400)
    with pytest.raises(ValueError, match="membership_identity_key_invalid"):
        membership.read_identity_key(path)
    path.chmod(0o600)
    alias = tmp_path / "alias.key"
    alias.symlink_to(path)
    with pytest.raises(OSError):
        membership.read_identity_key(alias)
    alias.unlink()
    os.link(path, alias)
    with pytest.raises(ValueError, match="membership_identity_key_invalid"):
        membership.read_identity_key(path)


def test_output_never_overwrites_prior_evidence(tmp_path: Path) -> None:
    snapshot, _run = fixture()
    path = tmp_path / "snapshot.json"
    path.write_text("old evidence")
    with pytest.raises(FileExistsError):
        membership.write_output(path, snapshot)
    assert path.read_text() == "old evidence"


def test_cli_analysis_seals_input_hashes_without_database_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, run = fixture()
    snapshot_path = tmp_path / "snapshot.json"
    condition_path = tmp_path / "conditions.json"
    events_path = tmp_path / "events.jsonl"
    output = tmp_path / "report.json"
    snapshot_path.write_text(snapshot.model_dump_json())
    condition_path.write_text(run.conditions.model_dump_json(by_alias=True))
    events_path.write_text("\n".join(event.model_dump_json(by_alias=True) for event in run.events))
    engine = MagicMock(side_effect=AssertionError("DB must not be used by offline analysis"))
    monkeypatch.setattr(membership, "get_engine", engine)
    monkeypatch.setattr(membership.logfire, "configure", MagicMock())
    monkeypatch.setattr(membership.logfire, "info", MagicMock())
    assert (
        membership.main(
            [
                "analyze",
                "--snapshot",
                str(snapshot_path),
                "--conditions",
                str(condition_path),
                "--events",
                str(events_path),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    from quiz_app.observability.browser_membership_models import MembershipReport

    report = MembershipReport.model_validate_json(output.read_text())
    assert report.decision == "PASS"
    assert set(report.input_sha256) == {"snapshot", "conditions_0", "events_0"}
    engine.assert_not_called()


def test_cli_failure_does_not_log_validation_input_or_exception_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text('{"private": "must-not-be-disclosed"}')
    output = tmp_path / "report.json"
    error_log = MagicMock()
    monkeypatch.setattr(membership.logfire, "configure", MagicMock())
    monkeypatch.setattr(membership.logfire, "info", MagicMock())
    monkeypatch.setattr(membership.logfire, "error", error_log)
    assert (
        membership.main(
            [
                "analyze",
                "--snapshot",
                str(snapshot),
                "--conditions",
                "unused",
                "--events",
                "unused",
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert "must-not-be-disclosed" not in capsys.readouterr().err
    assert "must-not-be-disclosed" not in str(error_log.call_args)
    assert not output.exists()
