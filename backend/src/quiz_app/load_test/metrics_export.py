"""固定PromQLだけを安全な負荷試験artifactへexportする。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, RootModel, StrictStr, ValidationError

from quiz_app.load_test.manifest import MAX_MANIFEST_BYTES, LoadTestManifest

PROMETHEUS_QUERY_RANGE_URL: Final = "http://prometheus:9090/api/v1/query_range"
METRICS_ARTIFACT_DIRECTORY: Final = Path("/artifacts")
MAXIMUM_RESPONSE_BYTES: Final = 16_000_000
QUERY_TIMEOUT_SECONDS: Final = 5.0
QUERY_STEP_SECONDS: Final = 1
QUERY_CHUNK_DURATION: Final = timedelta(minutes=15)
MAXIMUM_EXPORT_WINDOW: Final = timedelta(minutes=90)
MAXIMUM_TOTAL_SERIES: Final = 1_000
MAXIMUM_TOTAL_SAMPLES: Final = 1_000_000
SOURCE_COMMIT_PATTERN: Final = r"^[0-9a-f]{40}$"
APPROVED_ROUTE_TEMPLATES: Final = frozenset(
    {
        "unmatched",
        "/api/docs",
        "/docs/oauth2-redirect",
        "/api/openapi.json",
        "/redoc",
        "/api/health/live",
        "/api/health/ready",
        "/api/v1/operator/auth/login",
        "/api/v1/operator/auth/session",
        "/api/v1/operator/auth/logout",
        "/api/v1/operator/state",
        "/api/v1/operator/ranking",
        "/api/v1/operator/result-visibility",
        "/api/v1/operator/rounds/{round_id}/prepare",
        "/api/v1/operator/rounds/{round_id}/question",
        "/api/v1/operator/rounds/{round_id}/schedule",
        "/api/v1/operator/rounds/{round_id}/retry-results",
        "/api/v1/operator/rounds/{round_id}/publish-results",
        "/api/v1/operator/rounds/{round_id}/advance",
        "/api/v1/operator/rounds/{round_id}/complete-event",
        "/api/v1/participant/join",
        "/api/v1/participant/session",
        "/api/v1/participant/result",
        "/api/v1/participant/session/heartbeat",
        "/api/v1/participant/rounds/{round_id}/readiness",
        "/api/v1/participant/rounds/{round_id}/answers",
        "/api/v1/participant/answers/{client_answer_id}/status",
        "/api/v1/time",
    }
)
CORE_QUERY_IDS: Final = frozenset(
    {
        "http_requests_rate",
        "websocket_attempts",
        "redis_dependency_up",
        "database_pool_supported",
        "host_cpu_utilization",
        "postgres_dependency_up",
        "redis_exporter_dependency_up",
        "scrape_targets_up",
    }
)
EXPECTED_SCRAPE_JOBS: Final = frozenset(
    {"quiz-api", "quiz-gateway", "quiz-postgres", "quiz-redis", "quiz-node"}
)
EXPECTED_API_INSTANCES: Final = frozenset({"api-primary", "api-secondary"})


@dataclass(frozen=True, slots=True)
class FixedQuery:
    query_id: str
    expression: str
    expected_labels: frozenset[str]
    allow_nan_as_missing: bool = False


FIXED_QUERIES: Final = (
    FixedQuery(
        "http_requests_rate",
        "sum by(instance,route,status_class)(rate(quiz_http_requests_total[1m]))",
        frozenset({"instance", "route", "status_class"}),
    ),
    FixedQuery(
        "http_latency_p95",
        "histogram_quantile(0.95,sum by(le,instance,route)"
        "(rate(quiz_http_request_duration_seconds_bucket[1m])))",
        frozenset({"instance", "route"}),
        True,
    ),
    FixedQuery(
        "http_latency_p99",
        "histogram_quantile(0.99,sum by(le,instance,route)"
        "(rate(quiz_http_request_duration_seconds_bucket[1m])))",
        frozenset({"instance", "route"}),
        True,
    ),
    FixedQuery(
        "http_in_progress",
        "sum by(instance)(quiz_http_requests_in_progress)",
        frozenset({"instance"}),
    ),
    FixedQuery(
        "websocket_active",
        "sum by(instance,audience)(quiz_websocket_active)",
        frozenset({"instance", "audience"}),
    ),
    FixedQuery(
        "websocket_attempts",
        "sum by(instance,audience)(quiz_websocket_attempts_total)",
        frozenset({"instance", "audience"}),
    ),
    FixedQuery(
        "websocket_closes",
        "sum by(instance,audience,outcome,code_class)(quiz_websocket_closes_total)",
        frozenset({"instance", "audience", "outcome", "code_class"}),
    ),
    FixedQuery(
        "polling_fallbacks",
        "sum by(instance,audience,reason)(quiz_realtime_polling_fallback_total)",
        frozenset({"instance", "audience", "reason"}),
    ),
    FixedQuery(
        "redis_publish",
        "sum by(instance,audience,result)(quiz_redis_publish_total)",
        frozenset({"instance", "audience", "result"}),
    ),
    FixedQuery(
        "redis_dependency_up",
        "min by(instance)(quiz_redis_dependency_up)",
        frozenset({"instance"}),
    ),
    FixedQuery(
        "database_pool",
        "sum by(instance,state)(quiz_db_pool_connections)",
        frozenset({"instance", "state"}),
    ),
    FixedQuery(
        "database_pool_supported",
        "min by(instance)(quiz_db_pool_metrics_supported)",
        frozenset({"instance"}),
    ),
    FixedQuery(
        "api_process_cpu",
        'sum by(instance)(rate(process_cpu_seconds_total{job="quiz-api"}[1m]))',
        frozenset({"instance"}),
    ),
    FixedQuery(
        "api_process_resident_memory",
        'sum by(instance)(process_resident_memory_bytes{job="quiz-api"})',
        frozenset({"instance"}),
    ),
    FixedQuery(
        "host_cpu_utilization",
        '1 - avg(rate(node_cpu_seconds_total{mode="idle"}[1m]))',
        frozenset(),
    ),
    FixedQuery(
        "host_memory_utilization",
        "avg(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))",
        frozenset(),
    ),
    FixedQuery(
        "gateway_5xx_rate",
        'sum(rate(caddy_http_request_duration_seconds_count{handler="reverse_proxy",'
        'code=~"5.."}[1m]))',
        frozenset(),
    ),
    FixedQuery(
        "gateway_latency_p95",
        "histogram_quantile(0.95,sum by(le)"
        '(rate(caddy_http_request_duration_seconds_bucket{handler="reverse_proxy"}[1m])))',
        frozenset(),
        True,
    ),
    FixedQuery(
        "gateway_in_progress",
        'sum(caddy_http_requests_in_flight{handler="reverse_proxy"})',
        frozenset(),
    ),
    FixedQuery("postgres_connections", "sum(pg_stat_database_numbackends)", frozenset()),
    FixedQuery("postgres_dependency_up", "min(pg_up)", frozenset()),
    FixedQuery("postgres_deadlocks", "sum(pg_stat_database_deadlocks)", frozenset()),
    FixedQuery(
        "postgres_transaction_rate",
        "sum(rate(pg_stat_database_xact_commit[1m])) + "
        "sum(rate(pg_stat_database_xact_rollback[1m]))",
        frozenset(),
    ),
    FixedQuery(
        "postgres_blocks_read_rate",
        "sum(rate(pg_stat_database_blks_read[1m]))",
        frozenset(),
    ),
    FixedQuery(
        "postgres_blocks_hit_rate",
        "sum(rate(pg_stat_database_blks_hit[1m]))",
        frozenset(),
    ),
    # pg_locks_countにgranted labelがないため、待機数ではなく総lock数のsurrogateとする。
    FixedQuery("postgres_lock_count", "sum(pg_locks_count)", frozenset()),
    FixedQuery("redis_connected_clients", "sum(redis_connected_clients)", frozenset()),
    FixedQuery("redis_exporter_dependency_up", "min(redis_up)", frozenset()),
    FixedQuery("redis_pubsub_channels", "sum(redis_pubsub_channels)", frozenset()),
    FixedQuery("redis_pubsub_patterns", "sum(redis_pubsub_patterns)", frozenset()),
    FixedQuery(
        "redis_rejected_connections_rate",
        "sum(rate(redis_rejected_connections_total[1m]))",
        frozenset(),
    ),
    FixedQuery(
        "redis_net_input_rate",
        "sum(rate(redis_net_input_bytes_total[1m]))",
        frozenset(),
    ),
    FixedQuery(
        "redis_net_output_rate",
        "sum(rate(redis_net_output_bytes_total[1m]))",
        frozenset(),
    ),
    FixedQuery("host_load1", "avg(node_load1)", frozenset()),
    FixedQuery(
        "scrape_targets_up",
        'min by(job)(up{job=~"quiz-api|quiz-gateway|quiz-postgres|quiz-redis|quiz-node"})',
        frozenset({"job"}),
    ),
)


class PrometheusSample(RootModel[tuple[float, StrictStr]]):
    pass


class PrometheusSeries(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: dict[StrictStr, StrictStr]
    values: list[PrometheusSample]


class PrometheusData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resultType: Literal["matrix"]
    result: list[PrometheusSeries]


class PrometheusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["success"]
    data: PrometheusData


class ExportLabel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: StrictStr = Field(min_length=1, max_length=32)
    value: StrictStr = Field(max_length=160)


class ExportSample(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: float
    value: float


class ExportSeries(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    labels: tuple[ExportLabel, ...]
    samples: tuple[ExportSample, ...]


class ExportQueryResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query_id: StrictStr
    series: tuple[ExportSeries, ...]


class MetricsExportArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["load-test-prometheus-export-v1"]
    run_id: UUID
    source_commit: StrictStr = Field(pattern=SOURCE_COMMIT_PATTERN)
    generated_at: datetime
    started_at: datetime
    ended_at: datetime
    step_seconds: Literal[1]
    results: tuple[ExportQueryResult, ...]


class MetricsExportError(RuntimeError):
    """秘密値を含めない固定codeでexport失敗を表す。"""


ResponseOpener = Callable[..., object]


class ManifestStat(Protocol):
    st_mode: int
    st_uid: int
    st_nlink: int
    st_size: int


def _is_safe_manifest_parent_status(status: ManifestStat, effective_uid: int) -> bool:
    return (
        stat.S_ISDIR(status.st_mode)
        and status.st_uid in {0, effective_uid}
        and not stat.S_IMODE(status.st_mode) & 0o022
    )


def _is_safe_manifest_file_status(status: ManifestStat, effective_uid: int) -> bool:
    return (
        stat.S_ISREG(status.st_mode)
        and stat.S_IMODE(status.st_mode) == 0o600
        and status.st_uid == effective_uid
        and status.st_nlink == 1
        and status.st_size <= MAX_MANIFEST_BYTES
    )


@dataclass(frozen=True, slots=True)
class MetricsArtifactPaths:
    json_path: Path
    csv_path: Path
    checksum_path: Path


def _validate_window(started_at: datetime, ended_at: datetime) -> None:
    if started_at.tzinfo is None or ended_at.tzinfo is None:
        raise MetricsExportError("metrics_export_window_timezone_required")
    if ended_at <= started_at or ended_at - started_at > MAXIMUM_EXPORT_WINDOW:
        raise MetricsExportError("metrics_export_window_invalid")


def _validate_label_value(name: str, value: str) -> None:
    fixed_values = {
        "instance": {"api-primary", "api-secondary"},
        "status_class": {"1xx", "2xx", "3xx", "4xx", "5xx", "other"},
        "audience": {"participant", "operator", "unmatched"},
        "reason": {"realtime_unavailable"},
        "result": {"success", "error"},
        "state": {"checked_in", "checked_out", "overflow", "size"},
        "code_class": {"1xxx", "2xxx", "3xxx", "4xxx", "other"},
        "job": {"quiz-api", "quiz-gateway", "quiz-postgres", "quiz-redis", "quiz-node"},
    }
    if name in fixed_values and value not in fixed_values[name]:
        raise MetricsExportError("metrics_export_label_value_invalid")
    if name == "route" and value not in APPROVED_ROUTE_TEMPLATES:
        raise MetricsExportError("metrics_export_label_value_invalid")
    if name == "outcome" and value not in {
        "normal",
        "unsupported_data",
        "origin_rejected",
        "realtime_unavailable",
        "authentication_required",
        "event_not_found",
        "client_disconnect",
        "server_close",
        "application_exit",
    }:
        raise MetricsExportError("metrics_export_label_value_invalid")


def _read_response(response: object) -> bytes:
    reader = getattr(response, "read", None)
    closer = getattr(response, "close", None)
    if not callable(reader) or not callable(closer):
        raise MetricsExportError("prometheus_response_invalid")
    try:
        body: object = reader(MAXIMUM_RESPONSE_BYTES + 1)
    finally:
        closer()
    if not isinstance(body, bytes) or len(body) > MAXIMUM_RESPONSE_BYTES:
        raise MetricsExportError("prometheus_response_invalid")
    return body


def _fetch_fixed_query_chunk(
    query: FixedQuery,
    started_at: datetime,
    ended_at: datetime,
    *,
    opener: ResponseOpener = urlopen,
) -> ExportQueryResult:
    """固定endpointへ固定queryを送り、許可labelだけをtyped resultへ変換する。"""

    _validate_window(started_at, ended_at)
    endpoint = urlsplit(PROMETHEUS_QUERY_RANGE_URL)
    if (
        endpoint.scheme != "http"
        or endpoint.hostname != "prometheus"
        or endpoint.port != 9090
        or endpoint.path != "/api/v1/query_range"
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.query
        or endpoint.fragment
    ):
        raise MetricsExportError("prometheus_endpoint_invalid")
    parameters = urlencode(
        {
            "query": query.expression,
            "start": started_at.timestamp(),
            "end": ended_at.timestamp(),
            "step": QUERY_STEP_SECONDS,
        }
    )
    # URLは固定internal endpointとurlencode済みparameterだけから構築する。
    request = Request(  # noqa: S310
        f"{PROMETHEUS_QUERY_RANGE_URL}?{parameters}",
        method="GET",
    )
    try:
        response = opener(request, timeout=QUERY_TIMEOUT_SECONDS)
        body = _read_response(response)
        payload = PrometheusResponse.model_validate_json(body)
    except MetricsExportError:
        raise
    except (HTTPError, URLError, TimeoutError, OSError, ValidationError) as error:
        raise MetricsExportError("prometheus_query_failed") from error

    exported_series: list[ExportSeries] = []
    for series in payload.data.result:
        if set(series.metric) != query.expected_labels:
            raise MetricsExportError("metrics_export_label_name_invalid")
        labels = []
        for name, value in sorted(series.metric.items()):
            _validate_label_value(name, value)
            labels.append(ExportLabel(name=name, value=value))
        samples = []
        previous_timestamp: float | None = None
        for sample in series.values:
            timestamp, raw_value = sample.root
            try:
                value = float(raw_value)
            except ValueError as error:
                raise MetricsExportError("metrics_export_sample_invalid") from error
            if math.isnan(value) and query.allow_nan_as_missing:
                continue
            if (
                not math.isfinite(timestamp)
                or not started_at.timestamp() <= timestamp <= ended_at.timestamp()
                or (previous_timestamp is not None and timestamp <= previous_timestamp)
                or not math.isfinite(value)
            ):
                raise MetricsExportError("metrics_export_sample_invalid")
            samples.append(ExportSample(timestamp=timestamp, value=value))
            previous_timestamp = timestamp
        exported_series.append(ExportSeries(labels=tuple(labels), samples=tuple(samples)))
    return ExportQueryResult(query_id=query.query_id, series=tuple(exported_series))


def fetch_fixed_query(
    query: FixedQuery,
    started_at: datetime,
    ended_at: datetime,
    *,
    opener: ResponseOpener = urlopen,
) -> ExportQueryResult:
    """固定queryを15分chunkで取得し、境界sampleを一致確認して結合する。"""

    _validate_window(started_at, ended_at)
    chunk_start = started_at
    merged: dict[tuple[ExportLabel, ...], list[ExportSample]] = {}
    while chunk_start < ended_at:
        chunk_end = min(chunk_start + QUERY_CHUNK_DURATION, ended_at)
        chunk = _fetch_fixed_query_chunk(
            query,
            chunk_start,
            chunk_end,
            opener=opener,
        )
        for series in chunk.series:
            samples = merged.setdefault(series.labels, [])
            for sample in series.samples:
                if samples and sample.timestamp == samples[-1].timestamp:
                    if sample.value != samples[-1].value:
                        raise MetricsExportError("metrics_export_chunk_boundary_conflict")
                    continue
                if samples and sample.timestamp < samples[-1].timestamp:
                    raise MetricsExportError("metrics_export_sample_invalid")
                samples.append(sample)
        chunk_start = chunk_end
    return ExportQueryResult(
        query_id=query.query_id,
        series=tuple(
            ExportSeries(labels=labels, samples=tuple(merged[labels]))
            for labels in sorted(
                merged,
                key=lambda values: tuple((label.name, label.value) for label in values),
            )
        ),
    )


def _has_fresh_terminal_sample(
    series: ExportSeries,
    *,
    ended_at: datetime,
    expected_value: float,
) -> bool:
    if not series.samples:
        return False
    terminal_sample = series.samples[-1]
    return (
        terminal_sample.timestamp >= ended_at.timestamp() - QUERY_STEP_SECONDS
        and terminal_sample.value == expected_value
    )


def _validate_complete_export(
    results: tuple[ExportQueryResult, ...],
    *,
    ended_at: datetime,
) -> None:
    by_query_id = {result.query_id: result for result in results}
    for query_id in CORE_QUERY_IDS:
        result = by_query_id[query_id]
        if not any(series.samples for series in result.series):
            raise MetricsExportError("metrics_export_core_query_empty")

    scrape_result = by_query_id["scrape_targets_up"]
    jobs: set[str] = set()
    for series in scrape_result.series:
        labels = {label.name: label.value for label in series.labels}
        job = labels.get("job")
        if job is None or not _has_fresh_terminal_sample(
            series,
            ended_at=ended_at,
            expected_value=1.0,
        ):
            raise MetricsExportError("metrics_export_scrape_recovery_invalid")
        jobs.add(job)
    if jobs != EXPECTED_SCRAPE_JOBS:
        raise MetricsExportError("metrics_export_scrape_recovery_invalid")

    for query_id in ("redis_dependency_up", "database_pool_supported"):
        instance_result = by_query_id[query_id]
        recovered_instances: set[str] = set()
        for series in instance_result.series:
            labels = {label.name: label.value for label in series.labels}
            instance = labels.get("instance")
            if instance is None or not _has_fresh_terminal_sample(
                series,
                ended_at=ended_at,
                expected_value=1.0,
            ):
                raise MetricsExportError("metrics_export_api_recovery_invalid")
            recovered_instances.add(instance)
        if recovered_instances != EXPECTED_API_INSTANCES:
            raise MetricsExportError("metrics_export_api_recovery_invalid")

    for query_id in ("postgres_dependency_up", "redis_exporter_dependency_up"):
        dependency_result = by_query_id[query_id]
        if len(dependency_result.series) != 1 or not _has_fresh_terminal_sample(
            dependency_result.series[0],
            ended_at=ended_at,
            expected_value=1.0,
        ):
            raise MetricsExportError("metrics_export_dependency_recovery_invalid")


def export_metrics(
    manifest: LoadTestManifest,
    *,
    now: datetime,
    opener: ResponseOpener = urlopen,
) -> MetricsExportArtifact:
    started_at = manifest.scheduled_start_at
    ended_at = started_at + timedelta(seconds=manifest.scenario.lifecycle.ramp_down_end_seconds)
    _validate_window(started_at, ended_at)
    if now.tzinfo is None or now < ended_at:
        raise MetricsExportError("metrics_export_too_early")
    results_list: list[ExportQueryResult] = []
    total_series = 0
    total_samples = 0
    for query in FIXED_QUERIES:
        result = fetch_fixed_query(query, started_at, ended_at, opener=opener)
        total_series += len(result.series)
        total_samples += sum(len(series.samples) for series in result.series)
        if total_series > MAXIMUM_TOTAL_SERIES or total_samples > MAXIMUM_TOTAL_SAMPLES:
            raise MetricsExportError("metrics_export_result_limit_exceeded")
        results_list.append(result)
    results = tuple(results_list)
    _validate_complete_export(results, ended_at=ended_at)
    return MetricsExportArtifact(
        format="load-test-prometheus-export-v1",
        run_id=manifest.run_id,
        source_commit=manifest.artifacts.source_commit,
        generated_at=now.astimezone(UTC),
        started_at=started_at,
        ended_at=ended_at,
        step_seconds=QUERY_STEP_SECONDS,
        results=results,
    )


def _runtime_repository_root() -> Path:
    module_path = Path(__file__).resolve(strict=True)
    layouts = (
        (
            Path("backend/src/quiz_app/load_test/metrics_export.py"),
            (Path("backend/pyproject.toml"), Path("compose.yaml")),
        ),
        (
            Path("src/quiz_app/load_test/metrics_export.py"),
            (Path("pyproject.toml"), Path("alembic.ini")),
        ),
    )
    for module_sentinel, root_sentinels in layouts:
        for candidate_root in module_path.parents:
            candidate_module = candidate_root / module_sentinel
            if (
                candidate_module.is_file()
                and candidate_module.samefile(module_path)
                and all((candidate_root / sentinel).is_file() for sentinel in root_sentinels)
            ):
                return candidate_root.resolve(strict=True)
    raise MetricsExportError("metrics_export_manifest_invalid")


def load_export_manifest(path: Path, repository_root: Path) -> LoadTestManifest:
    """root所有mount親を許可しつつ、manifest file自体をruntime UIDへ固定する。"""

    try:
        resolved_repository = repository_root.resolve(strict=True)
        resolved_parent = path.parent.resolve(strict=True)
        path_status = path.lstat()
    except (OSError, ValueError, ValidationError) as error:
        raise MetricsExportError("metrics_export_manifest_invalid") from error
    if resolved_repository != _runtime_repository_root():
        raise MetricsExportError("metrics_export_manifest_invalid")
    if (
        not path.is_absolute()
        or ".." in path.parts
        or resolved_parent.is_relative_to(resolved_repository)
        or path.is_symlink()
        or not _is_safe_manifest_file_status(path_status, os.geteuid())
    ):
        raise MetricsExportError("metrics_export_manifest_invalid")
    for ancestor in (path.parent, *path.parent.parents):
        try:
            ancestor_status = ancestor.lstat()
        except OSError as error:
            raise MetricsExportError("metrics_export_manifest_invalid") from error
        if ancestor.is_symlink() or not _is_safe_manifest_parent_status(
            ancestor_status, os.geteuid()
        ):
            raise MetricsExportError("metrics_export_manifest_invalid")
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        file_status = os.fstat(descriptor)
        if (file_status.st_dev, file_status.st_ino) != (
            path_status.st_dev,
            path_status.st_ino,
        ) or not _is_safe_manifest_file_status(file_status, os.geteuid()):
            raise MetricsExportError("metrics_export_manifest_invalid")
        with os.fdopen(descriptor, "rb") as manifest_file:
            descriptor = -1
            body = manifest_file.read(MAX_MANIFEST_BYTES + 1)
        if len(body) > MAX_MANIFEST_BYTES:
            raise MetricsExportError("metrics_export_manifest_invalid")
        return LoadTestManifest.model_validate_json(body)
    except MetricsExportError:
        raise
    except (OSError, ValueError, ValidationError) as error:
        raise MetricsExportError("metrics_export_manifest_invalid") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _serialize_csv(artifact: MetricsExportArtifact) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        (
            "run_id",
            "source_commit",
            "started_at",
            "ended_at",
            "query_id",
            "labels_json",
            "timestamp",
            "value",
        )
    )
    for result in artifact.results:
        for series in result.series:
            labels = {label.name: label.value for label in series.labels}
            serialized_labels = json.dumps(
                labels, ensure_ascii=True, separators=(",", ":"), sort_keys=True
            )
            for sample in series.samples:
                writer.writerow(
                    (
                        artifact.run_id,
                        artifact.source_commit,
                        artifact.started_at.isoformat(),
                        artifact.ended_at.isoformat(),
                        result.query_id,
                        serialized_labels,
                        sample.timestamp,
                        sample.value,
                    )
                )
    return output.getvalue().encode("utf-8")


def _write_new_file(directory_descriptor: int, name: str, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_descriptor)
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        try:
            os.unlink(name, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass
        raise


def write_metrics_artifact(
    directory: Path,
    artifact: MetricsExportArtifact,
) -> MetricsArtifactPaths:
    """JSON/CSV/checksumを専用directoryへ非上書きで一括永続化する。"""

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_descriptor = os.open(directory, directory_flags)
    except OSError as error:
        raise MetricsExportError("metrics_artifact_directory_invalid") from error
    try:
        status = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(status.st_mode)
            or stat.S_IMODE(status.st_mode) != 0o700
            or status.st_uid != os.geteuid()
            or status.st_gid != os.getegid()
        ):
            raise MetricsExportError("metrics_artifact_directory_invalid")
        try:
            existing_names = os.listdir(directory_descriptor)
        except OSError as error:
            raise MetricsExportError("metrics_artifact_directory_invalid") from error
        if existing_names:
            raise MetricsExportError("metrics_artifact_directory_not_empty")

        basename = f"metrics-{artifact.run_id}"
        json_name = f"{basename}.json"
        csv_name = f"{basename}.csv"
        checksum_name = f"{basename}.sha256"
        json_content = artifact.model_dump_json(indent=2).encode("utf-8") + b"\n"
        csv_content = _serialize_csv(artifact)
        checksum_content = (
            f"{hashlib.sha256(json_content).hexdigest()}  {json_name}\n"
            f"{hashlib.sha256(csv_content).hexdigest()}  {csv_name}\n"
        ).encode("ascii")
        created_names: list[str] = []
        try:
            for name, content in (
                (json_name, json_content),
                (csv_name, csv_content),
                (checksum_name, checksum_content),
            ):
                _write_new_file(directory_descriptor, name, content)
                created_names.append(name)
            os.fsync(directory_descriptor)
        except OSError as error:
            for created_name in created_names:
                try:
                    os.unlink(created_name, dir_fd=directory_descriptor)
                except FileNotFoundError:
                    pass
            try:
                os.fsync(directory_descriptor)
            except OSError:
                pass
            if isinstance(error, FileExistsError):
                raise MetricsExportError("metrics_artifact_already_exists") from error
            raise MetricsExportError("metrics_artifact_write_failed") from error
    finally:
        os.close(directory_descriptor)
    return MetricsArtifactPaths(
        json_path=directory / json_name,
        csv_path=directory / csv_name,
        checksum_path=directory / checksum_name,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="固定Prometheus queryを負荷試験artifactへ保存する")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--repository-root", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        manifest = load_export_manifest(arguments.manifest, arguments.repository_root)
        artifact = export_metrics(
            manifest,
            now=datetime.now(UTC),
        )
        outputs = write_metrics_artifact(METRICS_ARTIFACT_DIRECTORY, artifact)
    except MetricsExportError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(
        " ".join(path.name for path in (outputs.json_path, outputs.csv_path, outputs.checksum_path))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
