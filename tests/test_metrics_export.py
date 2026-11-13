import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request
from uuid import UUID

import pytest
from starlette.routing import Route

from quiz_app.load_test import metrics_export as metrics_export_module
from quiz_app.load_test.manifest import LoadTestManifest
from quiz_app.load_test.metrics_export import (
    APPROVED_ROUTE_TEMPLATES,
    FIXED_QUERIES,
    MAXIMUM_RESPONSE_BYTES,
    PROMETHEUS_QUERY_RANGE_URL,
    FixedQuery,
    MetricsExportArtifact,
    MetricsExportError,
    _is_safe_manifest_file_status,
    _is_safe_manifest_parent_status,
    export_metrics,
    fetch_fixed_query,
    main,
    write_metrics_artifact,
)
from quiz_app.main import create_app
from quiz_app.observability.middleware import METRICS_PATH

RUN_ID = UUID("90000000-0000-4000-8000-000000000001")
SOURCE_COMMIT = "a" * 40
STARTED_AT = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
ENDED_AT = STARTED_AT + timedelta(minutes=80)
QUERY_ENDED_AT = STARTED_AT + timedelta(minutes=10)


class StubResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.is_closed = False

    def read(self, _: int) -> bytes:
        return self._body

    def close(self) -> None:
        self.is_closed = True


def _empty_response_opener(request: Request, *, timeout: float) -> StubResponse:
    assert timeout == 5.0
    url = request.full_url
    parsed = urlsplit(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == PROMETHEUS_QUERY_RANGE_URL
    parameters = parse_qs(parsed.query)
    assert parameters["query"][0] in {query.expression for query in FIXED_QUERIES}
    assert parameters["step"] == ["1"]
    query = next(query for query in FIXED_QUERIES if query.expression == parameters["query"][0])
    label_values = {
        "instance": "api-primary",
        "route": "/api/health/live",
        "status_class": "2xx",
        "audience": "participant",
        "outcome": "normal",
        "code_class": "1xxx",
        "reason": "realtime_unavailable",
        "result": "success",
        "state": "size",
    }
    if query.query_id == "scrape_targets_up":
        result = [
            {
                "metric": {"job": job},
                "values": [[float(parameters["end"][0]), "1"]],
            }
            for job in (
                "quiz-api",
                "quiz-gateway",
                "quiz-postgres",
                "quiz-redis",
                "quiz-node",
            )
        ]
    elif query.query_id in {"redis_dependency_up", "database_pool_supported"}:
        result = [
            {
                "metric": {"instance": instance},
                "values": [[float(parameters["end"][0]), "1"]],
            }
            for instance in ("api-primary", "api-secondary")
        ]
    else:
        result = [
            {
                "metric": {label: label_values[label] for label in query.expected_labels},
                "values": [[float(parameters["end"][0]), "1"]],
            }
        ]
    return StubResponse(
        json.dumps(
            {"status": "success", "data": {"resultType": "matrix", "result": result}}
        ).encode("utf-8")
    )


def _manifest() -> LoadTestManifest:
    return LoadTestManifest.model_construct(
        run_id=RUN_ID,
        scheduled_start_at=STARTED_AT,
        artifacts=SimpleNamespace(source_commit=SOURCE_COMMIT),
        scenario=SimpleNamespace(lifecycle=SimpleNamespace(ramp_down_end_seconds=4_800)),
    )


def _artifact() -> MetricsExportArtifact:
    return export_metrics(
        _manifest(),
        now=ENDED_AT + timedelta(seconds=1),
        opener=_empty_response_opener,
    )


def test_export_uses_only_fixed_internal_queries() -> None:
    artifact = _artifact()

    assert artifact.run_id == RUN_ID
    assert artifact.source_commit == SOURCE_COMMIT
    assert artifact.started_at == STARTED_AT
    assert artifact.ended_at == ENDED_AT
    assert tuple(result.query_id for result in artifact.results) == tuple(
        query.query_id for query in FIXED_QUERIES
    )


def test_export_rejects_unapproved_prometheus_labels() -> None:
    body = (
        b'{"status":"success","data":{"resultType":"matrix","result":['
        b'{"metric":{"participant_id":"secret"},"values":[[1.0,"1"]]}'
        b"]}}"
    )

    with pytest.raises(MetricsExportError, match="metrics_export_label_name_invalid"):
        fetch_fixed_query(
            FIXED_QUERIES[0],
            STARTED_AT,
            QUERY_ENDED_AT,
            opener=lambda *_, **__: StubResponse(body),
        )


def test_export_requires_exact_expected_labels() -> None:
    body = (
        b'{"status":"success","data":{"resultType":"matrix","result":['
        b'{"metric":{"instance":"api-primary","route":"/api/health/live"},'
        b'"values":[[1788832800.0,"1"]]}'
        b"]}}"
    )
    with pytest.raises(MetricsExportError, match="metrics_export_label_name_invalid"):
        fetch_fixed_query(
            FIXED_QUERIES[0],
            STARTED_AT,
            QUERY_ENDED_AT,
            opener=lambda *_, **__: StubResponse(body),
        )


def test_approved_routes_include_manual_publication_and_preserve_legacy_routes() -> None:
    application = create_app()
    routes = set(application.openapi()["paths"]) | {
        route.path
        for route in application.routes
        if isinstance(route, Route) and route.path != METRICS_PATH
    }

    # 旧証跡のroute許可は維持し、現APIへ廃止routeを復活させない。
    publication_route = "/api/v1/operator/rounds/{round_id}/publish-results"
    assert publication_route in routes & APPROVED_ROUTE_TEMPLATES
    legacy_only = {
        "/api/v1/participant/session/heartbeat",
        "/api/v1/participant/rounds/{round_id}/readiness",
    }
    # 現行の運営専用APIを、旧正式試験の固定export契約へ追加しない。
    current_only = {
        "/api/v1/operator/response-times",
        "/api/v1/operator/participant-preview",
        "/api/v1/operator/final-round",
        "/api/v1/operator/award-ceremony",
    }
    assert not legacy_only & routes
    assert current_only <= routes
    assert APPROVED_ROUTE_TEMPLATES == (routes - current_only) | {"unmatched"} | legacy_only


@pytest.mark.parametrize("route", sorted(APPROVED_ROUTE_TEMPLATES))
def test_export_accepts_current_route_templates(route: str) -> None:
    route_query = FixedQuery("route-test", "vector(1)", frozenset({"route"}))
    body = json.dumps(
        {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {"route": route},
                        "values": [[STARTED_AT.timestamp(), "1"]],
                    }
                ],
            },
        }
    ).encode("ascii")

    result = fetch_fixed_query(
        route_query,
        STARTED_AT,
        QUERY_ENDED_AT,
        opener=lambda *_, **__: StubResponse(body),
    )

    assert result.series[0].labels[0].value == route


@pytest.mark.parametrize(
    "route",
    [
        "/api/v1/operator/events/{event_id}",
        "/api/v1/operator/events/{event_id}/ranking",
        "/api/v1/operator/events/{event_id}/ranking-visibility",
        "/api/v1/participant/events/{event_slug}/join",
        "/api/v1/public/events/{event_slug}",
        "/api/v1/public/state",
        "/api/v1/participant/id-candidate",
        "/api/v1/operator/response-times",
    ],
)
def test_export_rejects_routes_outside_approved_contract(route: str) -> None:
    route_query = FixedQuery("route-test", "vector(1)", frozenset({"route"}))
    body = json.dumps(
        {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {"route": route},
                        "values": [[STARTED_AT.timestamp(), "1"]],
                    }
                ],
            },
        }
    ).encode("ascii")

    with pytest.raises(MetricsExportError, match="metrics_export_label_value_invalid"):
        fetch_fixed_query(
            route_query,
            STARTED_AT,
            QUERY_ENDED_AT,
            opener=lambda *_, **__: StubResponse(body),
        )


def test_export_allows_only_fixed_unmatched_route_label() -> None:
    route_query = FixedQuery("route-test", "vector(1)", frozenset({"route"}))
    valid = (
        '{"status":"success","data":{"resultType":"matrix","result":['
        f'{{"metric":{{"route":"unmatched"}},"values":[[{STARTED_AT.timestamp()},"1"]]}}'
        "]}}"
    ).encode("ascii")
    result = fetch_fixed_query(
        route_query,
        STARTED_AT,
        QUERY_ENDED_AT,
        opener=lambda *_, **__: StubResponse(valid),
    )
    assert result.series[0].labels[0].value == "unmatched"

    invalid = valid.replace(b'"unmatched"', b'"/raw/private/value"')
    with pytest.raises(MetricsExportError, match="metrics_export_label_value_invalid"):
        fetch_fixed_query(
            route_query,
            STARTED_AT,
            QUERY_ENDED_AT,
            opener=lambda *_, **__: StubResponse(invalid),
        )
    raw_uuid = valid.replace(
        b'"unmatched"',
        b'"/api/v1/participant/rounds/11111111-1111-4111-8111-111111111111/answers"',
    )
    with pytest.raises(MetricsExportError, match="metrics_export_label_value_invalid"):
        fetch_fixed_query(
            route_query,
            STARTED_AT,
            QUERY_ENDED_AT,
            opener=lambda *_, **__: StubResponse(raw_uuid),
        )


def test_quantile_nan_is_a_missing_sample_but_infinity_is_rejected() -> None:
    query = next(query for query in FIXED_QUERIES if query.query_id == "http_latency_p95")
    body = json.dumps(
        {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {
                            "instance": "api-primary",
                            "route": "/api/health/live",
                        },
                        "values": [
                            [STARTED_AT.timestamp(), "NaN"],
                            [STARTED_AT.timestamp() + 1, "0.1"],
                        ],
                    }
                ],
            },
        }
    ).encode("ascii")
    result = fetch_fixed_query(
        query,
        STARTED_AT,
        QUERY_ENDED_AT,
        opener=lambda *_, **__: StubResponse(body),
    )
    assert [sample.value for sample in result.series[0].samples] == [0.1]

    infinity = body.replace(b'"NaN"', b'"+Inf"')
    with pytest.raises(MetricsExportError, match="metrics_export_sample_invalid"):
        fetch_fixed_query(
            query,
            STARTED_AT,
            QUERY_ENDED_AT,
            opener=lambda *_, **__: StubResponse(infinity),
        )


def test_chunk_boundary_deduplicates_equal_value_and_rejects_conflict() -> None:
    query = FixedQuery("chunk-test", "vector(1)", frozenset())

    def chunk_opener(request: Request, *, timeout: float) -> StubResponse:
        assert timeout == 5.0
        parameters = parse_qs(urlsplit(request.full_url).query)
        start = float(parameters["start"][0])
        end = float(parameters["end"][0])
        body = json.dumps(
            {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [{"metric": {}, "values": [[start, "1"], [end, "1"]]}],
                },
            }
        ).encode("ascii")
        return StubResponse(body)

    merged = fetch_fixed_query(
        query,
        STARTED_AT,
        STARTED_AT + timedelta(minutes=20),
        opener=chunk_opener,
    )
    assert len(merged.series[0].samples) == 3

    call_count = 0

    def conflicting_opener(request: Request, *, timeout: float) -> StubResponse:
        nonlocal call_count
        call_count += 1
        parameters = parse_qs(urlsplit(request.full_url).query)
        start = float(parameters["start"][0])
        end = float(parameters["end"][0])
        value = call_count
        body = json.dumps(
            {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {},
                            "values": [[start, str(value)], [end, str(value)]],
                        }
                    ],
                },
            }
        ).encode("ascii")
        return StubResponse(body)

    with pytest.raises(MetricsExportError, match="metrics_export_chunk_boundary_conflict"):
        fetch_fixed_query(
            query,
            STARTED_AT,
            STARTED_AT + timedelta(minutes=20),
            opener=conflicting_opener,
        )


@pytest.mark.parametrize(
    "timestamps",
    [
        [STARTED_AT.timestamp() - 1],
        [ENDED_AT.timestamp() + 1],
        [STARTED_AT.timestamp(), STARTED_AT.timestamp()],
        [STARTED_AT.timestamp() + 1, STARTED_AT.timestamp()],
    ],
)
def test_export_rejects_out_of_window_or_non_increasing_samples(
    timestamps: list[float],
) -> None:
    values = ",".join(f'[{timestamp},"1"]' for timestamp in timestamps)
    body = (
        '{"status":"success","data":{"resultType":"matrix","result":['
        f'{{"metric":{{}},"values":[{values}]}}'
        "]}}"
    ).encode("ascii")
    no_label_query = FixedQuery("sample-test", "vector(1)", frozenset())
    with pytest.raises(MetricsExportError, match="metrics_export_sample_invalid"):
        fetch_fixed_query(
            no_label_query,
            STARTED_AT,
            QUERY_ENDED_AT,
            opener=lambda *_, **__: StubResponse(body),
        )


def test_export_rejects_oversized_prometheus_response() -> None:
    oversized = b"x" * (MAXIMUM_RESPONSE_BYTES + 1)

    with pytest.raises(MetricsExportError, match="prometheus_response_invalid"):
        fetch_fixed_query(
            FIXED_QUERIES[0],
            STARTED_AT,
            QUERY_ENDED_AT,
            opener=lambda *_, **__: StubResponse(oversized),
        )


def test_artifact_is_0600_and_never_overwritten(tmp_path: Path) -> None:
    artifact_directory = tmp_path / "artifacts"
    artifact_directory.mkdir(mode=0o700)
    artifact_directory.chmod(0o700)

    outputs = write_metrics_artifact(artifact_directory, _artifact())

    assert outputs.json_path.name == f"metrics-{RUN_ID}.json"
    for output in (outputs.json_path, outputs.csv_path, outputs.checksum_path):
        assert output.stat().st_mode & 0o777 == 0o600
    assert "participant_id" not in outputs.json_path.read_text(encoding="utf-8")
    assert SOURCE_COMMIT in outputs.json_path.read_text(encoding="utf-8")
    assert outputs.csv_path.read_text(encoding="utf-8").startswith(
        "run_id,source_commit,started_at,ended_at,query_id,labels_json,timestamp,value\n"
    )
    assert SOURCE_COMMIT in outputs.csv_path.read_text(encoding="utf-8")
    assert outputs.checksum_path.read_text(encoding="ascii") == (
        f"{hashlib.sha256(outputs.json_path.read_bytes()).hexdigest()}  {outputs.json_path.name}\n"
        f"{hashlib.sha256(outputs.csv_path.read_bytes()).hexdigest()}  {outputs.csv_path.name}\n"
    )
    with pytest.raises(MetricsExportError, match="metrics_artifact_directory_not_empty"):
        write_metrics_artifact(artifact_directory, _artifact())


@pytest.mark.parametrize("failing_write", [2, 3])
def test_partial_artifact_write_removes_only_files_from_current_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_write: int,
) -> None:
    artifact_directory = tmp_path / "artifacts"
    artifact_directory.mkdir(mode=0o700)
    artifact_directory.chmod(0o700)
    original_write = metrics_export_module._write_new_file
    write_count = 0

    def fail_selected_write(
        directory_descriptor: int,
        name: str,
        content: bytes,
    ) -> None:
        nonlocal write_count
        write_count += 1
        if write_count == failing_write:
            raise OSError("forced write failure")
        original_write(directory_descriptor, name, content)

    monkeypatch.setattr(metrics_export_module, "_write_new_file", fail_selected_write)

    with pytest.raises(MetricsExportError, match="metrics_artifact_write_failed"):
        write_metrics_artifact(artifact_directory, _artifact())

    assert list(artifact_directory.iterdir()) == []


def test_artifact_rejects_shared_or_symlink_directory(tmp_path: Path) -> None:
    shared_directory = tmp_path / "shared"
    shared_directory.mkdir(mode=0o755)
    shared_directory.chmod(0o755)
    with pytest.raises(MetricsExportError, match="metrics_artifact_directory_invalid"):
        write_metrics_artifact(shared_directory, _artifact())

    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    symlink = tmp_path / "linked"
    symlink.symlink_to(target, target_is_directory=True)
    with pytest.raises(MetricsExportError, match="metrics_artifact_directory_invalid"):
        write_metrics_artifact(symlink, _artifact())


def test_export_window_is_bounded() -> None:
    with pytest.raises(MetricsExportError, match="metrics_export_window_invalid"):
        fetch_fixed_query(
            FIXED_QUERIES[0],
            STARTED_AT,
            STARTED_AT + timedelta(minutes=91),
            opener=_empty_response_opener,
        )


def test_export_rejects_early_collection() -> None:
    with pytest.raises(MetricsExportError, match="metrics_export_too_early"):
        export_metrics(
            _manifest(),
            now=ENDED_AT - timedelta(seconds=1),
            opener=_empty_response_opener,
        )


def test_export_rejects_empty_core_queries() -> None:
    def empty_opener(*_: object, **__: object) -> StubResponse:
        return StubResponse(b'{"status":"success","data":{"resultType":"matrix","result":[]}}')

    with pytest.raises(MetricsExportError, match="metrics_export_core_query_empty"):
        export_metrics(
            _manifest(),
            now=ENDED_AT + timedelta(seconds=1),
            opener=empty_opener,
        )


@pytest.mark.parametrize(
    ("query_id", "instance", "terminal_value"),
    [
        ("redis_dependency_up", "api-primary", "0"),
        ("database_pool_supported", "api-secondary", "0"),
        ("redis_dependency_up", "api-primary", "1"),
    ],
)
def test_export_requires_both_api_dependencies_recovered(
    query_id: str,
    instance: str,
    terminal_value: str,
) -> None:
    def incomplete_api_opener(request: Request, *, timeout: float) -> StubResponse:
        response = _empty_response_opener(request, timeout=timeout)
        parameters = parse_qs(urlsplit(request.full_url).query)
        query = next(
            fixed_query
            for fixed_query in FIXED_QUERIES
            if fixed_query.expression == parameters["query"][0]
        )
        if query.query_id != query_id:
            return response
        samples = [
            {
                "metric": {"instance": instance},
                "values": [[float(parameters["start"][0]), terminal_value]],
            }
        ]
        if terminal_value == "0":
            other_instance = "api-secondary" if instance == "api-primary" else "api-primary"
            samples.append(
                {
                    "metric": {"instance": other_instance},
                    "values": [[float(parameters["start"][0]), "1"]],
                }
            )
        return StubResponse(
            json.dumps(
                {
                    "status": "success",
                    "data": {"resultType": "matrix", "result": samples},
                }
            ).encode("utf-8")
        )

    with pytest.raises(MetricsExportError, match="metrics_export_api_recovery_invalid"):
        export_metrics(
            _manifest(),
            now=ENDED_AT + timedelta(seconds=1),
            opener=incomplete_api_opener,
        )


def test_export_requires_fresh_terminal_recovery_sample() -> None:
    def stale_recovery_opener(request: Request, *, timeout: float) -> StubResponse:
        response = _empty_response_opener(request, timeout=timeout)
        parameters = parse_qs(urlsplit(request.full_url).query)
        query = next(
            fixed_query
            for fixed_query in FIXED_QUERIES
            if fixed_query.expression == parameters["query"][0]
        )
        if query.query_id != "postgres_dependency_up":
            return response
        stale_timestamp = float(parameters["end"][0]) - 2
        return StubResponse(
            json.dumps(
                {
                    "status": "success",
                    "data": {
                        "resultType": "matrix",
                        "result": [{"metric": {}, "values": [[stale_timestamp, "1"]]}],
                    },
                }
            ).encode("utf-8")
        )

    with pytest.raises(
        MetricsExportError,
        match="metrics_export_dependency_recovery_invalid",
    ):
        export_metrics(
            _manifest(),
            now=ENDED_AT + timedelta(seconds=1),
            opener=stale_recovery_opener,
        )


def test_export_enforces_total_series_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metrics_export_module, "MAXIMUM_TOTAL_SERIES", 1)
    with pytest.raises(MetricsExportError, match="metrics_export_result_limit_exceeded"):
        export_metrics(
            _manifest(),
            now=ENDED_AT + timedelta(seconds=1),
            opener=_empty_response_opener,
        )


def test_cli_rejects_deprecated_identity_and_time_arguments() -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "--run-id",
                str(RUN_ID),
                "--source-commit",
                SOURCE_COMMIT,
                "--started-at",
                STARTED_AT.isoformat(),
                "--ended-at",
                ENDED_AT.isoformat(),
            ]
        )


def test_manifest_mount_accepts_root_parent_but_requires_runtime_owned_file() -> None:
    root_parent = SimpleNamespace(st_mode=0o40755, st_uid=0, st_nlink=1, st_size=0)
    runtime_file = SimpleNamespace(st_mode=0o100600, st_uid=65534, st_nlink=1, st_size=100)

    assert _is_safe_manifest_parent_status(root_parent, 65534)
    assert _is_safe_manifest_file_status(runtime_file, 65534)
    assert not _is_safe_manifest_file_status(
        SimpleNamespace(st_mode=0o100600, st_uid=0, st_nlink=1, st_size=100),
        65534,
    )
