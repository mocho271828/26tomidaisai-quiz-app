"""Prometheus系列の解析と生成根拠。同じprocessの隣接標本だけを関連付ける。"""

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from prometheus_client.parser import text_string_to_metric_families

from quiz_app.observability.local_observation_models import HistogramTotal, MeasurementWindow


@dataclass(frozen=True)
class SeriesKey:
    name: str
    labels: tuple[tuple[str, str], ...]


@dataclass
class ParsedMetrics:
    counters: dict[SeriesKey, float] = field(default_factory=dict)
    histograms: dict[SeriesKey, dict[str, float]] = field(default_factory=dict)
    gauges: dict[SeriesKey, float] = field(default_factory=dict)
    counter_created: dict[SeriesKey, float] = field(default_factory=dict)
    histogram_created: dict[SeriesKey, float] = field(default_factory=dict)
    process_started: float | None = None


def parse_metrics(exposition: str) -> ParsedMetrics:
    """公式parserでPrometheus契約を読み、未知typeは計算対象へ入れない。"""
    result = ParsedMetrics()
    created: dict[SeriesKey, float] = {}
    for metric in text_string_to_metric_families(exposition):
        for sample in metric.samples:
            value = float(sample.value)
            if not math.isfinite(value) or (metric.type in {"counter", "histogram"} and value < 0):
                raise ValueError("invalid_metric_value")
            labels = tuple(sorted(sample.labels.items()))
            key = SeriesKey(sample.name, labels)
            if sample.name.endswith("_created") and metric.type in {
                "counter",
                "histogram",
                "gauge",
            }:
                if value < 0 or (key in created and created[key] != value):
                    raise ValueError("invalid_creation_metadata")
                created[key] = value
            if metric.type == "counter" and sample.name.endswith("_total"):
                result.counters[key] = value
            elif metric.type == "histogram" and sample.name.endswith(("_bucket", "_count", "_sum")):
                group = SeriesKey(metric.name, tuple(pair for pair in labels if pair[0] != "le"))
                part = sample.labels.get("le") if sample.name.endswith("_bucket") else None
                if part is None:
                    part = "count" if sample.name.endswith("_count") else "sum"
                result.histograms.setdefault(group, {})[part] = value
            elif metric.type == "gauge":
                result.gauges[key] = value
                if sample.name == "process_start_time_seconds":
                    result.process_started = value
    for key in result.counters:
        metadata = SeriesKey(key.name.removesuffix("_total") + "_created", key.labels)
        if metadata in created:
            result.counter_created[key] = created[metadata]
    for key in result.histograms:
        metadata = SeriesKey(key.name + "_created", key.labels)
        if metadata in created:
            result.histogram_created[key] = created[metadata]
    return result


SeriesIdentity = tuple[Literal["counter", "histogram"], SeriesKey]


@dataclass
class SeriesAssessment:
    created_counters: set[SeriesKey] = field(default_factory=set)
    created_histograms: set[SeriesKey] = field(default_factory=set)
    discontinuities: int = 0
    metadata_changes: int = 0
    changed_counters: set[SeriesKey] = field(default_factory=set)
    changed_histograms: set[SeriesKey] = field(default_factory=set)


@dataclass
class SeriesHistory:
    # 欠測・消失・再起動の後も履歴を保持し、再出現を新規生成と誤認しない。
    seen: dict[SeriesIdentity, float | None] = field(default_factory=dict)

    def assess(
        self,
        current: ParsedMetrics,
        previous: ParsedMetrics | None,
        previous_started_at: datetime | None,
        completed_at: datetime,
        window: MeasurementWindow | None,
    ) -> SeriesAssessment:
        """生成時0を証明できる初出だけ区別し、それ以外の不連続は維持する。"""
        result = SeriesAssessment()
        kinds: tuple[Literal["counter", "histogram"], ...] = ("counter", "histogram")
        for kind in kinds:
            keys = current.counters.keys() if kind == "counter" else current.histograms.keys()
            before_keys = (
                (previous.counters.keys() if kind == "counter" else previous.histograms.keys())
                if previous is not None
                else set()
            )
            metadata = current.counter_created if kind == "counter" else current.histogram_created
            accepted = result.created_counters if kind == "counter" else result.created_histograms
            changed = result.changed_counters if kind == "counter" else result.changed_histograms
            if previous is not None:
                result.discontinuities += len(before_keys - keys)
            for key in keys:
                identity = (kind, key)
                created = metadata.get(key)
                if identity in self.seen and self.seen[identity] != created:
                    result.metadata_changes += 1
                    changed.add(key)
                if previous is not None and key not in before_keys:
                    # prometheus-clientの収集は非atomicなため、生成は前/現scrape内もあり得る。
                    # 不在を0とはみなさず、同一processの_createdがこの有効区間内の時だけ使う。
                    proven = (
                        identity not in self.seen
                        and created is not None
                        and window is not None
                        and previous_started_at is not None
                        and current.process_started is not None
                        and previous.process_started == current.process_started
                        and current.process_started <= created
                        and previous_started_at.timestamp() <= created <= completed_at.timestamp()
                        and window.start_at.timestamp() <= created <= window.end_at.timestamp()
                    )
                    if proven:
                        accepted.add(key)
                    else:
                        result.discontinuities += 1
                self.seen[identity] = created
        return result


def percentile_bucket(buckets: dict[str, float], count: float) -> tuple[float | None, ...]:
    """累積bucketからp95を補間する。+Inf区間は上限/推定値を欠測にする。"""
    if count <= 0:
        return None, None, None
    target = count * 0.95
    previous_bound, previous_count = 0.0, 0.0
    for bound, cumulative in sorted((float(bound), value) for bound, value in buckets.items()):
        if cumulative < previous_count:
            raise ValueError("non_monotonic_histogram")
        if cumulative >= target:
            if math.isinf(bound):
                return None, previous_bound, None
            width = cumulative - previous_count
            estimate = (
                bound
                if width == 0
                else previous_bound + ((bound - previous_bound) * (target - previous_count) / width)
            )
            return estimate, previous_bound, bound
        previous_bound, previous_count = bound, cumulative
    raise ValueError("incomplete_histogram")


def histogram_total(key: SeriesKey, values: dict[str, float], intervals: int) -> HistogramTotal:
    if not {"count", "sum", "+Inf"} <= values.keys():
        raise ValueError("histogram_required_series_missing")
    buckets = {name: value for name, value in values.items() if name not in {"count", "sum"}}
    count = values["count"]
    if "+Inf" not in buckets or buckets["+Inf"] != count:
        raise ValueError("histogram_count_mismatch")
    previous = 0.0
    for bound, cumulative in sorted((float(bound), value) for bound, value in buckets.items()):
        if (
            math.isnan(bound)
            or bound == -math.inf
            or not math.isfinite(cumulative)
            or cumulative < previous
            or cumulative > count
        ):
            raise ValueError("invalid_histogram_bucket")
        previous = cumulative
    p95, lower, upper = percentile_bucket(buckets, count)
    return HistogramTotal(
        name=key.name,
        labels=dict(key.labels),
        count=count,
        sum=values["sum"],
        buckets=buckets,
        intervals=intervals,
        p95=p95,
        p95_lower_bound=lower,
        p95_upper_bound=upper,
    )
