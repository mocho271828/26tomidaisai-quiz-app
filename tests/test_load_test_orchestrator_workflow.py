from __future__ import annotations

from concurrent.futures import Future
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from pydantic import SecretStr
from tests.test_load_test_controller import REPOSITORY_ROOT, WorkflowApi, _manifest
from tests.test_load_test_controller_verification import _generator_evidence
from tests.test_load_test_orchestrator import AdvancingClock, _bare_runner, _files

from quiz_app.audience.contracts import TimeResponse
from quiz_app.contracts import ReadinessHealthResponse
from quiz_app.load_test.controller import ControllerRefusal, LoadTestController
from quiz_app.load_test.controller_contracts import ControllerStatus
from quiz_app.load_test.controller_journal import ControllerJournal
from quiz_app.load_test.controller_verification import FileVerificationProvider
from quiz_app.load_test.db_verifier_artifacts import write_canonical_artifact
from quiz_app.load_test.db_verifier_models import DbVerifierRunResult
from quiz_app.load_test.evidence_handoff import load_bound_verification_request
from quiz_app.load_test.legacy_control_contracts import (
    OperatorStateResponse,
)
from quiz_app.load_test.orchestrator_contracts import (
    OperatorCredentials,
    OrchestratorError,
    RunFiles,
)
from quiz_app.load_test.runtime_configuration import FORMAL_RESULT_VISIBILITY
from quiz_app.load_test.verification_contracts import ArtifactIdentity, RoundVerificationReceipt
from quiz_app.models.enums import EventState, RoundResultPublication, RoundState
from quiz_app.operator.control_contracts import (
    OperatorMutationResponse,
)


class ClockedWorkflowApi(WorkflowApi):
    """外部HTTPだけを置換し、controllerのphase・journal・handoffは実装を通す。"""

    def __init__(self, clock: AdvancingClock) -> None:
        super().__init__(_manifest(), clock.now())
        self.clock = clock
        self.redis_recovery_offset = 2590

    def get_readiness(self) -> ReadinessHealthResponse:
        offset = (self.clock.now() - self.manifest.scheduled_start_at).total_seconds()
        if 2280 <= offset < self.redis_recovery_offset:
            return ReadinessHealthResponse(
                status="degraded", database="ok", migration="current", redis="degraded"
            )
        return super().get_readiness()

    def get_server_time(self) -> TimeResponse:
        self.now = self.clock.now()
        return super().get_server_time()

    def get_event_state(self) -> OperatorStateResponse:
        self.now = self.clock.now()
        slot = self.manifest.scenario.rounds[self.round_number - 1]
        offset = (self.now - self.manifest.scheduled_start_at).total_seconds()
        if self.stage == "preparing" and offset >= slot.start_offset_seconds - 20:
            self.stage = "ready"
            self.version += 1
            self.event_version += 1
        if self.stage == "scheduled" and offset >= slot.start_offset_seconds + 20:
            self.aggregate_automatically()
        state = super().get_event_state()
        current = state.current_round
        assert current is not None
        start = self.manifest.scheduled_start_at + timedelta(seconds=slot.start_offset_seconds)
        if self.stage in {"scheduled", "awaiting", "aggregated", "reveal", "completed"}:
            current = current.model_copy(
                update={
                    "open_at": start,
                    "standard_close_at": start + timedelta(seconds=10),
                }
            )
        if self.stage == "completed":
            current = current.model_copy(
                update={
                    "state": RoundState.ROUND_BACKUP_VALIDATED,
                    "result_publication": RoundResultPublication.PUBLISHED,
                }
            )
            state = state.model_copy(update={"event_state": EventState.COMPLETED})
        return state.model_copy(update={"current_round": current})

    def post_mutation(
        self, operation: str, round_id: UUID, body: bytes
    ) -> OperatorMutationResponse:
        if operation == "complete_event":
            assert round_id == self.round_id
            self.bodies.append(body)
            self.version += 1
            self.event_version += 1
            self.stage = "completed"
            return OperatorMutationResponse(
                operation="complete_event",
                event_id=self.manifest.event.id,
                event_version=self.event_version,
                round_id=self.round_id,
                round_version=self.version,
                round_state=RoundState.ROUND_BACKUP_VALIDATED,
                result_visibility=FORMAL_RESULT_VISIBILITY,
            )
        response = super().post_mutation(operation, round_id, body)
        if operation == "schedule_round":
            start = self.manifest.scheduled_start_at + timedelta(
                seconds=self.manifest.scenario.rounds[self.round_number - 1].start_offset_seconds
            )
            return response.model_copy(
                update={
                    "open_at": start,
                    "standard_close_at": start + timedelta(seconds=10),
                }
            )
        return response


def seal_test_receipt(
    files: RunFiles, status: ControllerStatus, api: ClockedWorkflowApi
) -> DbVerifierRunResult:
    request = load_bound_verification_request(files.output("requests"), status, api.manifest)
    report = write_canonical_artifact(
        files.output("db-reports"),
        f"test-report-{status.round_number:02d}.json",
        {"decision": "PASS"},
    )
    fields = set(RoundVerificationReceipt.model_fields) - {
        "format",
        "report",
        "decision",
        "failure_codes",
        "sealed_at",
    }
    receipt = RoundVerificationReceipt(
        **request.model_dump(include=fields),
        format="load-test-round-verification-receipt-v2",
        report=report,
        decision="PASS",
        failure_codes=(),
        sealed_at=api.clock.now(),
    )
    artifact = write_canonical_artifact(
        files.output("db-reports"), f"test-receipt-{status.round_number:02d}.json", receipt
    )
    return DbVerifierRunResult(mode="per-round", decision="PASS", report=report, receipt=artifact)


@pytest.mark.parametrize("recovery_offset", [2590, 2595, 2596])
def test_ten_rounds_use_real_controller_journal_and_receipts_through_fault_recovery(
    tmp_path: Path, recovery_offset: int
) -> None:
    runner = _bare_runner(_files(tmp_path))
    runner.manifest = _manifest()
    runner.clock = AdvancingClock(runner.manifest.scheduled_start_at + timedelta(seconds=600))
    runner.credentials = OperatorCredentials(username="operator", password=SecretStr("test"))
    api = ClockedWorkflowApi(runner.clock)
    api.redis_recovery_offset = recovery_offset
    runner.http = api
    runner.database = MagicMock()
    lock: Future[DbVerifierRunResult] = Future()
    lock.set_result(
        DbVerifierRunResult(
            mode="observe-locks",
            decision="PASS",
            receipt=None,
            report=ArtifactIdentity(name="locks.json", sha256="a" * 64, size_bytes=1),
        )
    )
    workers = MagicMock()
    workers.submit.return_value = lock
    for number in range(1, 11):
        write_canonical_artifact(
            runner.files.output("generator"),
            f"generator-round-{number:02d}.json",
            _generator_evidence(runner.manifest, number),
        )
    with ControllerJournal(
        runner.files.output("journal") / "controller.jsonl", REPOSITORY_ROOT
    ) as journal:
        controller = LoadTestController(
            runner.manifest,
            journal,
            runner.files.output("checkpoints"),
            runner.files.output("requests"),
            REPOSITORY_ROOT,
            api,
            FileVerificationProvider(),
        )

        def verify_round(status: ControllerStatus, _generator: Path) -> DbVerifierRunResult:
            result = seal_test_receipt(runner.files, status, api)
            if status.round_number == 5:
                assert api.get_readiness().redis == "degraded"
                assert result.receipt is not None
                # 読取後のadvance待ちは許容しても、故障中の実mutationは拒否する。
                with pytest.raises(ControllerRefusal, match="health_preflight"):
                    runner._action(
                        controller,
                        "advance",
                        receipt=runner.files.output("db-reports") / result.receipt.name,
                    )
                with pytest.raises(ControllerRefusal, match="fault_recovery_not_reached"):
                    controller._validate_pre_dispatch_time(5, "advance_round")
            return result

        runner.database.verify_round.side_effect = verify_round
        runner._action(controller, "prepare")
        for number in range(1, 11):
            if number == 5 and recovery_offset == 2596:
                with pytest.raises(OrchestratorError, match="phase_deadline"):
                    runner._round(controller, workers, number)
                assert api.round_number == 5
                assert runner.completed_rounds == 4
                return
            final = runner._round(controller, workers, number)
            if number < 10:
                assert final is None
                assert api.round_number == number + 1
                assert "SCHEDULE" in controller.confirmation_prompt("schedule")
            else:
                assert final == runner.database.seal.return_value
            if number == 5:
                assert (
                    runner.clock.now() - runner.manifest.scheduled_start_at
                ).total_seconds() == recovery_offset
        operations = [
            record.payload.operation
            for record in journal.records
            if record.payload.kind == "mutation_planned"
        ]
        assert operations == [
            "begin_preparation",
            "schedule_round",
            "publish_round_results",
            "advance_round",
        ] * 9 + [
            "begin_preparation",
            "schedule_round",
            "publish_round_results",
            "complete_event",
        ]
        assert len(tuple(runner.files.output("checkpoints").iterdir())) == 10
        assert api.get_event_state().event_state is EventState.COMPLETED
