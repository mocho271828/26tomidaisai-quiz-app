"""固定loopback経路の死活と非識別counterを、API要求処理から独立して採取する。"""

import math
from datetime import UTC, datetime
from time import monotonic
from typing import Literal

import httpx
from prometheus_client.parser import text_string_to_metric_families
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from quiz_app.contracts import LiveHealthResponse, ReadinessHealthResponse
from quiz_app.system_monitoring.diagnostics import record_failure

ApiKey = Literal["api", "api_secondary"]
PROBE_ORIGINS: tuple[tuple[ApiKey, str], ...] = (
    ("api", "http://127.0.0.1:18001"),
    ("api_secondary", "http://127.0.0.1:18002"),
)
EXCLUDED_ROUTES = frozenset(
    {
        "/api/health/live",
        "/api/health/ready",
        "/internal/metrics",
    }
)


class CounterReading(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, hide_input_in_errors=True)
    sampled_at: AwareDatetime
    process_started_at: float = Field(ge=0, allow_inf_nan=False)
    requests: float = Field(ge=0, allow_inf_nan=False)


class ProbeResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    key: ApiKey
    live: bool
    ready: bool
    counter: CounterReading | None


class HttpSample(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    method: str = Field(max_length=10)
    route: str = Field(max_length=160)
    status_class: str = Field(max_length=10)
    value: float = Field(ge=0, allow_inf_nan=False)


def _body(
    client: httpx.Client,
    url: str,
    limit: int,
    expires_at: datetime,
) -> tuple[int, bytes]:
    if datetime.now(UTC) >= expires_at:
        raise TimeoutError("monitoring_expired")
    deadline = monotonic() + 2
    with client.stream("GET", url) as response:
        data = bytearray()
        for part in response.iter_bytes():
            if monotonic() >= deadline or datetime.now(UTC) >= expires_at:
                raise TimeoutError("monitoring_probe_deadline")
            data.extend(part)
            if len(data) > limit:
                raise ValueError("monitoring_probe_body_too_large")
        return response.status_code, bytes(data)


def parse_counter(body: bytes, sampled_at: datetime) -> CounterReading:
    """Prometheus既存parserを使い、必要な型と固定の集計だけを残す。"""
    total = 0.0
    started_at: float | None = None
    seen: set[tuple[str, str, str]] = set()
    for family in text_string_to_metric_families(body.decode("utf-8")):
        for sample in family.samples:
            if sample.name == "process_start_time_seconds":
                if started_at is not None or not math.isfinite(sample.value):
                    raise ValueError("monitoring_process_start_invalid")
                started_at = sample.value
            elif sample.name == "quiz_http_requests_total":
                parsed = HttpSample.model_validate({**sample.labels, "value": sample.value})
                identity = (parsed.method, parsed.route, parsed.status_class)
                if identity in seen or len(seen) >= 1024:
                    raise ValueError("monitoring_http_series_invalid")
                seen.add(identity)
                if parsed.route not in EXCLUDED_ROUTES:
                    total += parsed.value
    if started_at is None:
        raise ValueError("monitoring_process_start_missing")
    return CounterReading(sampled_at=sampled_at, process_started_at=started_at, requests=total)


def probe_api(
    client: httpx.Client,
    key: ApiKey,
    origin: str,
    now: datetime,
    *,
    expires_at: datetime,
) -> ProbeResult:
    """固定経路の死活とcounterを採取し、例外本文は外部へ出さない。"""
    if (key, origin) not in PROBE_ORIGINS:
        raise ValueError("monitoring_probe_origin_invalid")
    live = False
    ready = False
    counter: CounterReading | None = None
    try:
        code, body = _body(client, origin + "/api/health/live", 4096, expires_at)
        live = code == 200 and LiveHealthResponse.model_validate_json(body).status == "ok"
    except (httpx.HTTPError, ValueError, TimeoutError) as error:
        record_failure("probe", error)
    if live:
        try:
            code, body = _body(client, origin + "/api/health/ready", 4096, expires_at)
            response = ReadinessHealthResponse.model_validate_json(body)
            database_ready = response.database == "ok" and response.migration == "current"
            ready = (
                code == 200
                and response.status == "ready"
                and response.redis == "ok"
                and database_ready
            )
        except (httpx.HTTPError, ValueError, TimeoutError) as error:
            record_failure("probe", error)
        try:
            code, body = _body(client, origin + "/internal/metrics", 1_048_576, expires_at)
            if code != 200:
                raise ValueError("monitoring_counter_unavailable")
            counter = parse_counter(body, now)
        except (httpx.HTTPError, ValueError, TimeoutError) as error:
            record_failure("probe", error)
    return ProbeResult(
        key=key,
        live=live,
        ready=ready,
        counter=counter,
    )


def request_rate(previous: CounterReading | None, current: CounterReading | None) -> float | None:
    """連続した同一processの区間だけを1分換算し、reset/欠測は数値にしない。"""
    if previous is None or current is None:
        return None
    seconds = (current.sampled_at - previous.sampled_at).total_seconds()
    if (
        current.process_started_at != previous.process_started_at
        or not 45 <= seconds <= 90
        or current.requests < previous.requests
    ):
        return None
    return (current.requests - previous.requests) * 60 / seconds
