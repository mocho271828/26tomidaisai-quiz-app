"""匿名snapshotと予定workerを突き合わせ、停止・不足を固定母数へ残す。"""

from collections import Counter
from typing import Literal

from quiz_app.observability.browser_membership_models import (
    BrowserEvent,
    BrowserManifest,
    BrowserRun,
    DuplicatePair,
    ManualParticipantAllowlist,
    MembershipReport,
    MembershipSnapshot,
    Pair,
    PairComparison,
)

PairKey = tuple[int, int]
PairKind = Literal["stored", "accepted", "results", "clicks", "sending"]

MEASUREMENT_MARKERS = (
    "participant_run_started",
    "participant_measurement_started",
    "participant_run_finished",
)


def measurement_events(run: BrowserRun, *, allow_incomplete: bool = False) -> list[BrowserEvent]:
    """途中停止は未封印の区間として残し、旧形式・重複markerは拒否する。"""
    markers: list[BrowserEvent | None] = []
    for name in MEASUREMENT_MARKERS:
        found = [event for event in run.events if event.event == name]
        if not found and allow_incomplete:
            markers.append(None)
            continue
        if len(found) != 1 or found[0].lifecycle_version != 2:
            raise ValueError("membership_measurement_markers_invalid")
        markers.append(found[0])
    start, measurement, end = markers
    if start is None or measurement is None:
        return []
    if start.monotonic_ms > measurement.monotonic_ms or (
        end is not None and measurement.monotonic_ms > end.monotonic_ms
    ):
        raise ValueError("membership_measurement_order_invalid")
    if start.cohort is None or (
        start.cohort.count != run.conditions.count
        or start.cohort.ordinal_start != run.conditions.ordinal_start
    ):
        raise ValueError("membership_cohort_mismatch")
    return [
        event
        for event in run.events
        if measurement.monotonic_ms <= event.monotonic_ms
        and (end is None or event.monotonic_ms <= end.monotonic_ms)
    ]


def pairs(values: set[PairKey]) -> list[Pair]:
    return [Pair(serial=serial, round=round_number) for serial, round_number in sorted(values)]


def compare(values: list[PairKey], expected: set[PairKey]) -> PairComparison:
    counts = Counter(values)
    return PairComparison(
        observed_rows=len(values),
        distinct_pairs=len(counts),
        missing=pairs(expected - counts.keys()),
        extra=pairs(counts.keys() - expected),
        duplicates=[
            DuplicatePair(serial=serial, round=round_number, occurrences=count)
            for (serial, round_number), count in sorted(counts.items())
            if count > 1
        ],
    )


def completed_lifecycle(run: BrowserRun) -> bool:
    """全問の集合一致だけで異常終了・回収未完了を成功にしない。"""
    ended = [event for event in run.events if event.event == "participant_run_finished"]
    shutdown = [event for event in run.events if event.event == "participant_shutdown_finished"]
    return (
        all(
            sum(event.event == marker for event in run.events) == 1
            for marker in MEASUREMENT_MARKERS
        )
        and len(ended) == len(shutdown) == 1
        and ended[0].reason == "completed"
        and shutdown[0].lifecycle_version == 2
        and shutdown[0].reason == "completed"
        and shutdown[0].monotonic_ms >= ended[0].monotonic_ms
    )


def build_report(
    snapshot: MembershipSnapshot,
    runs: list[BrowserRun],
    manifest: BrowserManifest | None = None,
    manual_allowlist: ManualParticipantAllowlist | None = None,
) -> MembershipReport:
    """予定人数×6問を固定し、対応不明・重複・停止workerの不足を残す。"""
    if manifest is not None and (manifest.run_id, manifest.source_commit) != (
        snapshot.run_id,
        snapshot.source_commit,
    ):
        raise ValueError("membership_manifest_binding_mismatch")
    if manual_allowlist is not None and (
        manifest is None
        or manual_allowlist.run_id != snapshot.run_id
        or manual_allowlist.identity_key_fingerprint != snapshot.identity_key_fingerprint
    ):
        raise ValueError("membership_manual_allowlist_binding_mismatch")
    expected_serials = (
        {
            serial
            for worker in manifest.workers
            for serial in range(worker.ordinal_start, worker.ordinal_start + worker.count)
        }
        if manifest is not None
        else set()
    )
    planned_workers = {worker.worker_id: worker for worker in manifest.workers} if manifest else {}
    observed_serials: set[int] = set()
    received_workers: set[str] = set()
    incomplete_workers: list[str] = []
    recording_incomplete_workers: list[str] = []
    observed: list[BrowserEvent] = []
    excluded = 0
    outside_cohort = 0
    for run in runs:
        config = run.conditions
        if (config.run_id, config.source_commit) != (snapshot.run_id, snapshot.source_commit):
            raise ValueError("membership_run_binding_mismatch")
        if manifest is not None:
            worker = planned_workers.get(config.worker_id or "")
            if worker is None or (worker.count, worker.ordinal_start) != (
                config.count,
                config.ordinal_start,
            ):
                raise ValueError("membership_worker_binding_mismatch")
            received_workers.add(worker.worker_id)
            if (
                run.recording_health is None
                or not run.recording_health.is_complete()
                or run.recording_health.written_records != len(run.events)
                or any(event.sequence != index for index, event in enumerate(run.events))
            ):
                recording_incomplete_workers.append(worker.worker_id)
            if not completed_lifecycle(run):
                incomplete_workers.append(worker.worker_id)
        serials = set(range(config.ordinal_start, config.ordinal_start + config.count))
        if max(serials) > 17576 or observed_serials & serials:
            raise ValueError("membership_cohort_overlap")
        observed_serials.update(serials)
        if manifest is None:
            expected_serials.update(serials)
        measured = measurement_events(run, allow_incomplete=manifest is not None)
        outside_cohort += sum(
            event.serial is not None and event.serial not in serials for event in measured
        )
        observed.extend(measured)
        excluded += len(run.events) - len(measured)
    if manifest is None and len(expected_serials) != 100:
        raise ValueError("membership_requires_fixed_100_participants")
    missing_workers = sorted(planned_workers.keys() - received_workers)
    if snapshot.metadata.participant_count != len(snapshot.participants) or (
        snapshot.metadata.answer_count != len(snapshot.answers)
    ):
        raise ValueError("membership_snapshot_cardinality_invalid")
    if any(answer.round_number > snapshot.metadata.round_count for answer in snapshot.answers):
        raise ValueError("membership_snapshot_answer_without_round")
    identities: dict[int, list[str]] = {}
    rejected = 0
    outside_identities = 0
    for event in observed:
        if event.event != "participant_identity":
            continue
        if event.serial not in expected_serials:
            outside_identities += 1
        elif event.participant_key is None or (
            event.identity_key_fingerprint != snapshot.identity_key_fingerprint
        ):
            rejected += 1
        else:
            assert event.serial is not None
            identities.setdefault(event.serial, []).append(event.participant_key)
    database_keys = set(snapshot.participants)
    manual_keys = set(manual_allowlist.participant_keys) if manual_allowlist is not None else set()
    expected_manual = manifest.manual_participant_count if manifest is not None else 0
    browser_keys = {value for values in identities.values() for value in values}
    manual_conflicts = len(manual_keys & browser_keys)
    if any(answer.participant_key not in database_keys for answer in snapshot.answers):
        raise ValueError("membership_snapshot_answer_without_participant")
    identity_counts = Counter(value for values in identities.values() for value in set(values))
    mapping = {
        values[0]: serial
        for serial, values in identities.items()
        if len(values) == 1 and identity_counts[values[0]] == 1 and values[0] in database_keys
    }
    duplicate_identities = sum(len(values) - 1 for values in identities.values())
    shared_keys = sum(count > 1 for count in identity_counts.values())
    groups: dict[PairKind, list[PairKey]] = {
        "stored": [],
        "accepted": [],
        "results": [],
        "clicks": [],
        "sending": [],
    }
    unmapped_answers = 0
    manual_answers = 0
    for answer in snapshot.answers:
        serial = mapping.get(answer.participant_key)
        if serial is None and answer.participant_key in manual_keys:
            manual_answers += 1
        elif serial is None:
            unmapped_answers += 1
        else:
            groups["stored"].append((serial, answer.round_number))
    event_kinds: dict[str, PairKind] = {
        "accepted_dom": "accepted",
        "result_dom": "results",
        "manual_answer_clicked" if manifest is not None else "click_planned": "clicks",
    }
    for event in observed:
        if manifest is not None and event.event == "result_dom" and event.reason == "unanswered":
            continue
        kind = event_kinds.get(event.event)
        number = event.round
        if (
            event.telemetry is not None
            and event.telemetry.kind == "answer"
            and (event.telemetry.phase == "sending")
        ):
            kind, number = "sending", event.telemetry.round_number
        if kind is None:
            continue
        if event.serial is None or number is None or number not in range(1, 7):
            raise ValueError("membership_browser_pair_invalid")
        groups[kind].append((event.serial, number))
    expected = {(serial, number) for serial in expected_serials for number in range(1, 7)}
    comparisons = {kind: compare(values, expected) for kind, values in groups.items()}
    unmapped_browser = sorted(expected_serials - set(mapping.values()))
    unmapped_database = len(database_keys - mapping.keys() - manual_keys)
    duplicate_database = len(snapshot.participants) - len(set(snapshot.participants))
    verified = not any(
        (
            unmapped_browser,
            unmapped_database,
            unmapped_answers,
            duplicate_database,
            duplicate_identities,
            shared_keys,
            outside_identities,
            outside_cohort,
            rejected,
            missing_workers,
            incomplete_workers,
            recording_incomplete_workers,
            manual_conflicts,
            len(manual_keys) != expected_manual,
            manual_keys - database_keys,
        )
    )
    verified = verified and all(
        not (value.missing or value.extra or value.duplicates) for value in comparisons.values()
    )
    stored, accepted, results = (set(groups[kind]) for kind in ("stored", "accepted", "results"))
    return MembershipReport(
        run_id=snapshot.run_id,
        source_commit=snapshot.source_commit,
        identity_key_fingerprint=snapshot.identity_key_fingerprint,
        decision="PASS" if verified else "FAIL",
        expected_participants=len(expected_serials),
        expected_pairs=len(expected),
        expected_manual_participants=expected_manual,
        allowlisted_manual_participants=len(manual_keys),
        observed_manual_participants=len(manual_keys & database_keys),
        observed_manual_answers=manual_answers,
        missing_manual_participants=max(expected_manual - len(manual_keys & database_keys), 0),
        manual_identity_conflicts=manual_conflicts,
        missing_workers=missing_workers,
        incomplete_workers=sorted(incomplete_workers),
        recording_incomplete_workers=sorted(recording_incomplete_workers),
        observed_database_rounds=snapshot.metadata.round_count,
        mapped_participants=len(mapping),
        unmapped_browser_participants=unmapped_browser,
        unmapped_database_participants=unmapped_database,
        unmapped_database_answers=unmapped_answers,
        duplicate_database_participants=duplicate_database,
        duplicate_identity_observations=duplicate_identities,
        shared_identity_keys=shared_keys,
        outside_cohort_identity_observations=outside_identities,
        outside_cohort_observations=outside_cohort,
        rejected_identity_observations=rejected,
        excluded_outside_measurement_events=excluded,
        comparisons=comparisons,
        stored_without_accepted=pairs(stored - accepted),
        accepted_without_stored=pairs(accepted - stored),
        stored_without_results=pairs(stored - results),
        results_without_stored=pairs(results - stored),
        all_expected_membership_verified=verified,
    )
