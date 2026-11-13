from datetime import datetime, timedelta

from quiz_app.audience.contracts import TimeResponse
from quiz_app.domain.rounds import STANDARD_DURATION_SECONDS
from quiz_app.load_test.controller_contracts import ControllerRefusal
from quiz_app.load_test.legacy_control_contracts import (
    OperatorStateResponse,
)
from quiz_app.load_test.manifest import LoadTestManifest
from quiz_app.models.enums import RoundResultPublication, RoundState

WAITING_RESULT_STATES = frozenset(
    {
        RoundState.SCHEDULED,
        RoundState.OPEN,
        RoundState.READY_TO_REVEAL,
        RoundState.AWAITING_VALIDATION,
    }
)


def results_deadline(manifest: LoadTestManifest, round_number: int) -> datetime:
    """次slot開始以降の結果を当該Roundの正式証拠へ混入させない。"""

    end_offset = (
        manifest.scenario.rounds[round_number].start_offset_seconds
        if round_number < 10
        else manifest.scenario.lifecycle.measurement_end_seconds
    )
    return manifest.scheduled_start_at + timedelta(seconds=end_offset)


def results_observation_in_window(
    manifest: LoadTestManifest, round_number: int, observed_at: datetime
) -> bool:
    slot = manifest.scenario.rounds[round_number - 1]
    close_at = manifest.scheduled_start_at + timedelta(
        seconds=slot.start_offset_seconds + STANDARD_DURATION_SECONDS
    )
    return close_at <= observed_at < results_deadline(manifest, round_number)


def validate_results_state(
    manifest: LoadTestManifest,
    round_number: int,
    server_time: TimeResponse,
    state: OperatorStateResponse,
) -> bool:
    """未公開は待機し、公開済みの固定集計または期限に違反すれば停止する。"""

    current = state.current_round
    if current is None:
        raise ControllerRefusal("controller_results_state_drift")
    if current.state is RoundState.VOID_CANDIDATE or current.result_failure_code is not None:
        raise ControllerRefusal("controller_automatic_results_failed")
    if max(server_time.server_time, state.projected_at) >= results_deadline(manifest, round_number):
        raise ControllerRefusal("controller_results_deadline_missed")
    if (
        current.state in WAITING_RESULT_STATES
        and current.result_publication is RoundResultPublication.WITHHELD
    ):
        if state.latest_ranking_snapshot_version != (round_number - 1 or None):
            raise ControllerRefusal("controller_results_ranking_mismatch")
        return False
    counts = {stat.option_id: stat.answer_count for stat in current.statistics.options}
    expected = manifest.scenario.rounds[round_number - 1].expected_option_counts
    expected_by_id = {
        option.id: count
        for option, count in zip(
            manifest.questions[round_number - 1].options,
            (expected.a, expected.b, expected.c, expected.d),
            strict=True,
        )
    }
    if (
        current.state is not RoundState.REVEAL
        or current.result_publication is not RoundResultPublication.PUBLISHED
        or not results_observation_in_window(manifest, round_number, state.projected_at)
        or current.statistics.answer_count != manifest.scenario.thresholds.expected_answer_count
        or counts != expected_by_id
    ):
        raise ControllerRefusal("controller_results_gate_closed")
    if state.latest_ranking_snapshot_version != round_number:
        raise ControllerRefusal("controller_results_ranking_mismatch")
    return True
