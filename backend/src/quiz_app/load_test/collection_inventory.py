from __future__ import annotations

import json
import re
from collections.abc import Iterator
from hashlib import sha256
from pathlib import PurePosixPath

from pydantic import BaseModel, Field, TypeAdapter

from quiz_app.load_test.controller import _journal_progress
from quiz_app.load_test.controller_contracts import (
    ControllerCheckpoint,
    ControllerJournalRecord,
    VerificationRequestExpectation,
    canonical_json_bytes,
    manifest_context,
)
from quiz_app.load_test.controller_verification import (
    GeneratorRoundEvidence,
    LoadedGeneratorRoundEvidence,
    RoundVerificationRequest,
    build_verification_request,
    verification_request_bytes,
)
from quiz_app.load_test.db_verifier_artifacts import read_secure_external_file
from quiz_app.load_test.db_verifier_contracts import (
    DB_VERIFIER_REQUEST_ADAPTER,
    QueryExecutionMetadata,
)
from quiz_app.load_test.db_verifier_models import (
    DbVerifierReport,
    FinalVerificationReport,
    LockObservationReport,
    PerRoundVerificationReport,
)
from quiz_app.load_test.db_verifier_queries import query_specs_for_mode
from quiz_app.load_test.fault_controller_contracts import (
    FaultControllerJournalRecord,
    FaultControllerStatus,
    FaultTopologyIdentity,
)
from quiz_app.load_test.manifest import LoadTestManifest
from quiz_app.load_test.metrics_export import (
    FIXED_QUERIES,
    MetricsExportArtifact,
    _serialize_csv,
    _validate_label_value,
)
from quiz_app.load_test.orchestrator import RampReady
from quiz_app.load_test.orchestrator_acceptance import (
    AcceptanceReport,
    GeneratorMetrics,
    evaluate_acceptance,
)
from quiz_app.load_test.orchestrator_contracts import (
    OrchestratorError,
    RunFiles,
    RunResult,
    StrictRunModel,
    require_private_directory,
)
from quiz_app.load_test.orchestrator_fault_evidence import validate_fault_evidence
from quiz_app.load_test.verification_contracts import ArtifactIdentity, RoundVerificationReceipt
from quiz_app.load_test_aws.models import AwsPreparationEvidence

MAX_COLLECTION_BYTES = 512 * 1024 * 1024
MAX_COLLECTION_FILES = 200
_UUID = r"[0-9a-f-]{36}"
_ROUND = r"(?:0[1-9]|10)"
_PATTERNS = {
    "inputs": r"(?:manifest|preparation)\.json",
    "generator": rf"generator-(?:round-{_ROUND}|ramp-ready|metrics-summary)\.json",
    "db-inputs": (
        rf"(?:db-verifier-(?:per-round|final)-round-{_ROUND}-{_UUID}"
        rf"|db-lock-observation-round-{_ROUND}-{_UUID})\.json"
    ),
    "db-reports": (
        rf"db-(?:verification-(?:round-{_ROUND}|final)-(?:report|receipt)-{_UUID}"
        rf"|lock-observation-round-{_ROUND}-{_UUID})\.json"
    ),
    "checkpoints": rf"controller-round-{_ROUND}-{_UUID}\.json",
    "requests": rf"verification-request-round-{_ROUND}-{_UUID}\.json",
    "journal": r"controller\.jsonl",
    "app-faults": r"fault(?:-status\.json|\.jsonl)",
    "app-export": rf"metrics-{_UUID}\.(?:json|csv|sha256)",
    "results": r"(?:result|acceptance)\.json",
}
_REPORT = TypeAdapter(DbVerifierReport)
_QUERY_IDS = frozenset(
    spec.query_id
    for mode in ("per-round", "final", "observe-locks")
    for spec in query_specs_for_mode(mode)
)
_FAILURE_CODES = frozenset(
    {
        "round_identity_mismatch",
        "scoring_policy_mismatch",
        "readiness_mismatch",
        "answers_mismatch",
        "score_ledgers_mismatch",
        "option_stats_mismatch",
        "ranking_mismatch",
        "operation_audits_mismatch",
        "idempotency_records_mismatch",
        "final_event_mismatch",
        "final_scoring_policy_mismatch",
        "final_rounds_mismatch",
        "final_readiness_mismatch",
        "final_answers_mismatch",
        "final_score_ledgers_mismatch",
        "final_option_stats_mismatch",
        "final_ranking_mismatch",
        "final_participants_mismatch",
        "final_integrity_mismatch",
        "final_operation_audits_mismatch",
        "final_idempotency_records_mismatch",
        "final_lock_wait_nonzero",
        "generator_http_5xx",
        "application_http_5xx",
        "normal_participant_websockets",
    }
)


class CollectedFile(StrictRunModel):
    path: str = Field(pattern=r"^[a-z-]+/[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
    body: bytes = Field(min_length=1, max_length=MAX_COLLECTION_BYTES)

    @property
    def identity(self) -> ArtifactIdentity:
        return ArtifactIdentity(
            name=PurePosixPath(self.path).name,
            sha256=sha256(self.body).hexdigest(),
            size_bytes=len(self.body),
        )


def allowed_path(path: str) -> bool:
    parts = path.split("/")
    return (
        len(parts) == 2
        and parts[0] in _PATTERNS
        and re.fullmatch(_PATTERNS[parts[0]], parts[1]) is not None
    )


def _models(value: object) -> Iterator[BaseModel]:
    if isinstance(value, BaseModel):
        yield value
        for name in type(value).model_fields:
            yield from _models(getattr(value, name))
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _models(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _models(item)


def _parse(item: CollectedFile) -> tuple[BaseModel, ...]:
    folder, name = item.path.split("/")
    model: type[BaseModel]
    if folder == "inputs":
        model = LoadTestManifest if name == "manifest.json" else AwsPreparationEvidence
    elif folder == "generator":
        model = (
            RampReady
            if name == "generator-ramp-ready.json"
            else GeneratorMetrics
            if name == "generator-metrics-summary.json"
            else GeneratorRoundEvidence
        )
    elif folder == "db-inputs":
        return (DB_VERIFIER_REQUEST_ADAPTER.validate_json(item.body, strict=True),)
    elif folder == "db-reports":
        if "-receipt-" not in name:
            return (_REPORT.validate_json(item.body, strict=True),)
        model = RoundVerificationReceipt
    elif folder == "checkpoints":
        model = ControllerCheckpoint
    elif folder == "requests":
        model = RoundVerificationRequest
    elif folder == "journal" or name == "fault.jsonl":
        model = ControllerJournalRecord if folder == "journal" else FaultControllerJournalRecord
        lines = item.body.splitlines()
        if len(lines) > 1000 or not item.body.endswith(b"\n"):
            raise OrchestratorError("collection_journal_invalid")
        return tuple(model.model_validate_json(line, strict=True) for line in lines)
    elif folder == "app-faults":
        model = FaultControllerStatus
    elif folder == "app-export":
        if not name.endswith(".json"):
            return ()
        model = MetricsExportArtifact
    elif name == "acceptance.json":
        model = AcceptanceReport
    else:
        model = RunResult
    return (model.model_validate_json(item.body, strict=True),)


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    values = dict(pairs)
    if len(values) != len(pairs):
        raise OrchestratorError("collection_duplicate_json_key")
    return values


def validate_inventory(
    items: tuple[CollectedFile, ...], manifest: LoadTestManifest, prepared: AwsPreparationEvidence
) -> RunResult:
    """同じschema境界で送信前と回収後を検証し、秘密入力・自由形式ログを含めない。"""
    if not 3 <= len(items) <= MAX_COLLECTION_FILES:
        raise OrchestratorError("collection_inventory_size_invalid")
    if sum(len(item.body) for item in items) > MAX_COLLECTION_BYTES:
        raise OrchestratorError("collection_inventory_too_large")
    by_path = {item.path: item for item in items}
    identities = {item.identity for item in items}
    if len(by_path) != len(items):
        raise OrchestratorError("collection_duplicate_artifact")
    context = manifest_context(manifest)
    if prepared.run_id != manifest.run_id or prepared.manifest_sha256 != context.manifest_sha256:
        raise OrchestratorError("collection_binding_invalid")
    models: list[BaseModel] = []
    for item in items:
        if not allowed_path(item.path):
            raise OrchestratorError("collection_artifact_not_allowed")
        # Pydanticが後勝ちで捨てる重複keyへ秘密値を隠し、元bytesを持ち出す経路を閉じる。
        if item.path.endswith((".json", ".jsonl")):
            bodies = item.body.splitlines() if item.path.endswith(".jsonl") else (item.body,)
            for body in bodies:
                json.loads(body, object_pairs_hook=_unique_keys)
        models.extend(_parse(item))
    metric_paths = {path for path in by_path if path.startswith("app-export/")}
    if metric_paths and metric_paths != {
        f"app-export/metrics-{manifest.run_id}.{suffix}" for suffix in ("json", "csv", "sha256")
    }:
        raise OrchestratorError("collection_metrics_incomplete")
    for value in models:
        for nested in _models(value):
            for name in (
                "run_id",
                "event_id",
                "source_commit",
                "schema_revision",
                "manifest_sha256",
                "scenario_sha256",
                "event_slug",
                "public_origin",
            ):
                if hasattr(nested, name) and getattr(nested, name) != getattr(context, name):
                    raise OrchestratorError("collection_artifact_binding_mismatch")
            if hasattr(nested, "failure_codes"):
                codes = nested.failure_codes
                if set(codes) - _FAILURE_CODES or (
                    getattr(nested, "decision", None) == "PASS" and codes
                ):
                    raise OrchestratorError("collection_failure_code_invalid")
            if isinstance(nested, MetricsExportArtifact):
                _validate_metrics(nested, by_path)
            if isinstance(nested, QueryExecutionMetadata) and nested.query_id not in _QUERY_IDS:
                raise OrchestratorError("collection_query_id_invalid")
            if isinstance(nested, FaultTopologyIdentity) and (
                re.fullmatch(r"[a-zA-Z0-9.-]{3,253}", nested.redis_hostname) is None
                or any(
                    re.fullmatch(r"[a-zA-Z0-9.-]+:[0-9]{1,5}", endpoint) is None
                    for endpoint in (nested.listen, nested.upstream)
                )
            ):
                raise OrchestratorError("collection_fault_endpoint_invalid")
    try:
        archived_manifest = LoadTestManifest.model_validate_json(
            by_path["inputs/manifest.json"].body
        )
        archived_prepared = AwsPreparationEvidence.model_validate_json(
            by_path["inputs/preparation.json"].body
        )
        result = RunResult.model_validate_json(by_path["results/result.json"].body)
    except KeyError:
        raise OrchestratorError("collection_required_artifact_missing") from None
    if archived_manifest != manifest or archived_prepared != prepared:
        raise OrchestratorError("collection_binding_invalid")
    if result.finished_at.utcoffset() is None or not (
        prepared.prepared_at <= result.finished_at < prepared.expires_at
    ):
        raise OrchestratorError("collection_result_time_invalid")
    if len(result.artifacts) != len({value.name for value in result.artifacts}):
        raise OrchestratorError("collection_duplicate_result_artifact")
    for identity in result.artifacts:
        if identity not in identities:
            raise OrchestratorError("collection_result_artifact_missing_or_changed")
    if result.decision == "PASS":
        _validate_complete(models, identities, by_path, manifest)
    return result


def _validate_metrics(value: MetricsExportArtifact, by_path: dict[str, CollectedFile]) -> None:
    queries = {query.query_id: query for query in FIXED_QUERIES}
    if {result.query_id for result in value.results} - queries.keys():
        raise OrchestratorError("collection_metrics_query_invalid")
    for result in value.results:
        for series in result.series:
            if {label.name for label in series.labels} != queries[result.query_id].expected_labels:
                raise OrchestratorError("collection_metrics_labels_invalid")
            for label in series.labels:
                _validate_label_value(label.name, label.value)
    prefix = f"app-export/metrics-{value.run_id}"
    try:
        json_body, csv_body = by_path[prefix + ".json"].body, by_path[prefix + ".csv"].body
        checksum = by_path[prefix + ".sha256"].body
    except KeyError:
        raise OrchestratorError("collection_metrics_incomplete") from None
    expected = (
        f"{sha256(json_body).hexdigest()}  metrics-{value.run_id}.json\n"
        f"{sha256(csv_body).hexdigest()}  metrics-{value.run_id}.csv\n"
    ).encode("ascii")
    if csv_body != _serialize_csv(value) or checksum != expected:
        raise OrchestratorError("collection_metrics_checksum_invalid")


def _validate_complete(
    models: list[BaseModel],
    identities: set[ArtifactIdentity],
    by_path: dict[str, CollectedFile],
    manifest: LoadTestManifest,
) -> None:
    required = {
        GeneratorRoundEvidence: 10,
        GeneratorMetrics: 1,
        RampReady: 1,
        ControllerCheckpoint: 10,
        RoundVerificationRequest: 10,
        MetricsExportArtifact: 1,
        FaultControllerStatus: 1,
        AcceptanceReport: 1,
        PerRoundVerificationReport: 10,
        FinalVerificationReport: 1,
        LockObservationReport: 10,
        RoundVerificationReceipt: 11,
    }
    for model, count in required.items():
        if sum(isinstance(value, model) for value in models) != count:
            raise OrchestratorError("collection_pass_evidence_incomplete")
    if not any(isinstance(value, ControllerJournalRecord) for value in models) or not any(
        isinstance(value, FaultControllerJournalRecord) for value in models
    ):
        raise OrchestratorError("collection_pass_journal_missing")
    for model in (
        GeneratorRoundEvidence,
        ControllerCheckpoint,
        RoundVerificationRequest,
        PerRoundVerificationReport,
        LockObservationReport,
    ):
        if {value.round_number for value in models if isinstance(value, model)} != set(
            range(1, 11)
        ):
            raise OrchestratorError("collection_pass_rounds_incomplete")
    for value in models:
        if (
            isinstance(
                value, (PerRoundVerificationReport, FinalVerificationReport, LockObservationReport)
            )
            and value.decision != "PASS"
        ):
            raise OrchestratorError("collection_database_failed")
        if isinstance(value, AcceptanceReport) and value.decision != "PASS":
            raise OrchestratorError("collection_acceptance_failed")
        for nested in _models(value):
            if isinstance(nested, ArtifactIdentity) and nested not in identities:
                raise OrchestratorError("collection_referenced_artifact_missing_or_changed")
    validate_fault_evidence(
        manifest,
        by_path["app-faults/fault.jsonl"].body,
        FaultControllerStatus.model_validate_json(by_path["app-faults/fault-status.json"].body),
    )
    expected = evaluate_acceptance(
        manifest,
        GeneratorMetrics.model_validate_json(
            by_path["generator/generator-metrics-summary.json"].body
        ),
        MetricsExportArtifact.model_validate_json(
            by_path[f"app-export/metrics-{manifest.run_id}.json"].body
        ),
    )
    if expected.decision != "PASS" or expected != AcceptanceReport.model_validate_json(
        by_path["results/acceptance.json"].body
    ):
        raise OrchestratorError("collection_acceptance_mismatch")
    _validate_database_bindings(models, by_path)
    _validate_controller(by_path, manifest)


def _validate_database_bindings(models: list[BaseModel], by_path: dict[str, CollectedFile]) -> None:
    reports = (PerRoundVerificationReport, FinalVerificationReport)
    for value in models:
        if isinstance(value, reports):
            item = by_path.get(f"db-inputs/{value.input_artifact.name}")
            if item is None or item.identity != value.input_artifact:
                raise OrchestratorError("collection_database_input_mismatch")
            parsed = DB_VERIFIER_REQUEST_ADAPTER.validate_json(item.body, strict=True)
            fields = set(type(value).model_fields) & set(RoundVerificationRequest.model_fields) - {
                "format"
            }
            if (
                not hasattr(parsed, "request")
                or parsed.mode != value.mode
                or parsed.request.model_dump(include=fields) != value.model_dump(include=fields)
            ):
                raise OrchestratorError("collection_database_input_mismatch")
        if isinstance(value, RoundVerificationReceipt):
            item = by_path.get(f"db-reports/{value.report.name}")
            if item is None or item.identity != value.report:
                raise OrchestratorError("collection_database_receipt_mismatch")
            report = _REPORT.validate_json(item.body, strict=True)
            fields = set(type(report).model_fields) & set(RoundVerificationReceipt.model_fields) - {
                "format"
            }
            if not isinstance(report, reports) or value.model_dump(
                include=fields
            ) != report.model_dump(include=fields):
                raise OrchestratorError("collection_database_receipt_mismatch")


def _validate_controller(by_path: dict[str, CollectedFile], manifest: LoadTestManifest) -> None:
    records = [
        ControllerJournalRecord.model_validate_json(line)
        for line in by_path["journal/controller.jsonl"].body.splitlines()
    ]
    previous = None
    for sequence, record in enumerate(records, start=1):
        if (
            record.sequence != sequence
            or record.previous_sha256 != previous
            or record.entry_sha256
            != sha256(
                canonical_json_bytes(record.model_dump(mode="json", exclude={"entry_sha256"}))
            ).hexdigest()
        ):
            raise OrchestratorError("collection_controller_chain_invalid")
        previous = record.entry_sha256

    def find(folder: str, identity: ArtifactIdentity) -> CollectedFile:
        item = by_path.get(f"{folder}/{identity.name}")
        if item is None or item.identity != identity:
            raise OrchestratorError("collection_referenced_artifact_missing_or_changed")
        return item

    def checkpoint(expected: ControllerCheckpoint, identity: ArtifactIdentity) -> None:
        if find("checkpoints", identity).body != canonical_json_bytes(expected) + b"\n":
            raise OrchestratorError("collection_checkpoint_mismatch")

    def request(expected: VerificationRequestExpectation, identity: ArtifactIdentity) -> None:
        generator_item = find("generator", expected.generator_evidence)
        evidence = GeneratorRoundEvidence.model_validate_json(generator_item.body)
        value = build_verification_request(
            manifest,
            round_number=expected.round_number,
            round_id=expected.round_id,
            verification_requested_head_sha256=expected.verification_requested_head_sha256,
            generator=LoadedGeneratorRoundEvidence(
                evidence=evidence, artifact=generator_item.identity, loaded_at=evidence.sealed_at
            ),
            requested_at=expected.requested_at,
        )
        if find("requests", identity).body != verification_request_bytes(value):
            raise OrchestratorError("collection_request_mismatch")

    progress = _journal_progress(manifest, manifest_context(manifest), records, checkpoint, request)
    if progress.phase != "completed" or progress.round_number != 10:
        raise OrchestratorError("collection_controller_incomplete")


def collect_inventory(
    files: RunFiles, manifest: LoadTestManifest, prepared: AwsPreparationEvidence
) -> tuple[CollectedFile, ...]:
    """秘密を持つinputsはmanifest/preparationだけを選び、他の入力やprocessログを除外する。"""
    items: list[CollectedFile] = []
    total = 0
    for folder in _PATTERNS:
        directory = files.root / folder
        if not directory.exists():
            continue
        require_private_directory(directory, files.repository_root)
        for path in sorted(directory.iterdir()):
            relative = f"{folder}/{path.name}"
            if folder == "inputs" and not allowed_path(relative):
                continue
            if not allowed_path(relative) or len(items) >= MAX_COLLECTION_FILES:
                raise OrchestratorError("collection_artifact_not_allowed")
            body, _ = read_secure_external_file(path, maximum_bytes=256 * 1024 * 1024)
            total += len(body)
            if total > MAX_COLLECTION_BYTES:
                raise OrchestratorError("collection_inventory_too_large")
            items.append(CollectedFile(path=relative, body=body))
    result = tuple(items)
    validate_inventory(result, manifest, prepared)
    return result
