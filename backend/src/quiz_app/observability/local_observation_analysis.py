"""process別の累積差分を合算する。p95の平均や欠測のゼロ補完は行わない。"""

import argparse
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from quiz_app.observability.local_metric_series import (
    ParsedMetrics,
    SeriesHistory,
    SeriesKey,
    histogram_total,
    parse_metrics,
    percentile_bucket,
)
from quiz_app.observability.local_observation_models import (
    ApiInstance,
    CollectorFinished,
    DatabaseObservation,
    GaugeRange,
    HistogramTotal,
    MeasurementWindow,
    MetricTotal,
    ObservationCompletion,
    ObservationManifest,
    ProcessAnalysis,
    ServerAnalysis,
    TimedObservation,
    observation_adapter,
)


def window_position(record: TimedObservation, window: MeasurementWindow | None) -> str:
    if window is None:
        return "inside"
    completed_at = record.started_at + timedelta(seconds=record.elapsed_seconds)
    if completed_at <= window.start_at and record.started_at < window.start_at:
        return "before"
    if record.started_at > window.end_at:
        return "after"
    if record.started_at < window.start_at or completed_at > window.end_at:
        return "overlap"
    return "inside"


def analyze_process(
    path: Path,
    instance: ApiInstance,
    window: MeasurementWindow | None = None,
    max_gap_seconds: float = 2.0,
) -> ProcessAnalysis:
    result = ProcessAnalysis(instance=instance)
    if not path.exists():
        return result
    previous: ParsedMetrics | None = None
    previous_started_at: datetime | None = None
    last_process_started: float | None = None
    history = SeriesHistory()
    previous_time: datetime | None = None
    previous_monotonic: float | None = None
    counters: dict[SeriesKey, float] = defaultdict(float)
    counter_intervals: dict[SeriesKey, int] = defaultdict(int)
    histograms: dict[SeriesKey, dict[str, float]] = {}
    histogram_intervals: dict[SeriesKey, int] = defaultdict(int)
    gauges: dict[SeriesKey, list[float]] = defaultdict(list)
    with path.open() as stream:
        for line in stream:
            try:
                record = observation_adapter.validate_json(line)
            except ValidationError:
                result.invalid_records += 1
                previous = None
                continue
            if isinstance(record, CollectorFinished):
                result.finished = record.instance == instance and record.reason == "duration"
                continue
            if isinstance(record, DatabaseObservation):
                if window_position(record, window) == "inside":
                    result.database_samples += 1
                    result.database_unavailable += record.status != "ok"
                continue
            if record.instance != instance:
                result.invalid_records += 1
                previous = None
                continue
            position = window_position(record, window)
            if position != "inside":
                if position == "before":
                    result.excluded_before_window += 1
                    result.preceding_excluded_sample_at = record.started_at
                    result.preceding_excluded_sample_status = record.status
                elif position == "after":
                    result.excluded_after_window += 1
                else:
                    result.excluded_boundary_overlaps += 1
                continue
            result.samples += 1
            result.first_sample_at = result.first_sample_at or record.started_at
            result.last_sample_at = record.started_at
            result.collector_max_seconds = max(
                result.collector_max_seconds or 0, record.elapsed_seconds
            )
            if previous_time is not None:
                gap = (record.started_at - previous_time).total_seconds()
                if gap <= 0 or (
                    previous_monotonic is not None
                    and record.monotonic_seconds <= previous_monotonic
                ):
                    result.invalid_records += 1
                    previous = None
                    continue
                result.largest_sample_gap_seconds = max(result.largest_sample_gap_seconds or 0, gap)
            previous_time = record.started_at
            previous_monotonic = record.monotonic_seconds
            if record.status != "ok" or record.exposition is None:
                result.unavailable += 1
                previous = None
                continue
            try:
                current = parse_metrics(record.exposition)
                for key, values in current.histograms.items():
                    histogram_total(key, values, 0)
            except (ValueError, KeyError):
                result.invalid_records += 1
                previous = None
                continue
            for key, value in current.gauges.items():
                gauges[key].append(value)
            if current.process_started is None:
                result.identity_missing_samples += 1
                previous = None
                continue
            result.valid_counter_samples += 1
            result.counter_baseline_at = result.counter_baseline_at or record.started_at
            completed_at = record.started_at + timedelta(seconds=record.elapsed_seconds)
            result.counter_baseline_completed_at = (
                result.counter_baseline_completed_at or completed_at
            )
            result.last_counter_sample_at = record.started_at
            result.last_counter_sample_completed_at = completed_at
            if last_process_started is not None and last_process_started != current.process_started:
                result.restarts.append(record.started_at)
                previous = None
            last_process_started = current.process_started
            assessment = history.assess(
                current, previous, previous_started_at, completed_at, window
            )
            result.series_discontinuities += assessment.discontinuities
            result.created_metadata_changes += assessment.metadata_changes
            result.created_counter_series += len(assessment.created_counters)
            result.created_histogram_series += len(assessment.created_histograms)
            for key, value in current.counters.items():
                before = previous.counters.get(key) if previous is not None else None
                if key in assessment.created_counters:
                    before = 0.0
                if before is None:
                    result.unbased_series += 1
                elif value < before:
                    result.counter_resets += 1
                elif key in assessment.changed_counters:
                    continue
                else:
                    counters[key] += value - before
                    counter_intervals[key] += 1
            for key, values in current.histograms.items():
                before_histogram = previous.histograms.get(key) if previous is not None else None
                if key in assessment.created_histograms:
                    before_histogram = dict.fromkeys(values, 0.0)
                if before_histogram is None or before_histogram.keys() != values.keys():
                    result.unbased_series += 1
                    if before_histogram is not None:
                        result.series_discontinuities += 1
                    continue
                if any(value < before_histogram[part] for part, value in values.items()):
                    result.histogram_resets += 1
                    continue
                if key in assessment.changed_histograms:
                    continue
                difference = {
                    part: value - before_histogram[part] for part, value in values.items()
                }
                try:
                    histogram_total(key, difference, 1)
                except ValueError:
                    result.invalid_records += 1
                    continue
                if key in histograms and histograms[key].keys() != difference.keys():
                    result.invalid_records += 1
                    continue
                total = histograms.setdefault(key, dict.fromkeys(difference, 0.0))
                for part, value in difference.items():
                    total[part] += value
                histogram_intervals[key] += 1
            previous = current
            previous_started_at = record.started_at
    result.counters = [
        MetricTotal(
            name=key.name, labels=dict(key.labels), delta=value, intervals=counter_intervals[key]
        )
        for key, value in counters.items()
    ]
    result.histograms = [
        histogram_total(key, values, histogram_intervals[key]) for key, values in histograms.items()
    ]
    result.gauges = [
        GaugeRange(
            name=key.name,
            labels=dict(key.labels),
            minimum=min(values),
            maximum=max(values),
            last=values[-1],
            samples=len(values),
        )
        for key, values in gauges.items()
    ]
    if (
        window is not None
        and result.counter_baseline_completed_at is not None
        and result.last_counter_sample_at is not None
    ):
        result.leading_unobserved_seconds_upper = (
            result.counter_baseline_completed_at - window.start_at
        ).total_seconds()
        result.trailing_unobserved_seconds_upper = (
            window.end_at - result.last_counter_sample_at
        ).total_seconds()
        result.window_coverage_sufficient = (
            result.valid_counter_samples >= 2
            and result.leading_unobserved_seconds_upper <= max_gap_seconds
            and result.trailing_unobserved_seconds_upper <= max_gap_seconds
            and result.largest_sample_gap_seconds is not None
            and result.largest_sample_gap_seconds <= max_gap_seconds
        )
    return result


def combine(
    processes: list[ProcessAnalysis],
) -> tuple[list[MetricTotal], list[HistogramTotal], list[str]]:
    counters: dict[SeriesKey, MetricTotal] = {}
    histograms: dict[SeriesKey, HistogramTotal] = {}
    incompatible: set[SeriesKey] = set()
    for process in processes:
        for value in process.counters:
            key = SeriesKey(value.name, tuple(sorted(value.labels.items())))
            if key not in counters:
                counters[key] = value.model_copy(deep=True)
            else:
                counters[key].delta += value.delta
                counters[key].intervals += value.intervals
        for value in process.histograms:
            key = SeriesKey(value.name, tuple(sorted(value.labels.items())))
            if key not in histograms:
                histograms[key] = value.model_copy(deep=True)
                continue
            total = histograms[key]
            if total.buckets.keys() != value.buckets.keys():
                incompatible.add(key)
                continue
            total.count += value.count
            total.sum += value.sum
            total.intervals += value.intervals
            for bound, count in value.buckets.items():
                total.buckets[bound] += count
    results: list[HistogramTotal] = []
    for key, value in histograms.items():
        if key in incompatible:
            continue
        value.p95, value.p95_lower_bound, value.p95_upper_bound = percentile_bucket(
            value.buckets, value.count
        )
        results.append(value)
    return (
        list(counters.values()),
        results,
        [f"{key.name}{dict(key.labels)}" for key in incompatible],
    )


def analyze(
    directory: Path,
    window: MeasurementWindow | None = None,
    max_gap_seconds: float | None = None,
) -> ServerAnalysis:
    manifest = ObservationManifest.model_validate_json((directory / "observation.json").read_text())
    coverage_limit = (
        manifest.scrape_interval_seconds * 2 if max_gap_seconds is None else max_gap_seconds
    )
    if not math.isfinite(coverage_limit) or coverage_limit <= 0:
        raise ValueError("coverage_max_gap_must_be_positive")
    processes = [
        analyze_process(directory / instance / "samples.jsonl", instance, window, coverage_limit)
        for instance in manifest.instances
    ]
    completion_path = directory / "completion.json"
    completion = (
        ObservationCompletion.model_validate_json(completion_path.read_text())
        if completion_path.exists()
        else None
    )
    counters, histograms, incompatible = combine(processes)
    lifecycle_complete = (
        completion is not None
        and not completion.interrupted
        and (
            {value.instance for value in completion.processes} == set(manifest.instances)
            and all(value.exit_code == 0 for value in completion.processes)
            and all(value.finished for value in processes)
        )
    )
    failures: list[str] = []
    if window is None:
        failures.append("measurement_window_not_selected")
    if not lifecycle_complete:
        failures.append("collector_lifecycle_incomplete")
    if incompatible:
        failures.append("incompatible_histograms")
    for value in processes:
        if not value.window_coverage_sufficient:
            failures.append(f"{value.instance}:insufficient_window_coverage")
        for reason, count in (
            ("unavailable_samples", value.unavailable),
            ("invalid_records", value.invalid_records),
            ("identity_missing", value.identity_missing_samples),
            ("process_restarts", len(value.restarts)),
            ("counter_resets", value.counter_resets),
            ("histogram_resets", value.histogram_resets),
            ("series_discontinuities", value.series_discontinuities),
            ("created_metadata_changes", value.created_metadata_changes),
        ):
            if count:
                failures.append(f"{value.instance}:{reason}")
    return ServerAnalysis(
        collector_lifecycle_complete=lifecycle_complete,
        measurement_window=window,
        measurement_usable=not failures,
        measurement_quality_failures=failures,
        coverage_max_gap_seconds=coverage_limit,
        processes=processes,
        counters=counters,
        histograms=histograms,
        incompatible_histograms=incompatible,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--start-at", help="測定開始のtimezone付き日時。出力はUTCへ正規化")
    parser.add_argument("--end-at", help="測定終了のtimezone付き日時。--start-atと対で指定")
    parser.add_argument(
        "--max-sample-gap-seconds",
        type=float,
        help="端の不足と標本間隔の上限。既定はmanifest scrape周期の2倍",
    )
    arguments = parser.parse_args()
    if (arguments.start_at is None) != (arguments.end_at is None):
        parser.error("start-at and end-at are required together")
    window = None
    if arguments.start_at is not None:
        window = MeasurementWindow(start_at=arguments.start_at, end_at=arguments.end_at)
    result = analyze(arguments.directory, window, arguments.max_sample_gap_seconds)
    print(result.model_dump_json(indent=2))
    if window is not None and not result.measurement_usable:
        sys.exit(1)


if __name__ == "__main__":
    main()
