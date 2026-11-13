"""実際の生成metadataと観測境界を使い、初出系列だけの根拠付き集計を検証する。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

from quiz_app.observability.local_metric_series import SeriesKey, parse_metrics
from quiz_app.observability.local_observation_analysis import analyze
from quiz_app.observability.local_observation_models import (
    CollectorFinished,
    MeasurementWindow,
    MetricsObservation,
    ObservationCompletion,
    ObservationManifest,
    ProcessCompletion,
    ServerAnalysis,
)

START = datetime(2026, 10, 2, tzinfo=UTC)
PROCESS_START = START.timestamp() - 100


def identity(process: float = PROCESS_START) -> str:
    return f"# TYPE process_start_time_seconds gauge\nprocess_start_time_seconds {process}\n"


def late_series(count: int, created: float | None, *, label: str = "answers") -> str:
    metadata = ""
    if created is not None:
        timestamp = START.timestamp() + created
        metadata = (
            f'# TYPE work_created gauge\nwork_created{{route="{label}"}} {timestamp}\n'
            f'# TYPE latency_created gauge\nlatency_created{{route="{label}"}} {timestamp}\n'
        )
    return (
        f'# TYPE work_total counter\nwork_total{{route="{label}"}} {count}\n'
        f'# TYPE latency histogram\nlatency_bucket{{route="{label}",le="1"}} {count}\n'
        f'latency_bucket{{route="{label}",le="+Inf"}} {count}\n'
        f'latency_count{{route="{label}"}} {count}\nlatency_sum{{route="{label}"}} {count / 2}\n'
        + metadata
    )


def observation(index: int, exposition: str | None) -> str:
    return MetricsObservation(
        instance="api",
        started_at=START + timedelta(seconds=index),
        monotonic_seconds=index,
        elapsed_seconds=0.03,
        status="ok" if exposition is not None else "unavailable",
        exposition=exposition,
        failure=None if exposition is not None else "timeout",
    ).model_dump_json()


def collection(tmp_path: Path, records: list[str], end: float) -> ServerAnalysis:
    folder = tmp_path / "api"
    folder.mkdir()
    finished = CollectorFinished(
        instance="api", finished_at=START + timedelta(seconds=10), reason="duration"
    )
    (folder / "samples.jsonl").write_text("\n".join([*records, finished.model_dump_json()]) + "\n")
    (tmp_path / "observation.json").write_text(
        ObservationManifest(
            started_at=START, duration_seconds=10, instances=["api"]
        ).model_dump_json()
    )
    (tmp_path / "completion.json").write_text(
        ObservationCompletion(
            finished_at=START + timedelta(seconds=10),
            interrupted=False,
            processes=[ProcessCompletion(instance="api", exit_code=0)],
        ).model_dump_json()
    )
    return analyze(
        tmp_path, MeasurementWindow(start_at=START, end_at=START + timedelta(seconds=end))
    )


@pytest.mark.parametrize("created", [0.0, 0.02, 0.5, 1.02, 1.03])
def test_creation_within_previous_current_scrapes_and_between_them_counts_initial_value(
    tmp_path: Path, created: float
) -> None:
    result = collection(
        tmp_path,
        [
            observation(0, identity()),
            observation(1, identity() + late_series(5, created)),
            observation(2, identity() + late_series(8, created)),
        ],
        2.1,
    )
    assert result.measurement_usable is True
    assert result.counters[0].delta == 8
    assert result.histograms[0].count == 8
    assert result.histograms[0].sum == 4
    assert result.histograms[0].p95 == pytest.approx(0.95)
    process = result.processes[0]
    assert process.created_counter_series == process.created_histogram_series == 1
    assert process.unbased_series == process.series_discontinuities == 0


@pytest.mark.parametrize("created", [None, -1, 0.5, 2.031, 3.5])
def test_unknown_old_and_future_creation_cannot_be_used_as_zero_baseline(
    tmp_path: Path, created: float | None
) -> None:
    result = collection(
        tmp_path,
        [
            observation(0, identity()),
            observation(1, identity()),
            observation(2, identity() + late_series(5, created)),
            observation(3, identity() + late_series(8, created)),
        ],
        3.1,
    )
    assert result.measurement_usable is False
    assert "api:series_discontinuities" in result.measurement_quality_failures
    assert result.counters[0].delta == result.histograms[0].count == 3
    assert result.processes[0].created_counter_series == 0


def test_first_in_window_sample_remains_baseline_even_with_recent_created(tmp_path: Path) -> None:
    result = collection(
        tmp_path,
        [
            observation(1, identity() + late_series(100, 0.5)),
            observation(2, identity() + late_series(105, 0.5)),
        ],
        2.1,
    )
    assert result.measurement_usable is True
    assert result.counters[0].delta == result.histograms[0].count == 5
    assert result.processes[0].created_counter_series == 0
    assert result.processes[0].unbased_series == 2


@pytest.mark.parametrize("gap", ["unavailable", "invalid", "identity"])
def test_missing_or_invalid_observation_breaks_creation_and_delta_continuity(
    tmp_path: Path, gap: str
) -> None:
    separator = {
        "unavailable": observation(1, None),
        "invalid": "not-json",
        "identity": observation(1, late_series(4, 0.5)),
    }[gap]
    result = collection(
        tmp_path,
        [
            observation(0, identity()),
            separator,
            observation(2, identity() + late_series(5, 1.5)),
            observation(3, identity() + late_series(8, 1.5)),
        ],
        3.1,
    )
    assert result.measurement_usable is False
    assert result.processes[0].created_counter_series == 0
    assert result.counters[0].delta == result.histograms[0].count == 3


@pytest.mark.parametrize("second_created", [0.5, 2.5])
def test_disappearing_series_cannot_reappear_as_new_creation(
    tmp_path: Path, second_created: float
) -> None:
    result = collection(
        tmp_path,
        [
            observation(0, identity()),
            observation(1, identity() + late_series(5, 0.5)),
            observation(2, identity()),
            observation(3, identity() + late_series(8, second_created)),
        ],
        3.1,
    )
    assert result.measurement_usable is False
    assert result.processes[0].series_discontinuities == 4
    assert result.processes[0].created_counter_series == 1
    assert result.counters[0].delta == result.histograms[0].count == 5


@pytest.mark.parametrize("second_created", [None, 1.5])
def test_metadata_disappearance_or_change_is_rejected_even_if_values_increase(
    tmp_path: Path, second_created: float | None
) -> None:
    result = collection(
        tmp_path,
        [
            observation(0, identity()),
            observation(1, identity() + late_series(5, 0.5)),
            observation(2, identity() + late_series(8, second_created)),
        ],
        2.1,
    )
    assert result.measurement_usable is False
    assert result.processes[0].created_metadata_changes == 2
    assert "api:created_metadata_changes" in result.measurement_quality_failures
    assert result.counters[0].delta == result.histograms[0].count == 5


def test_restart_does_not_establish_creation_baseline(tmp_path: Path) -> None:
    result = collection(
        tmp_path,
        [
            observation(0, identity()),
            observation(1, identity(PROCESS_START + 1) + late_series(5, 0.5)),
            observation(2, identity(PROCESS_START + 1) + late_series(8, 0.5)),
        ],
        2.1,
    )
    assert result.measurement_usable is False
    assert result.processes[0].restarts == [START + timedelta(seconds=1)]
    assert result.processes[0].created_counter_series == 0
    assert result.counters[0].delta == result.histograms[0].count == 3


def test_creation_metadata_must_match_entire_label_set(tmp_path: Path) -> None:
    text = late_series(5, 0.5).replace('created{route="answers"}', 'created{route="other"}')
    result = collection(
        tmp_path, [observation(0, identity()), observation(1, identity() + text)], 1.1
    )
    assert result.measurement_usable is False
    assert result.processes[0].created_counter_series == 0
    assert result.processes[0].series_discontinuities == 2


def test_official_client_lazy_initialization_and_counter_reset_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = CollectorRegistry()
    counter = Counter("actual_work", "test", ["route"], registry=registry)
    histogram = Histogram("actual_latency", "test", ["route"], registry=registry)
    assert parse_metrics(generate_latest(registry).decode()).counters == {}
    monkeypatch.setattr("prometheus_client.metrics.time.time", lambda: START.timestamp() + 0.5)
    counter.labels(route="answers").inc(5)
    histogram.labels(route="answers").observe(0.2)
    parsed = parse_metrics(generate_latest(registry).decode())
    counter_key = SeriesKey("actual_work_total", (("route", "answers"),))
    histogram_key = SeriesKey("actual_latency", (("route", "answers"),))
    assert parsed.counter_created[counter_key] == parsed.histogram_created[histogram_key]
    assert parsed.counters[counter_key] == 5
    assert parsed.histograms[histogram_key]["count"] == 1
    monkeypatch.setattr("prometheus_client.metrics.time.time", lambda: START.timestamp() + 1.5)
    counter.labels(route="answers").reset()
    reset = parse_metrics(generate_latest(registry).decode())
    assert reset.counters[counter_key] == 0
    assert reset.counter_created[counter_key] != parsed.counter_created[counter_key]
    assert reset.histogram_created[histogram_key] == parsed.histogram_created[histogram_key]
