from __future__ import annotations

import json
from concurrent.futures import Future
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from db_verifier_fixtures import (
    _answers,
    _FakeDatabase,
    _final_results,
    _ledgers,
    _manifest,
    _ranking,
    _round_id,
    _round_results,
)
from pydantic import SecretStr
from tests.test_fault_controller import FakeClock, FakeHost, _identity, _run
from tests.test_load_test_collection import fixture_items
from tests.test_load_test_controller_verification import _generator_evidence
from tests.test_load_test_orchestrator import AdvancingClock, _bare_runner, _files, _prepared
from tests.test_load_test_orchestrator_acceptance import _export, _metrics
from tests.test_load_test_orchestrator_workflow import ClockedWorkflowApi

from quiz_app.load_test.collection_inventory import (
    CollectedFile,
    collect_inventory,
    validate_inventory,
)
from quiz_app.load_test.controller import LoadTestController
from quiz_app.load_test.controller_contracts import (
    ControllerStatus,
    canonical_json_bytes,
    manifest_context,
)
from quiz_app.load_test.controller_journal import ControllerJournal
from quiz_app.load_test.controller_verification import FileVerificationProvider
from quiz_app.load_test.db_verifier import run_db_verifier
from quiz_app.load_test.db_verifier_artifacts import (
    load_db_verifier_request,
    write_canonical_artifact,
)
from quiz_app.load_test.db_verifier_models import (
    DbVerifierRunResult,
    LockObservationFacts,
    LockObservationReport,
)
from quiz_app.load_test.evidence_handoff import seal_db_verifier_input, seal_lock_observation_input
from quiz_app.load_test.manifest import LoadTestManifest
from quiz_app.load_test.metrics_export import _serialize_csv
from quiz_app.load_test.orchestrator import RampReady
from quiz_app.load_test.orchestrator_acceptance import evaluate_acceptance
from quiz_app.load_test.orchestrator_contracts import (
    OperatorCredentials,
    OrchestratorError,
    RunResult,
)
from quiz_app.load_test.verification_contracts import ArtifactIdentity
from quiz_app.load_test_aws.models import AwsPreparationEvidence
from quiz_app.operator.control_contracts import OperatorMutationResponse


class OracleApi(ClockedWorkflowApi):
    def post_mutation(
        self, operation: str, round_id: UUID, body: bytes
    ) -> OperatorMutationResponse:
        response = super().post_mutation(operation, round_id, body)
        if operation == "advance_round":
            self.round_id = _round_id(self.manifest, self.round_number)
            response = response.model_copy(update={"round_id": self.round_id})
        return response


Inventory = tuple[LoadTestManifest, AwsPreparationEvidence, tuple[CollectedFile, ...]]


@pytest.fixture(scope="module")
def complete_inventory(tmp_path_factory: pytest.TempPathFactory) -> Inventory:
    directory = tmp_path_factory.mktemp("collected-complete")
    runner = _bare_runner(_files(directory))
    manifest = _manifest().model_copy(update={"public_origin": "https://10.0.1.20"})
    runner.manifest = manifest
    context = manifest_context(manifest)
    runner.clock = AdvancingClock(manifest.scheduled_start_at + timedelta(seconds=600))
    runner.credentials = OperatorCredentials(username="operator", password=SecretStr("test"))
    runner.prepared = _prepared().model_copy(
        update={
            "manifest_sha256": context.manifest_sha256,
            "prepared_at": manifest.created_at,
            "expires_at": manifest.scheduled_start_at + timedelta(hours=3),
        }
    )
    api = OracleApi(runner.clock)
    api.manifest, api.round_id = manifest, manifest.initial_round.id
    runner.http = api
    files = runner.files
    runner.database = MagicMock()

    def seal(status: ControllerStatus, *, mode: str) -> ArtifactIdentity:
        return seal_db_verifier_input(
            manifest, status, files.output("requests"), files.output("db-inputs"), mode=mode
        )

    def verify_round(status: ControllerStatus, generator: Path) -> DbVerifierRunResult:
        artifact = seal(status, mode="per-round")
        rows = _round_results(manifest, status.round_number)
        answers = tuple(
            answer
            for number in range(1, status.round_number + 1)
            for answer in _answers(manifest, number)
        )
        ledgers = tuple(
            ledger
            for number in range(1, status.round_number + 1)
            for ledger in _ledgers(manifest, number, _answers(manifest, number))
        )
        rows["ranking_consistency"] = _ranking(
            answers,
            ledgers,
            round_id=_round_id(manifest, status.round_number),
            version=status.round_number,
        )
        return run_db_verifier(
            manifest,
            load_db_verifier_request(files.output("db-inputs") / artifact.name),
            _FakeDatabase(rows),
            files.output("db-reports"),
            generator_evidence_path=generator,
            clock=runner.clock.now,
        )

    def lock_observation(_function: object, number: int) -> Future[DbVerifierRunResult]:
        artifact = seal_lock_observation_input(
            manifest, files.output("db-inputs"), round_number=number
        )
        start = manifest.scheduled_start_at + timedelta(
            seconds=manifest.scenario.rounds[number - 1].start_offset_seconds
        )
        facts = LockObservationFacts(
            sample_interval_seconds=1,
            sample_count=16,
            first_observed_at=start,
            last_observed_at=start + timedelta(seconds=15),
            peak_total_lock_count=0,
            peak_waiting_lock_count=0,
            peak_waiting_session_count=0,
            peak_active_transaction_count=0,
            peak_idle_in_transaction_count=0,
            peak_oldest_transaction_age_ms=Decimal(0),
            final_waiting_lock_count=0,
            final_waiting_session_count=0,
            peak_interpretation="calibration-only",
        )
        report = LockObservationReport(
            format="load-test-db-lock-observation-report-v1",
            mode="observe-locks",
            **context.model_dump(
                include={
                    "run_id",
                    "event_id",
                    "source_commit",
                    "schema_revision",
                    "manifest_sha256",
                    "scenario_sha256",
                }
            ),
            round_number=number,
            input_artifact=artifact,
            query_executions=(),
            facts=facts,
            decision="PASS",
            failure_codes=(),
            sealed_at=start + timedelta(seconds=15),
        )
        identity = write_canonical_artifact(
            files.output("db-reports"),
            f"db-lock-observation-round-{number:02d}-{manifest.run_id}.json",
            report,
        )
        result: Future[DbVerifierRunResult] = Future()
        result.set_result(
            DbVerifierRunResult(
                mode="observe-locks", decision="PASS", report=identity, receipt=None
            )
        )
        return result

    runner.database.seal.side_effect, runner.database.verify_round.side_effect = seal, verify_round
    workers = MagicMock()
    workers.submit.side_effect = lock_observation
    for number in range(1, 11):
        evidence = _generator_evidence(manifest, number).model_copy(
            update={"round_id": _round_id(manifest, number)}
        )
        write_canonical_artifact(
            files.output("generator"), f"generator-round-{number:02d}.json", evidence
        )
    with ControllerJournal(
        files.output("journal") / "controller.jsonl", files.repository_root
    ) as journal:
        controller = LoadTestController(
            manifest,
            journal,
            files.output("checkpoints"),
            files.output("requests"),
            files.repository_root,
            api,
            FileVerificationProvider(),
        )
        runner._action(controller, "prepare")
        for number in range(1, 11):
            final_input = runner._round(controller, workers, number)
        final = run_db_verifier(
            manifest,
            load_db_verifier_request(files.output("db-inputs") / final_input.name),
            _FakeDatabase(_final_results(manifest)),
            files.output("db-reports"),
            generator_evidence_path=files.output("generator") / "generator-round-10.json",
            clock=runner.clock.now,
        )
        assert final.decision == "PASS"
        runner.artifacts.append(final.report)
    write_canonical_artifact(files.root / "inputs", "manifest.json", manifest)
    write_canonical_artifact(files.root / "inputs", "preparation.json", runner.prepared)
    ramp = RampReady(
        format="load-test-generator-ramp-ready-v1",
        run_id=manifest.run_id,
        event_id=manifest.event.id,
        manifest_sha256=context.manifest_sha256,
        sessions=600,
        websocket_connected=600,
        observed_at_unix_ms=int(
            (manifest.scheduled_start_at + timedelta(seconds=600)).timestamp() * 1000
        ),
    )
    write_canonical_artifact(files.output("generator"), "generator-ramp-ready.json", ramp)
    metrics = _metrics().model_copy(update={"manifest_sha256": context.manifest_sha256})
    exported = _export()
    write_canonical_artifact(files.output("generator"), "generator-metrics-summary.json", metrics)
    prefix = f"metrics-{manifest.run_id}"
    json_body = canonical_json_bytes(exported) + b"\n"
    csv_body = _serialize_csv(exported)
    for suffix, body in (
        ("json", json_body),
        ("csv", csv_body),
        (
            "sha256",
            (
                f"{sha256(json_body).hexdigest()}  {prefix}.json\n"
                f"{sha256(csv_body).hexdigest()}  {prefix}.csv\n"
            ).encode(),
        ),
    ):
        path = files.output("app-export") / f"{prefix}.{suffix}"
        path.write_bytes(body)
        path.chmod(0o600)
    fault_path = files.output("app-faults") / "fault.jsonl"
    status, _ = _run(
        manifest, _identity(manifest), FakeHost(FakeClock(manifest.scheduled_start_at)), fault_path
    )
    write_canonical_artifact(files.output("app-faults"), "fault-status.json", status)
    write_canonical_artifact(
        files.output("results"), "acceptance.json", evaluate_acceptance(manifest, metrics, exported)
    )
    runner.clock.value = manifest.scheduled_start_at + timedelta(seconds=4801)
    runner.result("PASS")
    return manifest, runner.prepared, collect_inventory(files, manifest, runner.prepared)


def test_complete_real_controller_database_and_fault_inventory_is_revalidated(
    complete_inventory: Inventory,
) -> None:
    manifest, prepared, items = complete_inventory
    assert validate_inventory(items, manifest, prepared).decision == "PASS"


@pytest.mark.parametrize(
    "name",
    [
        "final-report",
        "lock-observation",
        "verification-round-03-report",
        "controller.jsonl",
        "fault.jsonl",
    ],
)
def test_missing_required_proof_cannot_be_collected_as_pass(
    complete_inventory: Inventory, name: str
) -> None:
    manifest, prepared, items = complete_inventory
    omitted = {item.identity for item in items if name not in item.path}
    changed = tuple(item for item in items if item.identity in omitted)
    # 最終report/receipt自体をresultの参照からも落として、単なるhash照合では防げない欠落を再現する。
    if name == "final-report":
        changed = tuple(item for item in changed if "final-receipt" not in item.path)
    identities = {item.identity for item in changed}
    changed = tuple(
        item.model_copy(
            update={
                "body": canonical_json_bytes(
                    RunResult.model_validate_json(item.body).model_copy(
                        update={
                            "artifacts": tuple(
                                value
                                for value in RunResult.model_validate_json(item.body).artifacts
                                if value in identities
                            )
                        }
                    )
                )
                + b"\n"
            }
        )
        if item.path == "results/result.json"
        else item
        for item in changed
    )
    with pytest.raises((ValueError, OrchestratorError)):
        validate_inventory(changed, manifest, prepared)


@pytest.mark.parametrize("suffix", ["csv", "sha256"])
def test_unpaired_or_foreign_metrics_bytes_cannot_carry_free_text(suffix: str) -> None:
    manifest, prepared, _, items = fixture_items()
    extra = CollectedFile(
        path=f"app-export/metrics-{manifest.run_id}.{suffix}", body=b"secret_canary\n"
    )
    with pytest.raises(OrchestratorError, match="metrics_incomplete"):
        validate_inventory((*items, extra), manifest, prepared)


def test_duplicate_json_key_cannot_hide_discarded_secret():
    manifest, prepared, _, items = fixture_items()
    changed = items[-1].model_copy(
        update={"body": items[-1].body.replace(b"{", b'{"decision":"secret_canary",', 1)}
    )
    with pytest.raises(OrchestratorError, match="duplicate_json_key"):
        validate_inventory((*items[:-1], changed), manifest, prepared)


@pytest.mark.parametrize("fault", ["journal_hash", "fault_status", "acceptance"])
def test_passing_shapes_do_not_replace_controller_fault_and_acceptance_proof(
    complete_inventory: Inventory, fault: str
) -> None:
    manifest, prepared, items = complete_inventory
    target = {
        "journal_hash": "journal/controller.jsonl",
        "fault_status": "app-faults/fault-status.json",
        "acceptance": "results/acceptance.json",
    }[fault]
    changed = []
    for item in items:
        if item.path != target:
            changed.append(item)
            continue
        if fault == "journal_hash":
            lines = item.body.splitlines()
            record = json.loads(lines[-1])
            record["entry_sha256"] = "0" * 64
            lines[-1] = canonical_json_bytes(record)
            body = b"\n".join(lines) + b"\n"
        else:
            payload = json.loads(item.body)
            if fault == "fault_status":
                payload.update(status="waiting", next_slot=1)
            else:
                payload["generator_server_failures"] = 1
            body = canonical_json_bytes(payload) + b"\n"
        changed.append(item.model_copy(update={"body": body}))
    with pytest.raises(OrchestratorError):
        validate_inventory(tuple(changed), manifest, prepared)


@pytest.mark.parametrize("kind", ["naive", "before", "expired"])
def test_result_completion_time_must_be_aware_and_within_run(kind: str) -> None:
    manifest, prepared, result, items = fixture_items()
    finished = {
        "naive": result.finished_at.replace(tzinfo=None),
        "before": prepared.prepared_at - timedelta(seconds=1),
        "expired": prepared.expires_at,
    }[kind]
    changed = items[-1].model_copy(
        update={
            "body": canonical_json_bytes(result.model_copy(update={"finished_at": finished}))
            + b"\n"
        }
    )
    with pytest.raises(OrchestratorError, match="result_time_invalid"):
        validate_inventory((*items[:-1], changed), manifest, prepared)


def test_query_metadata_cannot_carry_arbitrary_text(complete_inventory: Inventory) -> None:
    manifest, prepared, items = complete_inventory
    changed = []
    for item in items:
        if "verification-round-01-report" in item.path:
            payload = json.loads(item.body)
            payload["query_executions"][0]["query_id"] = "secret_canary"
            item = item.model_copy(update={"body": canonical_json_bytes(payload) + b"\n"})
        changed.append(item)
    with pytest.raises(OrchestratorError, match="query_id_invalid"):
        validate_inventory(tuple(changed), manifest, prepared)
