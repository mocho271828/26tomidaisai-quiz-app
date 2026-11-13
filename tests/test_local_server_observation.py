import asyncio
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from time import monotonic

import httpx
import pytest
from pydantic import ValidationError

from quiz_app.observability import local_observation as collector
from quiz_app.observability.local_observation_analysis import (
    SeriesKey,
    analyze,
    analyze_process,
    combine,
    histogram_total,
    parse_metrics,
)
from quiz_app.observability.local_observation_models import (
    ApiInstance,
    CollectorFinished,
    ContainerSample,
    ContainersObservation,
    MeasurementWindow,
    MetricsObservation,
    ObservationCompletion,
    ObservationManifest,
    ProcessCompletion,
)

START = datetime(2026, 10, 2, tzinfo=UTC)


def test_container_observation_roundtrip_preserves_docker_stats() -> None:
    container = ContainerSample.model_validate_json(
        '{"Name":"local-api-1","CPUPerc":"12.50%","MemUsage":"230MiB / 4GiB",'
        '"MemPerc":"5.62%","PIDs":"9"}'
    )
    observation = ContainersObservation(
        started_at=START,
        monotonic_seconds=10,
        elapsed_seconds=2.1,
        status="ok",
        samples=[container],
    )
    stream = StringIO()
    collector.write_record(stream, observation)
    saved = stream.getvalue()

    assert '"cpu_percent":"12.50%"' in saved
    assert '"CPUPerc"' not in saved
    assert ContainerSample.model_validate_json(container.model_dump_json()) == container
    assert ContainersObservation.model_validate_json(saved) == observation


@pytest.mark.parametrize("invalid", [None, 12.5])
def test_saved_container_sample_still_rejects_invalid_resource_types(
    invalid: float | None,
) -> None:
    with pytest.raises(ValidationError):
        ContainerSample.model_validate(
            {
                "name": "local-api-1",
                "cpu_percent": invalid,
                "memory_usage": "230MiB / 4GiB",
                "memory_percent": "5.62%",
                "pids": "9",
            }
        )


def exposition(count: int, small: int, process: int = 10, bound: str = "1") -> str:
    return f"""# TYPE process_start_time_seconds gauge
process_start_time_seconds {process}
# TYPE work_total counter
work_total {count}
# TYPE latency histogram
latency_bucket{{le="{bound}"}} {small}
latency_bucket{{le="10"}} {count}
latency_bucket{{le="+Inf"}} {count}
latency_count {count}
latency_sum {count * 2}
"""


def sample(index: int, value: str | None, instance: ApiInstance = "api") -> MetricsObservation:
    return MetricsObservation(
        instance=instance,
        started_at=START + timedelta(seconds=index),
        monotonic_seconds=index,
        elapsed_seconds=0.03,
        status="ok" if value is not None else "unavailable",
        exposition=value,
        failure=None if value is not None else "timeout",
    )


def saved_process(tmp_path: Path, instance: ApiInstance, records: list[MetricsObservation]) -> Path:
    folder = tmp_path / instance
    folder.mkdir()
    path = folder / "samples.jsonl"
    finished = CollectorFinished(
        instance=instance, finished_at=START + timedelta(seconds=30), reason="duration"
    )
    path.write_text("\n".join(record.model_dump_json() for record in [*records, finished]) + "\n")
    return path


def test_two_processes_merge_counts_and_buckets_before_percentile(tmp_path: Path) -> None:
    primary = analyze_process(
        saved_process(
            tmp_path,
            "api",
            [
                sample(0, exposition(0, 0)),
                sample(1, exposition(100, 99)),
            ],
        ),
        "api",
    )
    secondary = analyze_process(
        saved_process(
            tmp_path,
            "api-secondary",
            [
                sample(0, exposition(0, 0), "api-secondary"),
                sample(1, exposition(10, 0), "api-secondary"),
            ],
        ),
        "api-secondary",
    )
    counters, histograms, incompatible = combine([primary, secondary])
    assert incompatible == []
    assert counters[0].delta == 110
    histogram = histograms[0]
    assert histogram.count == 110
    assert histogram.sum == 220
    assert histogram.buckets == {"1": 99, "10": 110, "+Inf": 110}
    assert histogram.p95 == pytest.approx(5.5)
    assert histogram.p95 != pytest.approx(
        (primary.histograms[0].p95 + secondary.histograms[0].p95) / 2
    )


def test_missing_scrape_restart_and_counter_reset_are_not_zero_filled(tmp_path: Path) -> None:
    path = saved_process(
        tmp_path,
        "api",
        [
            sample(0, exposition(50, 20)),
            sample(1, None),
            sample(3, exposition(60, 25)),
            sample(4, exposition(200, 100, process=11)),
            sample(5, exposition(202, 101, process=11)),
            sample(6, exposition(1, 0, process=11)),
            sample(7, exposition(4, 1, process=11)),
        ],
    )
    result = analyze_process(path, "api")
    assert result.unavailable == 1
    assert result.restarts == [START + timedelta(seconds=4)]
    assert result.counter_resets == 1
    assert result.histogram_resets == 1
    assert result.counters[0].delta == 5
    assert result.histograms[0].count == 5
    assert result.largest_sample_gap_seconds == 2


def test_missing_process_does_not_look_like_zero_load_or_complete_run(tmp_path: Path) -> None:
    manifest = ObservationManifest(
        started_at=START, duration_seconds=30, instances=["api", "api-secondary"]
    )
    (tmp_path / "observation.json").write_text(manifest.model_dump_json())
    completion = ObservationCompletion(
        finished_at=START,
        interrupted=False,
        processes=[
            ProcessCompletion(instance="api", exit_code=0),
            ProcessCompletion(instance="api-secondary", exit_code=1),
        ],
    )
    (tmp_path / "completion.json").write_text(completion.model_dump_json())
    saved_process(tmp_path, "api", [sample(0, exposition(0, 0)), sample(1, exposition(3, 2))])
    result = analyze(tmp_path)
    assert result.collector_lifecycle_complete is False
    assert result.measurement_usable is False
    assert result.processes[1].samples == 0
    assert result.processes[1].collector_max_seconds is None
    assert result.processes[1].counters == []


def test_incompatible_bucket_schemas_are_not_merged(tmp_path: Path) -> None:
    primary = analyze_process(
        saved_process(
            tmp_path,
            "api",
            [
                sample(0, exposition(0, 0)),
                sample(1, exposition(100, 99)),
            ],
        ),
        "api",
    )
    secondary = analyze_process(
        saved_process(
            tmp_path,
            "api-secondary",
            [
                sample(0, exposition(0, 0, bound="2"), "api-secondary"),
                sample(1, exposition(10, 0, bound="2"), "api-secondary"),
            ],
        ),
        "api-secondary",
    )
    _, histograms, incompatible = combine([primary, secondary])
    assert histograms == []
    assert incompatible == ["latency{}"]


def test_zero_samples_and_unbounded_tail_have_no_invented_percentile() -> None:
    key = SeriesKey("latency", ())
    empty = histogram_total(key, {"count": 0, "sum": 0, "1": 0, "+Inf": 0}, 1)
    assert empty.p95 is None
    tail = histogram_total(key, {"count": 10, "sum": 50, "1": 1, "+Inf": 10}, 1)
    assert tail.p95 is None
    assert tail.p95_lower_bound == 1
    assert tail.p95_upper_bound is None


def test_invalid_record_and_missing_identity_do_not_create_counter_deltas(tmp_path: Path) -> None:
    no_identity = "# TYPE work_total counter\nwork_total 300\n"
    path = saved_process(tmp_path, "api", [sample(0, exposition(0, 0)), sample(1, no_identity)])
    with path.open("a") as stream:
        stream.write("not-json\n")
    result = analyze_process(path, "api")
    assert result.identity_missing_samples == 1
    assert result.invalid_records == 1
    assert result.counters == []
    assert parse_metrics(no_identity).process_started is None


@pytest.mark.asyncio
async def test_metrics_timeout_http_error_and_success_are_distinct() -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("test", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        timeout = await collector.scrape_metrics(client, "api")
    assert timeout.status == "unavailable" and timeout.failure == "timeout"
    assert timeout.exposition is None
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(503))
    ) as client:
        failure = await collector.scrape_metrics(client, "api-secondary")
    assert failure.failure == "http_status"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, text=exposition(1, 1)))
    ) as client:
        success = await collector.scrape_metrics(client, "api-secondary")
    assert success.status == "ok" and success.failure is None
    assert success.exposition == exposition(1, 1)


@pytest.mark.asyncio
async def test_slow_probe_child_is_killed_and_collected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collector, "PROBE_TIMEOUT_SECONDS", 0.03)
    output, failure = await collector.command_output(
        [
            collector.sys.executable,
            "-c",
            "import time; time.sleep(10)",
        ]
    )
    assert output is None
    assert failure == "timeout"


@pytest.mark.asyncio
async def test_parent_eof_stops_resident_collector_and_emits_completion() -> None:
    import sys

    from quiz_app.observability.local_observation_models import observation_adapter

    command = """
import asyncio
from datetime import UTC, datetime
from quiz_app.observability import local_observation as module
from quiz_app.observability.local_observation_models import MetricsObservation
async def mocked_scrape(_client, instance):
    return MetricsObservation(instance=instance, started_at=datetime.now(UTC),
        monotonic_seconds=1, elapsed_seconds=0, status='ok', exposition='')
module.scrape_metrics = mocked_scrape
asyncio.run(module.collect('api', 300, False))
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    # scrapeをmockにして外部I/Oせず、実pipeのEOFで常駐processが終わることを確認する。
    output, _ = await asyncio.wait_for(process.communicate(b""), 5)
    assert process.returncode == 0
    records = [observation_adapter.validate_json(line) for line in output.splitlines()]
    finished = records[-1]
    assert isinstance(finished, CollectorFinished)
    assert finished.reason == "parent_closed"


@pytest.mark.asyncio
async def test_slow_auxiliary_probe_does_not_serialize_metric_scrapes() -> None:
    stop = asyncio.Event()
    metric_count = 0
    counts_while_probe_running: list[int] = []

    async def metrics() -> None:
        nonlocal metric_count
        metric_count += 1
        if metric_count == 4:
            stop.set()

    async def slow_probe() -> None:
        await asyncio.wait_for(stop.wait(), 1)
        counts_while_probe_running.append(metric_count)

    await asyncio.gather(
        collector.periodic(stop, monotonic() + 1, 0.005, metrics),
        collector.periodic(stop, monotonic() + 1, 0.05, slow_probe),
    )
    assert counts_while_probe_running == [4]


def test_duplicate_processes_and_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ValidationError, match="invalid_observation_instances"):
        ObservationManifest(started_at=START, duration_seconds=10, instances=["api", "api"])
    with pytest.raises(ValidationError, match="timezone"):
        sample(0, exposition(0, 0)).model_validate(
            {
                "instance": "api",
                "started_at": "2026-10-02T00:00:00",
                "monotonic_seconds": 0,
                "elapsed_seconds": 0,
                "status": "unavailable",
                "failure": "timeout",
            }
        )


def save_single_process_collection(tmp_path: Path, records: list[MetricsObservation]) -> None:
    manifest = ObservationManifest(started_at=START, duration_seconds=30, instances=["api"])
    (tmp_path / "observation.json").write_text(manifest.model_dump_json())
    completion = ObservationCompletion(
        finished_at=START + timedelta(seconds=30),
        interrupted=False,
        processes=[ProcessCompletion(instance="api", exit_code=0)],
    )
    (tmp_path / "completion.json").write_text(completion.model_dump_json())
    saved_process(tmp_path, "api", records)


def measurement_window(start: float, end: float) -> MeasurementWindow:
    return MeasurementWindow(
        start_at=START + timedelta(seconds=start), end_at=START + timedelta(seconds=end)
    )


def test_measurement_window_excludes_setup_shutdown_and_outside_counter_baseline(
    tmp_path: Path,
) -> None:
    save_single_process_collection(
        tmp_path,
        [
            sample(0, exposition(0, 0)),
            sample(1, exposition(100, 50)),
            sample(2, exposition(101, 51)),
            sample(3, exposition(104, 54)),
            sample(4, exposition(1004, 500)),
        ],
    )
    result = analyze(tmp_path, measurement_window(1.5, 3.5))
    process = result.processes[0]
    assert result.format == "local-server-analysis-v3"
    assert result.collector_lifecycle_complete is True
    assert result.measurement_usable is True
    assert result.measurement_quality_failures == []
    assert result.counters[0].delta == 3
    assert result.histograms[0].count == 3
    assert process.samples == 2
    assert process.excluded_before_window == 2
    assert process.excluded_after_window == 1
    assert process.preceding_excluded_sample_at == START + timedelta(seconds=1)
    assert process.preceding_excluded_sample_status == "ok"
    assert process.counter_baseline_at == START + timedelta(seconds=2)
    assert process.last_counter_sample_at == START + timedelta(seconds=3)
    assert process.leading_unobserved_seconds_upper == pytest.approx(0.53)
    assert process.trailing_unobserved_seconds_upper == pytest.approx(0.5)


def test_scrapes_crossing_either_window_boundary_are_not_proportionally_allocated(
    tmp_path: Path,
) -> None:
    first = sample(1, exposition(100, 50)).model_copy(update={"elapsed_seconds": 0.75})
    last = sample(3, exposition(300, 150)).model_copy(update={"elapsed_seconds": 0.75})
    save_single_process_collection(tmp_path, [first, sample(2, exposition(200, 100)), last])
    result = analyze(tmp_path, measurement_window(1.5, 3.5))
    process = result.processes[0]
    assert process.excluded_boundary_overlaps == 2
    assert process.samples == 1
    assert process.valid_counter_samples == 1
    assert result.counters == []
    assert result.histograms == []
    assert result.collector_lifecycle_complete is True
    assert result.measurement_usable is False
    assert "api:insufficient_window_coverage" in result.measurement_quality_failures


def test_sparse_samples_fail_coverage_even_when_collector_exited_normally(tmp_path: Path) -> None:
    save_single_process_collection(
        tmp_path, [sample(0, exposition(0, 0)), sample(5, exposition(10, 8))]
    )
    result = analyze(tmp_path, measurement_window(0, 5.1))
    assert result.collector_lifecycle_complete is True
    assert result.coverage_max_gap_seconds == 2
    assert result.measurement_usable is False
    assert result.processes[0].largest_sample_gap_seconds == 5
    assert result.counters[0].delta == 10


def test_window_boundary_shortfall_is_reported_even_without_missing_samples(tmp_path: Path) -> None:
    save_single_process_collection(
        tmp_path, [sample(3, exposition(10, 8)), sample(4, exposition(12, 9))]
    )
    result = analyze(tmp_path, measurement_window(0, 8))
    assert result.measurement_usable is False
    process = result.processes[0]
    assert process.leading_unobserved_seconds_upper == pytest.approx(3.03)
    assert process.trailing_unobserved_seconds_upper == 4
    assert result.counters[0].delta == 2


@pytest.mark.parametrize("case", ["reset", "missing", "restart", "new_series", "lost_series"])
def test_window_quality_fails_closed_for_reset_gap_restart_and_series_changes(
    tmp_path: Path, case: str
) -> None:
    records = [
        sample(0, exposition(0, 0)),
        sample(1, exposition(10, 8)),
        sample(2, exposition(12, 9)),
    ]
    expected = ""
    if case == "reset":
        records[2] = sample(2, exposition(2, 1))
        expected = "counter_resets"
    elif case == "missing":
        records[1] = sample(1, None)
        expected = "unavailable_samples"
    elif case == "restart":
        records[2] = sample(2, exposition(12, 9, process=20))
        expected = "process_restarts"
    elif case == "new_series":
        records[2] = sample(2, exposition(12, 9) + "# TYPE later_total counter\nlater_total 100\n")
        expected = "series_discontinuities"
    elif case == "lost_series":
        records[1] = sample(1, exposition(10, 8) + "# TYPE later_total counter\nlater_total 100\n")
        expected = "series_discontinuities"
    save_single_process_collection(tmp_path, records)
    result = analyze(tmp_path, measurement_window(0, 2.1))
    assert result.collector_lifecycle_complete is True
    assert result.processes[0].window_coverage_sufficient is True
    assert result.measurement_usable is False
    assert f"api:{expected}" in result.measurement_quality_failures


def test_no_window_is_only_collector_lifecycle_diagnostics(tmp_path: Path) -> None:
    save_single_process_collection(
        tmp_path, [sample(0, exposition(0, 0)), sample(1, exposition(1, 1))]
    )
    result = analyze(tmp_path)
    assert result.collector_lifecycle_complete is True
    assert result.measurement_usable is False
    assert result.measurement_window is None
    assert "measurement_window_not_selected" in result.measurement_quality_failures


def test_window_requires_ordered_aware_timestamps_and_normalizes_utc() -> None:
    result = MeasurementWindow.model_validate(
        {
            "start_at": "2026-10-02T09:00:00+09:00",
            "end_at": "2026-10-02T09:00:01+09:00",
        }
    )
    assert result.start_at == START
    assert result.start_at.tzinfo == UTC
    for start, end in [
        ("2026-10-02T00:00:00", "2026-10-02T00:00:01Z"),
        ("2026-10-02T00:00:02Z", "2026-10-02T00:00:01Z"),
    ]:
        with pytest.raises(ValidationError):
            MeasurementWindow.model_validate({"start_at": start, "end_at": end})


def test_histogram_validates_all_buckets_even_after_the_percentile_bucket() -> None:
    key = SeriesKey("latency", ())
    with pytest.raises(ValueError, match="invalid_histogram_bucket"):
        histogram_total(key, {"count": 100, "sum": 200, "1": 99, "10": 80, "+Inf": 100}, 1)
    with pytest.raises(ValueError, match="required_series"):
        histogram_total(key, {"count": 100, "1": 99, "+Inf": 100}, 1)


def test_collector_status_and_completion_records_cannot_hide_missing_data() -> None:
    with pytest.raises(ValidationError, match="metrics_success_requires_exposition"):
        MetricsObservation(
            instance="api", started_at=START, monotonic_seconds=0, elapsed_seconds=0, status="ok"
        )
    with pytest.raises(ValidationError, match="duplicate_process_completion"):
        ObservationCompletion(
            finished_at=START,
            interrupted=False,
            processes=[
                ProcessCompletion(instance="api", exit_code=1),
                ProcessCompletion(instance="api", exit_code=0),
            ],
        )


def test_explicit_window_cli_returns_diagnostics_and_failure_exit_for_insufficient_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from quiz_app.observability import local_observation_analysis as module

    save_single_process_collection(tmp_path, [sample(0, exposition(0, 0))])
    monkeypatch.setattr(
        module.sys,
        "argv",
        [
            "local_observation_analysis",
            str(tmp_path),
            "--start-at",
            "2026-10-02T00:00:00Z",
            "--end-at",
            "2026-10-02T00:00:05Z",
        ],
    )
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 1
    assert '"measurement_usable": false' in capsys.readouterr().out
