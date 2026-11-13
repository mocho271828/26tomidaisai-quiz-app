import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError
from test_load_test_controller import (
    REPOSITORY_ROOT,
    WorkflowApi,
    _directories,
    _manifest,
    _write_evidence_files,
)

from quiz_app.load_test.controller import LoadTestController, _journal_progress
from quiz_app.load_test.controller_contracts import (
    ControllerAction,
    ControllerRefusal,
    ControllerStatus,
    MutationPlannedPayload,
    ResultsObservedPayload,
    manifest_context,
    required_confirmation_phrase,
)
from quiz_app.load_test.controller_journal import ControllerJournal
from quiz_app.load_test.controller_verification import FileVerificationProvider
from quiz_app.load_test.legacy_control_contracts import (
    OperatorStateResponse,
)
from quiz_app.load_test.manifest import LoadTestManifest
from quiz_app.models.enums import RoundState
from quiz_app.operator.control_contracts import (
    OperatorLatestRankingResponse,
)


@dataclass
class Workflow:
    manifest: LoadTestManifest
    api: WorkflowApi
    controller: LoadTestController
    journal: ControllerJournal
    generator: Path
    requests: Path

    def execute(self, action: ControllerAction = "wait-results") -> ControllerStatus:
        return self.controller.execute(
            action,
            username="operator",
            password=SecretStr("secret"),
            confirmation_phrase=required_confirmation_phrase(
                manifest_context(self.manifest), 1, action
            ),
            generator_evidence_path=self.generator,
        )

    def publish(self) -> None:
        self.api.now = self.manifest.scheduled_start_at + timedelta(seconds=930)
        self.api.aggregate_automatically()
        assert self.execute("publish").next_action == "wait-results"


@pytest.fixture
def workflow(tmp_path: Path) -> Iterator[Workflow]:
    manifest = _manifest()
    journal_dir, checkpoints, requests = _directories(tmp_path)
    generator = _write_evidence_files(tmp_path, manifest)
    api = WorkflowApi(manifest, manifest.scheduled_start_at + timedelta(seconds=700))
    with ControllerJournal(journal_dir / "controller.jsonl", REPOSITORY_ROOT) as journal:
        controller = LoadTestController(
            manifest,
            journal,
            checkpoints,
            requests,
            REPOSITORY_ROOT,
            api,
            FileVerificationProvider(),
        )
        flow = Workflow(manifest, api, controller, journal, generator, requests)
        flow.execute("prepare")
        api.stage = "ready"
        api.version += 1
        api.event_version += 1
        api.now = manifest.scheduled_start_at + timedelta(seconds=895)
        assert flow.execute("schedule").next_action == "publish"
        yield flow


def test_waiting_then_published_records_observation_without_mutation(
    workflow: Workflow,
) -> None:
    head = workflow.journal.head_sha256
    for stage, offset in (("scheduled", 900), ("awaiting", 930)):
        workflow.api.stage = stage
        workflow.api.now = workflow.manifest.scheduled_start_at + timedelta(seconds=offset)
        status = workflow.execute("publish")
        assert status.next_action == "publish"
        assert status.verification_request_artifact is None
        assert workflow.journal.head_sha256 == head
    workflow.publish()
    status = workflow.execute()
    assert status.next_action == "advance"
    assert status.verification_request_artifact is not None
    assert len(workflow.api.bodies) == 3
    observed = [r.payload for r in workflow.journal.records if r.payload.kind == "results_observed"]
    assert len(observed) == 1
    assert observed[0].round_version == workflow.api.version
    assert b"public_label" not in workflow.journal.path.read_bytes()


@pytest.mark.parametrize("fault", ["event", "round", "version", "timing", "void"])
def test_results_reject_live_drift_without_mutation(
    workflow: Workflow, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    workflow.publish()
    valid = workflow.api.get_event_state()
    current = valid.current_round
    assert current is not None
    if fault == "event":
        state = valid.model_copy(update={"event_id": uuid4()})
    else:
        updates = {
            "round": {"round_id": uuid4()},
            "version": {"version": current.version + 1},
            "timing": {"standard_close_at": current.standard_close_at + timedelta(seconds=1)},
            "void": {
                "state": RoundState.VOID_CANDIDATE,
                "result_failure_code": "automatic_aggregate_integrity_failure",
            },
        }
        state = valid.model_copy(
            update={"current_round": current.model_copy(update=updates[fault])}
        )
    monkeypatch.setattr(workflow.api, "get_event_state", lambda: state)
    expected = (
        "controller_automatic_results_failed" if fault == "void" else "controller_live_state_drift"
    )
    with pytest.raises(ControllerRefusal, match=expected):
        workflow.execute()
    assert len(workflow.api.bodies) == 3
    assert not list(workflow.requests.iterdir())


@pytest.mark.parametrize("fault", ["missing", "round", "snapshot", "count", "duplicate"])
def test_results_reject_unbound_ranking(
    workflow: Workflow, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    workflow.publish()
    ranking = workflow.api.get_latest_ranking()
    assert ranking is not None
    invalid: OperatorLatestRankingResponse | None
    if fault == "missing":
        invalid = None
    else:
        updates = {
            "round": {"round_id": uuid4()},
            "snapshot": {"snapshot_version": 2},
            "count": {"rows": []},
            "duplicate": {"rows": [ranking.rows[0]] * len(ranking.rows)},
        }
        invalid = ranking.model_copy(update=updates[fault])
    monkeypatch.setattr(workflow.api, "get_latest_ranking", lambda: invalid)
    with pytest.raises(ControllerRefusal, match="controller_results_ranking_mismatch"):
        workflow.execute()
    assert len(workflow.api.bodies) == 3
    assert not list(workflow.requests.iterdir())


@pytest.mark.parametrize("mixed", ["event_version", "snapshot_version"])
@pytest.mark.parametrize("persistent", [False, True])
def test_result_projection_race_has_one_bounded_retry(
    workflow: Workflow, monkeypatch: pytest.MonkeyPatch, mixed: str, persistent: bool
) -> None:
    workflow.api.now = workflow.manifest.scheduled_start_at + timedelta(seconds=930)
    workflow.api.aggregate_automatically()
    published = workflow.api.get_event_state()
    if mixed == "event_version":
        crossed = published.model_copy(update={"event_version": published.event_version - 1})
    else:
        crossed = published.model_copy(update={"latest_ranking_snapshot_version": None})
    reads = 0
    live_state = workflow.api.get_event_state

    def get_state() -> OperatorStateResponse:
        nonlocal reads
        reads += 1
        return crossed if persistent or reads == 1 else live_state()

    monkeypatch.setattr(workflow.api, "get_event_state", get_state)
    if persistent:
        with pytest.raises(
            ControllerRefusal,
            match="controller_(live_state_drift|results_ranking_mismatch)",
        ):
            workflow.execute("publish")
        assert reads == 2
        assert not list(workflow.requests.iterdir())
    else:
        assert workflow.execute("publish").next_action == "wait-results"
        assert reads == 3
    assert len(workflow.api.bodies) == (2 if persistent else 3)


def test_crash_after_results_observation_resumes_without_duplicate(
    workflow: Workflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow.publish()
    finalize = workflow.controller._finalize_verification_request

    def crash(*_args: object) -> None:
        raise RuntimeError("crash after observation")

    monkeypatch.setattr(workflow.controller, "_finalize_verification_request", crash)
    with pytest.raises(RuntimeError, match="crash after observation"):
        workflow.execute()
    assert workflow.journal.records[-1].payload.kind == "results_observed"
    monkeypatch.setattr(workflow.controller, "_finalize_verification_request", finalize)
    assert workflow.execute().next_action == "advance"
    assert sum(r.payload.kind == "results_observed" for r in workflow.journal.records) == 1
    assert len(workflow.api.bodies) == 3


@pytest.mark.parametrize(
    "fault", ["round_version", "event_version", "ranking_snapshot_version", "round_id"]
)
def test_journal_rejects_forged_result_observation(workflow: Workflow, fault: str) -> None:
    workflow.publish()
    workflow.execute()
    records = workflow.journal.records.copy()
    index = next(i for i, record in enumerate(records) if record.payload.kind == "results_observed")
    payload = records[index].payload
    assert isinstance(payload, ResultsObservedPayload)
    update = {fault: uuid4() if fault == "round_id" else getattr(payload, fault) + 1}
    records[index] = records[index].model_copy(
        update={"payload": payload.model_copy(update=update)}
    )
    with pytest.raises(ControllerRefusal, match="controller_journal_state_invalid"):
        _journal_progress(workflow.manifest, manifest_context(workflow.manifest), records)


def test_unknown_operation_cannot_be_loaded(workflow: Workflow) -> None:
    plan = next(r.payload for r in workflow.journal.records if r.payload.kind == "mutation_planned")
    raw = plan.model_dump(mode="json")
    assert MutationPlannedPayload.model_validate_json(json.dumps(raw)) == plan
    raw["operation"] = "unknown_operation"
    with pytest.raises(ValidationError):
        MutationPlannedPayload.model_validate_json(json.dumps(raw))


def test_result_resume_cannot_seal_after_slot_deadline(
    workflow: Workflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow.publish()
    finalize = workflow.controller._finalize_verification_request

    def crash(*_args: object) -> None:
        raise RuntimeError("crash after observation")

    monkeypatch.setattr(workflow.controller, "_finalize_verification_request", crash)
    with pytest.raises(RuntimeError, match="crash after observation"):
        workflow.execute()
    head = workflow.journal.head_sha256
    monkeypatch.setattr(workflow.controller, "_finalize_verification_request", finalize)
    workflow.api.now = workflow.manifest.scheduled_start_at + timedelta(seconds=1260)
    with pytest.raises(ControllerRefusal, match="controller_results_deadline_missed"):
        workflow.execute()
    assert workflow.journal.head_sha256 == head
    assert not list(workflow.requests.iterdir())
    assert len(workflow.api.bodies) == 3


def test_slow_request_seal_cannot_report_ready_to_advance(
    workflow: Workflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow.publish()
    original = FileVerificationProvider.seal_verification_request

    def slow_seal(self: FileVerificationProvider, *args: object, **kwargs: object) -> object:
        # I/O後に期限が過ぎた状況を、実際の非上書きartifact保存を保って再現する。
        artifact = original(self, *args, **kwargs)  # type: ignore[arg-type]
        workflow.api.now = workflow.manifest.scheduled_start_at + timedelta(seconds=1260)
        return artifact

    monkeypatch.setattr(FileVerificationProvider, "seal_verification_request", slow_seal)
    with pytest.raises(ControllerRefusal, match="controller_verification_request_deadline_missed"):
        workflow.execute()
    assert workflow.journal.records[-1].payload.kind == "verification_requested"
    assert len(workflow.api.bodies) == 3


def test_readonly_wait_cannot_publish_an_aggregated_round(workflow: Workflow) -> None:
    workflow.api.now = workflow.manifest.scheduled_start_at + timedelta(seconds=930)
    workflow.api.aggregate_automatically()
    before = list(workflow.api.bodies)
    with pytest.raises(ControllerRefusal, match="controller_action_out_of_order"):
        workflow.execute("wait-results")
    assert workflow.api.bodies == before
    assert workflow.api.stage == "aggregated"
