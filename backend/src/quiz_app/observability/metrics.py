"""Application-owned Prometheus registry and bounded metric instruments."""

from __future__ import annotations

from collections.abc import Iterator
from threading import Lock
from typing import Literal, Protocol

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    ProcessCollector,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily

from quiz_app.observability.request_timing import (
    AnswerDifferenceBand,
    DatabaseWorkKind,
    TimingStage,
)

HTTP_DURATION_BUCKETS_SECONDS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)
DatabasePoolRole = Literal["sync", "async"]
RealtimeMetricAudience = Literal["participant", "operator"]
RealtimeDeliveryKind = Literal["state", "personal_result", "heartbeat", "control"]
RealtimeDeliveryOutcome = Literal["sent", "timeout", "coalesced", "discarded"]
RealtimeBuildKind = Literal["common", "personal_result"]
RealtimeBuildOutcome = Literal["success", "error", "timeout", "deferred", "discarded"]


class DatabasePoolReader(Protocol):
    """Subset of SQLAlchemy pool state exposed as aggregate gauges."""

    def checkedin(self) -> int: ...

    def checkedout(self) -> int: ...

    def overflow(self) -> int: ...

    def size(self) -> int: ...


class DatabasePoolCollector:
    """Read current pool aggregates without retaining connection identities."""

    def __init__(self) -> None:
        self._pools: dict[DatabasePoolRole, DatabasePoolReader] = {}
        self._lock = Lock()

    def add_pool(self, role: DatabasePoolRole, pool: DatabasePoolReader) -> None:
        with self._lock:
            self._pools[role] = pool

    def describe(self) -> Iterator[GaugeMetricFamily]:
        yield GaugeMetricFamily(
            "quiz_db_pool_connections",
            "Current SQLAlchemy database pool aggregate state.",
            labels=["state"],
        )
        yield GaugeMetricFamily(
            "quiz_db_pool_metrics_supported",
            "Whether all expected SQLAlchemy pool aggregate readers are available.",
        )
        yield GaugeMetricFamily(
            "quiz_db_pool_role_connections",
            "Database pool state separated by fixed execution role.",
            labels=["role", "state"],
        )

    def collect(self) -> Iterator[GaugeMetricFamily]:
        metric = GaugeMetricFamily(
            "quiz_db_pool_connections",
            "Current SQLAlchemy database pool aggregate state.",
            labels=["state"],
        )
        reader_names = (
            ("checked_in", "checkedin"),
            ("checked_out", "checkedout"),
            ("overflow", "overflow"),
            ("size", "size"),
        )
        role_metric = GaugeMetricFamily(
            "quiz_db_pool_role_connections",
            "Database pool state separated by fixed execution role.",
            labels=["role", "state"],
        )
        totals = {state: 0.0 for state, _ in reader_names}
        seen_states: set[str] = set()
        with self._lock:
            pools = self._pools.copy()
        is_supported = bool(pools)
        for role, pool in pools.items():
            for state, reader_name in reader_names:
                try:
                    reader = getattr(pool, reader_name)
                    if not callable(reader):
                        raise TypeError("database pool aggregate reader is not callable")
                    value = max(0.0, float(reader()))
                except (AttributeError, OverflowError, RuntimeError, TypeError, ValueError):
                    is_supported = False
                    continue
                totals[state] += value
                seen_states.add(state)
                role_metric.add_metric([role, state], value)
        for state, total in totals.items():
            if state in seen_states:
                metric.add_metric([state], total)
        yield metric
        yield role_metric
        supported_metric = GaugeMetricFamily(
            "quiz_db_pool_metrics_supported",
            "Whether all expected SQLAlchemy pool aggregate readers are available.",
        )
        supported_metric.add_metric([], 1 if is_supported else 0)
        yield supported_metric


class QuizMetrics:
    """Own all application collectors so tests and app instances stay isolated."""

    content_type = CONTENT_TYPE_LATEST

    def __init__(self) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        self.process_collector = ProcessCollector(registry=self.registry)
        self._http_requests = Counter(
            "quiz_http_requests_total",
            "Completed HTTP requests.",
            ("method", "route", "status_class"),
            registry=self.registry,
        )
        self._http_duration = Histogram(
            "quiz_http_request_duration_seconds",
            "HTTP request duration in seconds.",
            ("method", "route", "status_class"),
            buckets=HTTP_DURATION_BUCKETS_SECONDS,
            registry=self.registry,
        )
        self._http_in_progress = Gauge(
            "quiz_http_requests_in_progress",
            "HTTP requests currently being served.",
            ("method", "route"),
            registry=self.registry,
        )
        self._participant_request_stage = Histogram(
            "quiz_participant_request_stage_duration_seconds",
            "Participant request server stages; database and transaction stages overlap handler.",
            ("method", "route", "stage"),
            buckets=HTTP_DURATION_BUCKETS_SECONDS,
            registry=self.registry,
        )
        self._answer_difference = Counter(
            "quiz_answer_receipt_difference_total",
            "New answer validation attempts by fixed receipt-minus-client time band.",
            ["band"],
            registry=self.registry,
        )
        self._websocket_attempts = Counter(
            "quiz_websocket_attempts_total",
            "WebSocket connection attempts.",
            ("audience",),
            registry=self.registry,
        )
        self._websocket_active = Gauge(
            "quiz_websocket_active",
            "Accepted WebSocket connections currently active.",
            ("audience",),
            registry=self.registry,
        )
        self._websocket_closes = Counter(
            "quiz_websocket_closes_total",
            "WebSocket connection terminations.",
            ("audience", "outcome", "code_class"),
            registry=self.registry,
        )
        self._polling_fallbacks = Counter(
            "quiz_realtime_polling_fallback_total",
            "Decisions to fall back from Redis realtime delivery to polling.",
            ("audience", "reason"),
            registry=self.registry,
        )
        self._redis_publish = Counter(
            "quiz_redis_publish_total",
            "Redis realtime publish results.",
            ("audience", "result"),
            registry=self.registry,
        )
        self._redis_dependency_up = Gauge(
            "quiz_redis_dependency_up",
            "Whether the latest Redis dependency health probe succeeded.",
            registry=self.registry,
        )
        self._pool_registration_lock = Lock()
        self._database_pool_collector: DatabasePoolCollector | None = None
        self._participant_database_work = Histogram(
            "quiz_participant_database_work",
            "Database operations per participant request, "
            "excluding transaction control and pre-ping.",
            ["method", "route", "kind"],
            buckets=(0, 1, 2, 3, 5, 8, 13, 21, 34, 55),
            registry=self.registry,
        )
        self.event_loop_delay = Histogram(
            "quiz_event_loop_delay_seconds",
            "Monotonic scheduler delay.",
            buckets=HTTP_DURATION_BUCKETS_SECONDS,
            registry=self.registry,
        )
        self.thread_tokens = Gauge(
            "quiz_thread_pool_tokens",
            "AnyIO thread capacity limiter state.",
            ("state",),
            registry=self.registry,
        )
        self.database_acquisition = Histogram(
            "quiz_db_connection_acquisition_seconds",
            "Pool acquisition including queue wait, connect, TLS and pre-ping.",
            ("result",),
            buckets=HTTP_DURATION_BUCKETS_SECONDS,
            registry=self.registry,
        )
        self._redis_subscriptions = Gauge(
            "quiz_redis_subscriptions",
            "Active shared Redis subscriptions.",
            ("audience",),
            registry=self.registry,
        )
        self._redis_coalesced = Counter(
            "quiz_redis_delivery_coalesced_total",
            "Coalesced slow-subscriber notifications.",
            ("audience",),
            registry=self.registry,
        )
        self._redis_cleanup = Counter(
            "quiz_redis_subscription_cleanup_total",
            "Redis subscription cleanup results.",
            ("result",),
            registry=self.registry,
        )
        self._tls_context = Histogram(
            "quiz_redis_tls_context_initialization_seconds",
            "Verified Redis TLS context setup.",
            buckets=HTTP_DURATION_BUCKETS_SECONDS,
            registry=self.registry,
        )
        self._realtime_delivery = Counter(
            "quiz_realtime_delivery_total",
            "Per-connection realtime delivery outcomes; not distinct participant counts.",
            ("audience", "kind", "outcome"),
            registry=self.registry,
        )
        self._realtime_delivery_duration = Histogram(
            "quiz_realtime_delivery_duration_seconds",
            "Per-connection transport duration, excluding shared construction.",
            ("audience", "kind", "outcome"),
            buckets=HTTP_DURATION_BUCKETS_SECONDS,
            registry=self.registry,
        )
        self._realtime_build = Counter(
            "quiz_realtime_build_total",
            "Shared realtime construction outcomes.",
            ("audience", "kind", "outcome"),
            registry=self.registry,
        )
        self._realtime_build_duration = Histogram(
            "quiz_realtime_build_duration_seconds",
            "Shared realtime construction duration including database acquisition.",
            ("audience", "kind", "outcome"),
            buckets=HTTP_DURATION_BUCKETS_SECONDS,
            registry=self.registry,
        )
        self._realtime_redis_degraded = Gauge(
            "quiz_realtime_redis_degraded",
            "Whether realtime uses shared database reconciliation after Redis failure.",
            ("audience",),
            registry=self.registry,
        )

    def record_realtime_delivery(
        self,
        *,
        audience: RealtimeMetricAudience,
        kind: RealtimeDeliveryKind,
        outcome: RealtimeDeliveryOutcome,
        duration_seconds: float | None = None,
    ) -> None:
        """送信の試行を固定分類で記録し、再接続を人数として数えない。"""
        labels = {"audience": audience, "kind": kind, "outcome": outcome}
        self._realtime_delivery.labels(**labels).inc()
        if duration_seconds is not None:
            self._realtime_delivery_duration.labels(**labels).observe(duration_seconds)

    def record_realtime_build(
        self,
        *,
        audience: RealtimeMetricAudience,
        kind: RealtimeBuildKind,
        outcome: RealtimeBuildOutcome,
        duration_seconds: float | None = None,
    ) -> None:
        """共有構築と延期を分け、未実施の構築時間を0秒へ補完しない。"""
        labels = {"audience": audience, "kind": kind, "outcome": outcome}
        self._realtime_build.labels(**labels).inc()
        if duration_seconds is not None:
            self._realtime_build_duration.labels(**labels).observe(duration_seconds)

    def set_realtime_redis_degraded(
        self, *, audience: RealtimeMetricAudience, is_degraded: bool
    ) -> None:
        self._realtime_redis_degraded.labels(audience=audience).set(int(is_degraded))

    def change_redis_subscriptions(self, audience: str, delta: int) -> None:
        self._redis_subscriptions.labels(audience=audience).inc(delta)

    def record_redis_delivery_coalesced(self, audience: str) -> None:
        self._redis_coalesced.labels(audience=audience).inc()

    def record_tls_context_initialization(self, duration_seconds: float) -> None:
        self._tls_context.observe(duration_seconds)

    def record_redis_subscription_cleanup(self, succeeded: bool) -> None:
        self._redis_cleanup.labels(result="success" if succeeded else "error").inc()

    def begin_http_request(self, method: str) -> None:
        self._http_in_progress.labels(method=method, route="unmatched").inc()

    def record_answer_difference(self, band: AnswerDifferenceBand) -> None:
        self._answer_difference.labels(band=band).inc()

    def record_participant_request_stage(
        self, *, method: str, route: str, stage: TimingStage, duration_seconds: float
    ) -> None:
        self._participant_request_stage.labels(method=method, route=route, stage=stage).observe(
            duration_seconds
        )

    def record_participant_database_work(
        self, *, method: str, route: str, counts: dict[DatabaseWorkKind, int]
    ) -> None:
        kinds: tuple[DatabaseWorkKind, ...] = (
            "statements",
            "acquisitions",
            "auth_cache_hits",
            "sessions",
        )
        for kind in kinds:
            self._participant_database_work.labels(method=method, route=route, kind=kind).observe(
                counts.get(kind, 0)
            )

    def finish_http_request(
        self,
        *,
        method: str,
        route: str,
        status_class: str,
        duration_seconds: float,
    ) -> None:
        self._http_in_progress.labels(method=method, route="unmatched").dec()
        labels = {"method": method, "route": route, "status_class": status_class}
        self._http_requests.labels(**labels).inc()
        self._http_duration.labels(**labels).observe(duration_seconds)

    def record_websocket_attempt(self, audience: str) -> None:
        self._websocket_attempts.labels(audience=audience).inc()

    def change_websocket_active(self, *, audience: str, delta: int) -> None:
        self._websocket_active.labels(audience=audience).inc(delta)

    def record_websocket_close(
        self,
        *,
        audience: str,
        outcome: str,
        code_class: str,
    ) -> None:
        self._websocket_closes.labels(
            audience=audience,
            outcome=outcome,
            code_class=code_class,
        ).inc()

    def record_polling_fallback(self, *, audience: str) -> None:
        self._polling_fallbacks.labels(
            audience=audience,
            reason="realtime_unavailable",
        ).inc()

    def record_redis_publish(self, *, audience: str, succeeded: bool) -> None:
        self._redis_publish.labels(
            audience=audience,
            result="success" if succeeded else "error",
        ).inc()

    def set_redis_dependency_up(self, is_up: bool) -> None:
        self._redis_dependency_up.set(1 if is_up else 0)

    def register_database_pool(
        self, pool: DatabasePoolReader, *, role: DatabasePoolRole = "sync"
    ) -> None:
        """Register one aggregate pool collector per application registry."""

        with self._pool_registration_lock:
            if self._database_pool_collector is None:
                self._database_pool_collector = DatabasePoolCollector()
                self.registry.register(self._database_pool_collector)
            self._database_pool_collector.add_pool(role, pool)
            if hasattr(pool, "quiz_metrics"):
                pool.quiz_metrics = self

    def render(self) -> bytes:
        return generate_latest(self.registry)
