from datetime import datetime, timedelta

import pytest
from test_load_test_controller import _manifest, _state

from quiz_app.audience.contracts import TimeResponse
from quiz_app.load_test.controller_contracts import ControllerRefusal
from quiz_app.load_test.controller_results import (
    results_deadline,
    results_observation_in_window,
    validate_results_state,
)
from quiz_app.load_test.legacy_control_contracts import (
    OperatorStateResponse,
)
from quiz_app.load_test.manifest import LoadTestManifest
from quiz_app.models.enums import RoundResultPublication, RoundState
from quiz_app.operator.control_contracts import (
    OperatorOptionStatisticsResponse,
    OperatorRoundStatisticsResponse,
)


def _results_state(
    manifest: LoadTestManifest,
    *,
    round_number: int = 1,
    phase: RoundState = RoundState.REVEAL,
    publication: RoundResultPublication = RoundResultPublication.PUBLISHED,
    projected_at: datetime | None = None,
    counts: tuple[int, int, int, int] | None = None,
) -> OperatorStateResponse:
    slot = manifest.scenario.rounds[round_number - 1]
    open_at = manifest.scheduled_start_at + timedelta(seconds=slot.start_offset_seconds)
    observed_at = projected_at or open_at + timedelta(seconds=10)
    state = _state(manifest, observed_at, round_number=round_number)
    state.projected_at = observed_at
    current = state.current_round
    assert current is not None
    current.state = phase
    current.result_publication = publication
    current.result_failure_code = (
        "automatic_aggregate_integrity_failure" if phase is RoundState.VOID_CANDIDATE else None
    )
    current.open_at = open_at
    current.standard_close_at = open_at + timedelta(seconds=10)
    expected = slot.expected_option_counts
    answer_counts = counts or (expected.a, expected.b, expected.c, expected.d)
    current.statistics = OperatorRoundStatisticsResponse(
        answer_count=sum(answer_counts),
        average_response_ms=100.0,
        options=[
            OperatorOptionStatisticsResponse(
                option_id=option.id,
                answer_count=count,
                average_response_ms=100.0 if count else None,
            )
            for option, count in zip(
                manifest.questions[round_number - 1].options, answer_counts, strict=True
            )
        ],
    )
    state.latest_ranking_snapshot_version = (
        round_number
        if publication is RoundResultPublication.PUBLISHED
        else round_number - 1 or None
    )
    return OperatorStateResponse.model_validate(state.model_dump())


@pytest.mark.parametrize("round_number", [1, 2, 10])
@pytest.mark.parametrize(
    "phase",
    [
        RoundState.SCHEDULED,
        RoundState.OPEN,
        RoundState.READY_TO_REVEAL,
        RoundState.AWAITING_VALIDATION,
    ],
)
def test_waits_for_every_unpublished_scheduled_phase(round_number: int, phase: RoundState) -> None:
    manifest = _manifest()
    state = _results_state(
        manifest,
        round_number=round_number,
        phase=phase,
        publication=RoundResultPublication.WITHHELD,
    )

    assert not validate_results_state(
        manifest,
        round_number,
        TimeResponse(shared_sample_age_ms=0, server_time=state.projected_at),
        state,
    )


@pytest.mark.parametrize("round_number", range(1, 11))
def test_accepts_operator_publication_with_exact_scenario_totals(
    round_number: int,
) -> None:
    manifest = _manifest()
    state = _results_state(manifest, round_number=round_number)

    assert validate_results_state(
        manifest,
        round_number,
        TimeResponse(shared_sample_age_ms=0, server_time=state.projected_at),
        state,
    )


@pytest.mark.parametrize("mismatch", ["answer_count", "option_counts"])
def test_rejects_publication_with_incorrect_answer_or_option_counts(
    mismatch: str,
) -> None:
    manifest = _manifest()
    expected = manifest.scenario.rounds[0].expected_option_counts
    counts = (
        expected.a - 1,
        expected.b + (1 if mismatch == "option_counts" else 0),
        expected.c,
        expected.d,
    )
    state = _results_state(manifest, counts=counts)

    with pytest.raises(ControllerRefusal, match="controller_results_gate_closed"):
        validate_results_state(
            manifest, 1, TimeResponse(shared_sample_age_ms=0, server_time=state.projected_at), state
        )


@pytest.mark.parametrize(
    ("phase", "publication"),
    [
        (RoundState.REVEAL, RoundResultPublication.WITHHELD),
        (RoundState.AWAITING_VALIDATION, RoundResultPublication.PUBLISHED),
        (RoundState.PREPARING_ASSETS, RoundResultPublication.WITHHELD),
        (RoundState.READY_TO_SCHEDULE, RoundResultPublication.WITHHELD),
        (RoundState.VOIDED, RoundResultPublication.WITHHELD),
    ],
)
def test_rejects_states_outside_waiting_or_published_results(
    phase: RoundState, publication: RoundResultPublication
) -> None:
    manifest = _manifest()
    state = _results_state(manifest, phase=phase, publication=publication)

    with pytest.raises(ControllerRefusal, match="controller_results_gate_closed"):
        validate_results_state(
            manifest, 1, TimeResponse(shared_sample_age_ms=0, server_time=state.projected_at), state
        )


def test_stops_on_automatic_aggregation_failure() -> None:
    manifest = _manifest()
    state = _results_state(
        manifest,
        phase=RoundState.VOID_CANDIDATE,
        publication=RoundResultPublication.WITHHELD,
    )

    with pytest.raises(ControllerRefusal, match="controller_automatic_results_failed"):
        validate_results_state(
            manifest, 1, TimeResponse(shared_sample_age_ms=0, server_time=state.projected_at), state
        )


def test_rejects_missing_current_round() -> None:
    manifest = _manifest()
    state = _results_state(manifest)
    state.current_round = None

    with pytest.raises(ControllerRefusal, match="controller_results_state_drift"):
        validate_results_state(
            manifest, 1, TimeResponse(shared_sample_age_ms=0, server_time=state.projected_at), state
        )


@pytest.mark.parametrize("is_published", [False, True])
@pytest.mark.parametrize("round_number", [1, 2, 10])
def test_rejects_ranking_snapshot_from_another_round(is_published: bool, round_number: int) -> None:
    manifest = _manifest()
    state = _results_state(
        manifest,
        round_number=round_number,
        phase=RoundState.REVEAL if is_published else RoundState.AWAITING_VALIDATION,
        publication=(
            RoundResultPublication.PUBLISHED if is_published else RoundResultPublication.WITHHELD
        ),
    )
    state.latest_ranking_snapshot_version = round_number + 1

    with pytest.raises(ControllerRefusal, match="controller_results_ranking_mismatch"):
        validate_results_state(
            manifest,
            round_number,
            TimeResponse(shared_sample_age_ms=0, server_time=state.projected_at),
            state,
        )


@pytest.mark.parametrize(("round_number", "offset"), [(1, 1260), (9, 4140), (10, 4500)])
def test_deadline_uses_next_slot_and_final_measurement_end(round_number: int, offset: int) -> None:
    manifest = _manifest()

    assert results_deadline(manifest, round_number) == (
        manifest.scheduled_start_at + timedelta(seconds=offset)
    )


@pytest.mark.parametrize("round_number", [1, 10])
@pytest.mark.parametrize(
    ("boundary", "microseconds", "expected"),
    [
        ("close", -1, False),
        ("close", 0, True),
        ("deadline", -1, True),
        ("deadline", 0, False),
    ],
)
def test_results_window_includes_standard_close_and_excludes_next_slot(
    round_number: int, boundary: str, microseconds: int, expected: bool
) -> None:
    manifest = _manifest()
    boundary_at = (
        manifest.scheduled_start_at
        + timedelta(seconds=manifest.scenario.rounds[round_number - 1].start_offset_seconds + 10)
        if boundary == "close"
        else results_deadline(manifest, round_number)
    )

    assert (
        results_observation_in_window(
            manifest, round_number, boundary_at + timedelta(microseconds=microseconds)
        )
        is expected
    )


def test_projection_crossing_standard_close_is_accepted() -> None:
    manifest = _manifest()
    state = _results_state(manifest)
    time_before_close = TimeResponse(
        shared_sample_age_ms=0, server_time=state.projected_at - timedelta(seconds=1)
    )

    assert validate_results_state(manifest, 1, time_before_close, state)


def test_publication_observed_before_standard_close_is_rejected() -> None:
    manifest = _manifest()
    state = _results_state(manifest)
    state.projected_at -= timedelta(microseconds=1)

    with pytest.raises(ControllerRefusal, match="controller_results_gate_closed"):
        validate_results_state(
            manifest, 1, TimeResponse(shared_sample_age_ms=0, server_time=state.projected_at), state
        )


@pytest.mark.parametrize("round_number", [1, 10])
@pytest.mark.parametrize("is_published", [False, True])
def test_projection_crossing_results_deadline_stops_waiting_or_acceptance(
    round_number: int, is_published: bool
) -> None:
    manifest = _manifest()
    deadline = results_deadline(manifest, round_number)
    state = _results_state(
        manifest,
        round_number=round_number,
        phase=RoundState.REVEAL if is_published else RoundState.AWAITING_VALIDATION,
        publication=(
            RoundResultPublication.PUBLISHED if is_published else RoundResultPublication.WITHHELD
        ),
        projected_at=deadline,
    )
    earlier_time = TimeResponse(shared_sample_age_ms=0, server_time=deadline - timedelta(seconds=1))

    with pytest.raises(ControllerRefusal, match="controller_results_deadline_missed"):
        validate_results_state(manifest, round_number, earlier_time, state)


def test_legacy_formal_contract_rejects_current_p0_state_without_gate() -> None:
    from pydantic import ValidationError

    manifest = _manifest()
    state = _state(manifest, manifest.scheduled_start_at)
    payload = state.model_dump()
    assert isinstance(payload["current_round"], dict)
    del payload["current_round"]["gate"]
    with pytest.raises(ValidationError, match="gate"):
        OperatorStateResponse.model_validate(payload)
