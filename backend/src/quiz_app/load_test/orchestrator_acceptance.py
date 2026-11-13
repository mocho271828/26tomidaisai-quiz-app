from __future__ import annotations

import math
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from quiz_app.load_test.controller_contracts import manifest_context
from quiz_app.load_test.db_verifier_artifacts import (
    read_secure_external_file,
    write_canonical_artifact,
)
from quiz_app.load_test.fault_controller_contracts import FaultControllerStatus
from quiz_app.load_test.manifest import LoadTestManifest
from quiz_app.load_test.metrics_export import FIXED_QUERIES, MetricsExportArtifact
from quiz_app.load_test.orchestrator_contracts import (
    MAX_METRICS_ARTIFACT_BYTES,
    OrchestratorError,
    StrictRunModel,
)
from quiz_app.load_test.orchestrator_fault_evidence import validate_fault_evidence
from quiz_app.load_test.verification_contracts import ArtifactIdentity, Sha256

Count = Annotated[int, Field(ge=0, le=100_000_000)]
Duration = Annotated[int, Field(ge=0, le=120_000_000)]
HTTP_ROUTES = frozenset(
    {
        "answer_status",
        "app_index",
        "app_javascript",
        "app_stylesheet",
        "heartbeat",
        "join",
        "participant_session",
        "readiness",
        "server_time",
        "submit_answer",
    }
)


class HttpDuration(StrictRunModel):
    minimum_microseconds: Duration
    mean_microseconds: Duration
    p50_microseconds: Duration
    p95_microseconds: Duration
    p99_microseconds: Duration
    maximum_microseconds: Duration


class HttpCell(StrictRunModel):
    request_count: Count
    request_rate_milli_per_second: Annotated[int, Field(ge=0)]
    duration_microseconds: HttpDuration | None


class HttpOutcomes(StrictRunModel):
    client_failure: HttpCell
    contract_failure: HttpCell
    server_failure: HttpCell
    success: HttpCell
    transport_failure: HttpCell


class StaticFailures(StrictRunModel):
    bundle_contract_invalid: Count
    index_request_failed: Count
    javascript_request_failed: Count
    stylesheet_request_failed: Count


class SocketOutcomes(StrictRunModel):
    failed: Count
    opened: Count


class SocketMetrics(StrictRunModel):
    connections: dict[Literal["initial", "reconnect"], SocketOutcomes]
    messages: dict[Literal["invalid", "polling_required", "state_invalidated"], Count]
    closes: dict[Literal["1000", "1003", "1008", "1013", "4401", "other"], Count]
    error_count: Count


class GeneratorMetrics(StrictRunModel):
    format: Literal["load-test-generator-metrics-summary-v3"]
    run_id: UUID
    event_id: UUID
    source_commit: str
    schema_revision: str
    manifest_sha256: Sha256
    scenario_sha256: Sha256
    runtime_manifest_sha256: Sha256
    generated_at: datetime
    test_run_duration_ms: Annotated[int, Field(ge=0, le=86_400_000)]
    http: dict[str, HttpOutcomes]
    static_asset_failures: dict[Literal["cold", "warm"], StaticFailures]
    websocket: SocketMetrics
    protocol_refusals: dict[Literal["payload", "realtime", "runtime", "session"], Count]

    @model_validator(mode="after")
    def require_cardinality(self) -> GeneratorMetrics:
        if (
            set(self.http) != HTTP_ROUTES
            or set(self.static_asset_failures) != {"cold", "warm"}
            or set(self.websocket.connections) != {"initial", "reconnect"}
            or set(self.websocket.messages) != {"invalid", "polling_required", "state_invalidated"}
            or set(self.websocket.closes) != {"1000", "1003", "1008", "1013", "4401", "other"}
            or set(self.protocol_refusals) != {"payload", "realtime", "runtime", "session"}
        ):
            raise ValueError("orchestrator_metrics_cardinality_invalid")
        return self


class AcceptanceReport(StrictRunModel):
    format: Literal["load-test-acceptance-v1"] = "load-test-acceptance-v1"
    run_id: UUID
    manifest_sha256: Sha256
    decision: Literal["PASS", "FAIL"]
    failure_codes: tuple[str, ...]
    generator_server_failures: Count
    normal_websocket_missing_seconds: Count
    latency_mode: Literal["first-run-calibration-only"] = "first-run-calibration-only"


def evaluate_acceptance(
    manifest: LoadTestManifest, metrics: GeneratorMetrics, exported: MetricsExportArtifact
) -> AcceptanceReport:
    context = manifest_context(manifest)
    if (
        (
            metrics.run_id,
            metrics.event_id,
            metrics.source_commit,
            metrics.schema_revision,
            metrics.manifest_sha256,
            metrics.scenario_sha256,
        )
        != (
            context.run_id,
            context.event_id,
            context.source_commit,
            context.schema_revision,
            context.manifest_sha256,
            context.scenario_sha256,
        )
        or exported.run_id != context.run_id
        or exported.source_commit != context.source_commit
        or exported.started_at != manifest.scheduled_start_at
        or exported.ended_at != manifest.scheduled_start_at + timedelta(seconds=4800)
        or metrics.test_run_duration_ms < manifest.scenario.lifecycle.ramp_down_end_seconds * 1000
        or len(exported.results) != len(FIXED_QUERIES)
        or {result.query_id for result in exported.results}
        != {query.query_id for query in FIXED_QUERIES}
    ):
        raise OrchestratorError("orchestrator_metrics_binding_invalid")
    failures: list[str] = []
    server_failures = sum(route.server_failure.request_count for route in metrics.http.values())
    if server_failures > manifest.scenario.thresholds.http_5xx_maximum:
        failures.append("generator_http_5xx")
    websocket_by_second: dict[int, float] = {}
    start = manifest.scheduled_start_at.timestamp()
    for result in exported.results:
        for series in result.series:
            labels = {label.name: label.value for label in series.labels}
            for sample in series.samples:
                if not math.isfinite(sample.value) or not math.isfinite(sample.timestamp):
                    raise OrchestratorError("orchestrator_metrics_sample_invalid")
                offset = sample.timestamp - start
                if not offset.is_integer() or not 0 <= offset <= 4800:
                    raise OrchestratorError("orchestrator_metrics_sample_invalid")
                if (
                    result.query_id == "websocket_active"
                    and labels.get("audience") == "participant"
                ):
                    second = int(offset)
                    websocket_by_second[second] = websocket_by_second.get(second, 0) + sample.value
                if sample.value > manifest.scenario.thresholds.http_5xx_maximum and (
                    result.query_id == "gateway_5xx_rate"
                    or (
                        result.query_id == "http_requests_rate"
                        and labels.get("status_class") == "5xx"
                    )
                ):
                    failures.append("application_http_5xx")
    faults = manifest.scenario.fault_schedule
    windows = tuple(
        (
            faults[index].offset_seconds,
            faults[index + 1].offset_seconds
            + manifest.scenario.thresholds.recovery_seconds_maximum,
        )
        for index in (0, 2)
    )
    # 固定障害中と承認済み復旧猶予は通常接続数の判定から除く。
    missing = sum(
        websocket_by_second.get(second)
        != manifest.scenario.thresholds.normal_participant_websockets
        for second in range(
            manifest.scenario.lifecycle.measurement_start_seconds,
            manifest.scenario.lifecycle.measurement_end_seconds,
        )
        if not any(start <= second < end for start, end in windows)
    )
    if missing:
        failures.append("normal_participant_websockets")
    return AcceptanceReport(
        run_id=manifest.run_id,
        manifest_sha256=context.manifest_sha256,
        decision="FAIL" if failures else "PASS",
        failure_codes=tuple(sorted(set(failures))),
        generator_server_failures=server_failures,
        normal_websocket_missing_seconds=missing,
    )


def seal_acceptance(
    manifest: LoadTestManifest,
    generator_directory: Path,
    app_directory: Path,
    fault_directory: Path,
    output: Path,
) -> ArtifactIdentity:
    metrics_body, _ = read_secure_external_file(
        generator_directory / "generator-metrics-summary.json"
    )
    export_body, _ = read_secure_external_file(
        app_directory / f"metrics-{manifest.run_id}.json", maximum_bytes=MAX_METRICS_ARTIFACT_BYTES
    )
    status_body, _ = read_secure_external_file(fault_directory / "fault-status.json")
    journal_body, _ = read_secure_external_file(fault_directory / "fault.jsonl")
    validate_fault_evidence(
        manifest, journal_body, FaultControllerStatus.model_validate_json(status_body)
    )
    csv_body, _ = read_secure_external_file(
        app_directory / f"metrics-{manifest.run_id}.csv", maximum_bytes=MAX_METRICS_ARTIFACT_BYTES
    )
    checksum_body, _ = read_secure_external_file(
        app_directory / f"metrics-{manifest.run_id}.sha256"
    )
    expected_checksum = (
        f"{sha256(export_body).hexdigest()}  metrics-{manifest.run_id}.json\n"
        f"{sha256(csv_body).hexdigest()}  metrics-{manifest.run_id}.csv\n"
    ).encode("ascii")
    if checksum_body != expected_checksum:
        raise OrchestratorError("orchestrator_metrics_checksum_invalid")
    report = evaluate_acceptance(
        manifest,
        GeneratorMetrics.model_validate_json(metrics_body),
        MetricsExportArtifact.model_validate_json(export_body),
    )
    artifact = write_canonical_artifact(output, "acceptance.json", report)
    if report.decision != "PASS":
        raise OrchestratorError("orchestrator_acceptance_failed")
    return artifact
