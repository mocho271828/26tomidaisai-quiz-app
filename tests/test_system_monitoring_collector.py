from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import httpx
import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError

from quiz_app.system_monitoring import collector, diagnostics, probes
from quiz_app.system_monitoring.collector import Collector
from quiz_app.system_monitoring.collector_config import CollectorConfig, load_collector_config
from quiz_app.system_monitoring.probes import CounterReading, parse_counter, probe_api, request_rate

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
CONFIG = CollectorConfig(
    environment_id="gs-rehearsal-test",
    expires_at=NOW + timedelta(hours=6),
)
METRICS = b"""# TYPE process_start_time_seconds gauge
process_start_time_seconds 100
# TYPE quiz_http_requests_total counter
quiz_http_requests_total{method="POST",route="/api/v1/participant/join",status_class="2xx"} 120
quiz_http_requests_total{method="GET",route="/api/health/live",status_class="2xx"} 999
quiz_http_requests_total{method="GET",route="/api/health/ready",status_class="2xx"} 50
quiz_http_requests_total{method="GET",route="/internal/metrics",status_class="2xx"} 50
"""


def test_counter_excludes_monitoring_requests_and_requires_process_identity() -> None:
    reading = parse_counter(METRICS, NOW)
    assert reading.requests == 120
    assert reading.process_started_at == 100
    with pytest.raises(ValueError):
        parse_counter(b"quiz_http_requests_total 3", NOW)


@pytest.mark.parametrize(
    "seconds,count,start,expected",
    [
        (60, 150.0, 100.0, 30.0),
        (90, 180.0, 100.0, 40.0),
        (60, 150.0, 200.0, None),
        (60, 10.0, 100.0, None),
        (120, 180.0, 100.0, None),
        (0, 150.0, 100.0, None),
    ],
)
def test_counter_reset_gaps_and_rate_units(
    seconds: int,
    count: float,
    start: float,
    expected: float | None,
) -> None:
    old = parse_counter(METRICS, NOW)
    new = CounterReading(
        sampled_at=NOW + timedelta(seconds=seconds), process_started_at=start, requests=count
    )
    assert request_rate(old, new) == expected
    assert request_rate(None, new) is None
    assert request_rate(old, None) is None


def healthy_response(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/live"):
        return httpx.Response(200, json={"status": "ok", "service": "quiz-api", "version": "test"})
    if request.url.path.endswith("/ready"):
        return httpx.Response(
            200, json={"status": "ready", "database": "ok", "migration": "current", "redis": "ok"}
        )
    return httpx.Response(200, content=METRICS)


def test_probe_validates_readiness_and_does_not_follow_redirects() -> None:
    with httpx.Client(transport=httpx.MockTransport(healthy_response)) as client:
        result = probe_api(
            client, "api", probes.PROBE_ORIGINS[0][1], NOW, expires_at=CONFIG.expires_at
        )
    assert result.key == "api"
    assert result.live and result.ready
    assert result.counter is not None
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(302, headers={"Location": "https://external.invalid"})
        )
    ) as client:
        result = probe_api(
            client, "api", probes.PROBE_ORIGINS[0][1], NOW, expires_at=CONFIG.expires_at
        )
        assert not result.live and not result.ready
        assert result.counter is None
        with pytest.raises(ValueError):
            probe_api(client, "api", "http://169.254.169.254", NOW, expires_at=CONFIG.expires_at)


def test_probe_preserves_live_signal_when_readiness_is_degraded() -> None:
    def degraded_response(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ready"):
            return httpx.Response(
                200,
                json={
                    "status": "degraded",
                    "database": "ok",
                    "migration": "current",
                    "redis": "degraded",
                },
            )
        return healthy_response(request)

    with httpx.Client(transport=httpx.MockTransport(degraded_response)) as client:
        result = probe_api(
            client, "api", probes.PROBE_ORIGINS[0][1], NOW, expires_at=CONFIG.expires_at
        )
    assert result.live and not result.ready
    assert result.counter is not None


def test_probe_slow_trickle_has_total_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter((0.0, 3.0))
    monkeypatch.setattr(probes, "monotonic", lambda: next(clock))
    with httpx.Client(transport=httpx.MockTransport(healthy_response)) as client:
        result = probe_api(
            client, "api", probes.PROBE_ORIGINS[0][1], NOW, expires_at=CONFIG.expires_at
        )
    assert not result.live and not result.ready


def test_probe_refuses_oversized_body() -> None:
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 4097))
    ) as client:
        result = probe_api(
            client, "api", probes.PROBE_ORIGINS[0][1], NOW, expires_at=CONFIG.expires_at
        )
    assert not result.live and not result.ready


def test_collector_publishes_five_dashboard_series_without_aws_reads() -> None:
    aws = Mock()
    handler = Mock(side_effect=healthy_response)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        worker = Collector(CONFIG, aws, client, clock=lambda: NOW)
        assert worker.collect(NOW)
        first = aws.put_metric_data.call_args.kwargs
        assert first["Namespace"] == "GSQuiz/SystemHealth"
        assert len(first["MetricData"]) == 4
        assert {
            (datum["Dimensions"][1]["Value"], datum["Dimensions"][2]["Value"])
            for datum in first["MetricData"]
        } == {(api, health) for api in ("api", "api_secondary") for health in ("live", "ready")}
        assert all(datum["MetricName"] == "ApiHealth" for datum in first["MetricData"])
        assert all(datum["Value"] == 1 for datum in first["MetricData"])
        assert handler.call_count == 6
        assert worker.collect(NOW + timedelta(seconds=30))
        assert handler.call_count == 6
        assert aws.put_metric_data.call_count == 1
        assert worker.collect(NOW + timedelta(seconds=60))
        assert handler.call_count == 12
        assert aws.put_metric_data.call_count == 2
        published = aws.put_metric_data.call_args.kwargs
        assert len(published["MetricData"]) == 5
        assert published["MetricData"][-1] == {
            "MetricName": "ApiRequestsPerMinute",
            "Dimensions": [{"Name": "Environment", "Value": CONFIG.environment_id}],
            "Timestamp": NOW + timedelta(seconds=60),
            "Value": 0,
            "Unit": "None",
        }
        assert {call[0] for call in aws.method_calls} == {"put_metric_data"}


def test_missing_counter_does_not_publish_a_partial_request_rate() -> None:
    def missing_secondary_counter(request: httpx.Request) -> httpx.Response:
        if request.url.port == 18002 and request.url.path == "/internal/metrics":
            return httpx.Response(503)
        return healthy_response(request)

    aws = Mock()
    with httpx.Client(transport=httpx.MockTransport(missing_secondary_counter)) as client:
        worker = Collector(CONFIG, aws, client, clock=lambda: NOW)
        assert worker.collect(NOW)
        assert worker.collect(NOW + timedelta(seconds=60))
    assert len(aws.put_metric_data.call_args.kwargs["MetricData"]) == 4


def test_failed_publish_waits_until_next_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    aws = Mock()
    aws.put_metric_data.side_effect = ClientError(
        {"Error": {"Code": "Throttling", "Message": "sensitive-aws-response"}}, "PutMetricData"
    )
    failure = Mock()
    monkeypatch.setattr(collector, "record_failure", failure)
    with httpx.Client(transport=httpx.MockTransport(healthy_response)) as client:
        worker = Collector(CONFIG, aws, client, clock=lambda: NOW)
        assert worker.collect(NOW)
        assert worker.collect(NOW + timedelta(seconds=30))
        assert aws.put_metric_data.call_count == 1
        assert worker.collect(NOW + timedelta(seconds=60))
    assert aws.put_metric_data.call_count == 2
    assert failure.call_count == 2
    assert failure.call_args.args[0] == "cloudwatch_write"


def test_expiry_after_probe_prevents_new_aws_io() -> None:
    aws = Mock()
    clock = iter((NOW, CONFIG.expires_at))
    with httpx.Client(transport=httpx.MockTransport(healthy_response)) as client:
        worker = Collector(CONFIG, aws, client, clock=lambda: next(clock))
        assert not worker.collect(NOW)
    assert not aws.method_calls


def test_expired_collector_does_not_start_probes() -> None:
    aws, handler = Mock(), Mock(side_effect=healthy_response)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        worker = Collector(CONFIG, aws, client, clock=lambda: CONFIG.expires_at)
        assert not worker.collect(CONFIG.expires_at)
    handler.assert_not_called()
    assert not aws.method_calls


def test_expired_run_does_not_create_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = Mock()
    monkeypatch.setattr(collector, "create_client", factory)
    expired = CONFIG.model_copy(update={"expires_at": datetime(2000, 1, 1, tzinfo=UTC)})
    assert collector.run(expired, Event()) == 0
    factory.assert_not_called()


def test_collector_config_rejects_naive_expiry_unknown_fields_and_symlink(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        CollectorConfig.model_validate({**CONFIG.model_dump(), "expires_at": datetime(2030, 1, 1)})
    for retired_or_unknown in ("instance_id", "cpu_period_seconds", "endpoint_url"):
        with pytest.raises(ValidationError):
            CollectorConfig.model_validate({**CONFIG.model_dump(), retired_or_unknown: "unused"})
    path = tmp_path / "config.json"
    path.write_text(CONFIG.model_dump_json())
    path.chmod(0o640)
    assert load_collector_config(path) == CONFIG
    alias = tmp_path / "alias.json"
    alias.symlink_to(path)
    with pytest.raises(ValueError):
        load_collector_config(alias)


def test_failure_logging_never_contains_exception_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    error_log = Mock()
    monkeypatch.setattr(diagnostics.logfire, "error", error_log)
    try:
        raise ValueError("SECRET-CREDENTIAL")
    except ValueError as error:
        diagnostics.record_failure("cloudwatch_write", error)
    assert "SECRET-CREDENTIAL" not in str(error_log.call_args)
    assert error_log.call_args.kwargs["_exc_info"] is False
