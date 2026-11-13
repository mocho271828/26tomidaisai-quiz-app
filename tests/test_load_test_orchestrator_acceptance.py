from __future__ import annotations

from datetime import timedelta

import pytest
from tests.test_load_test_manifest import _manifest

from quiz_app.load_test.controller_contracts import manifest_context
from quiz_app.load_test.metrics_export import (
    FIXED_QUERIES,
    ExportLabel,
    ExportQueryResult,
    ExportSample,
    ExportSeries,
    MetricsExportArtifact,
)
from quiz_app.load_test.orchestrator_acceptance import (
    HTTP_ROUTES,
    GeneratorMetrics,
    HttpCell,
    HttpOutcomes,
    SocketMetrics,
    SocketOutcomes,
    StaticFailures,
    evaluate_acceptance,
)
from quiz_app.load_test.orchestrator_contracts import OrchestratorError


def _metrics() -> GeneratorMetrics:
    manifest = _manifest()
    context = manifest_context(manifest)
    zero = HttpCell(request_count=0, request_rate_milli_per_second=0, duration_microseconds=None)
    outcomes = HttpOutcomes(
        client_failure=zero,
        contract_failure=zero,
        server_failure=zero,
        success=zero,
        transport_failure=zero,
    )
    static = StaticFailures(
        bundle_contract_invalid=0,
        index_request_failed=0,
        javascript_request_failed=0,
        stylesheet_request_failed=0,
    )
    return GeneratorMetrics(
        format="load-test-generator-metrics-summary-v3",
        run_id=manifest.run_id,
        event_id=manifest.event.id,
        source_commit=context.source_commit,
        schema_revision=context.schema_revision,
        manifest_sha256=context.manifest_sha256,
        scenario_sha256=context.scenario_sha256,
        runtime_manifest_sha256="b" * 64,
        generated_at=manifest.scheduled_start_at + timedelta(seconds=4800),
        test_run_duration_ms=4_800_000,
        http=dict.fromkeys(HTTP_ROUTES, outcomes),
        static_asset_failures={"cold": static, "warm": static},
        websocket=SocketMetrics(
            connections={
                "initial": SocketOutcomes(failed=0, opened=600),
                "reconnect": SocketOutcomes(failed=0, opened=0),
            },
            messages={"invalid": 0, "polling_required": 0, "state_invalidated": 0},
            closes={"1000": 600, "1003": 0, "1008": 0, "1013": 0, "4401": 0, "other": 0},
            error_count=0,
        ),
        protocol_refusals={"payload": 0, "realtime": 0, "runtime": 0, "session": 0},
    )


def _export(
    *, missing_second: int | None = None, wrong_value: float = 599
) -> MetricsExportArtifact:
    manifest = _manifest()
    start = manifest.scheduled_start_at.timestamp()
    samples = tuple(
        ExportSample(
            timestamp=start + second,
            value=(
                wrong_value
                if second == missing_second
                else 0
                if 2280 <= second < 2700 or 3000 <= second < 3420
                else 600
            ),
        )
        for second in range(900, 4500)
    )
    websocket = ExportSeries(
        labels=(
            ExportLabel(name="audience", value="participant"),
            ExportLabel(name="instance", value="api-primary"),
        ),
        samples=samples,
    )
    return MetricsExportArtifact(
        format="load-test-prometheus-export-v1",
        run_id=manifest.run_id,
        source_commit=manifest.artifacts.source_commit,
        generated_at=manifest.scheduled_start_at + timedelta(seconds=4801),
        started_at=manifest.scheduled_start_at,
        ended_at=manifest.scheduled_start_at + timedelta(seconds=4800),
        step_seconds=1,
        results=tuple(
            ExportQueryResult(
                query_id=query.query_id,
                series=(websocket,) if query.query_id == "websocket_active" else (),
            )
            for query in FIXED_QUERIES
        ),
    )


def test_normal_websocket_threshold_excludes_only_declared_fault_and_recovery_windows() -> None:
    report = evaluate_acceptance(_manifest(), _metrics(), _export())
    assert report.decision == "PASS"
    assert report.normal_websocket_missing_seconds == 0
    assert report.latency_mode == "first-run-calibration-only"


@pytest.mark.parametrize("second", [900, 2279, 2700, 2999, 3420, 4499])
def test_missing_normal_second_fails_fixed_600_connection_requirement(second: int) -> None:
    report = evaluate_acceptance(_manifest(), _metrics(), _export(missing_second=second))
    assert report.decision == "FAIL"
    assert report.normal_websocket_missing_seconds == 1


def test_generator_5xx_uses_existing_manifest_zero_threshold() -> None:
    metrics = _metrics()
    failed = metrics.http["join"].model_copy(
        update={
            "server_failure": HttpCell(
                request_count=1, request_rate_milli_per_second=1, duration_microseconds=None
            )
        }
    )
    metrics = metrics.model_copy(update={"http": {**metrics.http, "join": failed}})
    report = evaluate_acceptance(_manifest(), metrics, _export())
    assert report.decision == "FAIL"
    assert report.failure_codes == ("generator_http_5xx",)


def test_asset_failure_not_given_unapproved_zero_threshold() -> None:
    metrics = _metrics()
    cold = metrics.static_asset_failures["cold"].model_copy(update={"stylesheet_request_failed": 1})
    metrics = metrics.model_copy(
        update={"static_asset_failures": {**metrics.static_asset_failures, "cold": cold}}
    )
    assert evaluate_acceptance(_manifest(), metrics, _export()).decision == "PASS"


def test_invalid_metric_number_is_never_accepted() -> None:
    with pytest.raises(OrchestratorError, match="sample_invalid"):
        evaluate_acceptance(
            _manifest(), _metrics(), _export(missing_second=900, wrong_value=float("nan"))
        )


def test_different_manifest_metrics_are_rejected() -> None:
    metrics = _metrics().model_copy(update={"manifest_sha256": "0" * 64})
    with pytest.raises(OrchestratorError, match="binding_invalid"):
        evaluate_acceptance(_manifest(), metrics, _export())


def test_short_generator_run_cannot_pass_even_with_all_ten_rounds() -> None:
    metrics = _metrics().model_copy(update={"test_run_duration_ms": 4_799_999})
    with pytest.raises(OrchestratorError, match="binding_invalid"):
        evaluate_acceptance(_manifest(), metrics, _export())
