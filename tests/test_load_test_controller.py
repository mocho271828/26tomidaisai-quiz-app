from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from itertools import islice, product
from pathlib import Path
from typing import cast
from uuid import UUID, uuid5

import pytest
from pydantic import SecretStr

from quiz_app.audience.contracts import TimeResponse
from quiz_app.contracts import LiveHealthResponse, ReadinessHealthResponse
from quiz_app.load_test.controller import (
    ControllerRefusal,
    LoadTestController,
    _journal_progress,
    _mutation_request,
    _request_body,
)
from quiz_app.load_test.controller_contracts import (
    CheckpointSealedPayload,
    ControllerCheckpoint,
    ControllerOperation,
    InitializedPayload,
    MutationFailedPayload,
    MutationPlannedPayload,
    MutationSucceededPayload,
    ReceiptAcceptedPayload,
    ResultsObservedPayload,
    VerificationRequestedPayload,
    VerificationRequestSealedPayload,
    canonical_json_bytes,
    manifest_context,
    required_confirmation_phrase,
    verification_id,
)
from quiz_app.load_test.controller_journal import ControllerJournal
from quiz_app.load_test.controller_verification import (
    FileVerificationProvider,
    GeneratorRoundEvidence,
    RoundOptionCounts,
    RoundVerificationRequest,
    VerificationHandoffError,
)
from quiz_app.load_test.legacy_control_contracts import (
    OperatorGateResponse,
    OperatorRoundResponse,
    OperatorStateResponse,
)
from quiz_app.load_test.manifest import LoadTestManifest, build_load_test_manifest
from quiz_app.load_test.runtime_configuration import FORMAL_RESULT_VISIBILITY
from quiz_app.load_test.verification_contracts import (
    ArtifactIdentity,
)
from quiz_app.models.enums import (
    AdminRole,
    EventState,
    RoundResultPublication,
    RoundState,
)
from quiz_app.operator.contracts import (
    OperatorIdentityResponse,
    OperatorSessionResponse,
)
from quiz_app.operator.control_contracts import (
    OperatorLatestRankingResponse,
    OperatorMutationResponse,
    OperatorOptionResponse,
    OperatorOptionStatisticsResponse,
    OperatorQuestionPreviewResponse,
    OperatorQuestionResponse,
    OperatorRankingRowResponse,
    OperatorRoundStatisticsResponse,
)

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
EVENT_ID = UUID("22222222-2222-4222-8222-222222222222")
ACCOUNT_ID = UUID("33333333-3333-4333-8333-333333333333")
CREATED_AT = datetime(2026, 9, 3, tzinfo=UTC)
IMAGE = f"registry.example.test/quiz/api@sha256:{'1' * 64}"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _artifact(name: str, digit: str = "1") -> ArtifactIdentity:
    return ArtifactIdentity(name=name, sha256=digit * 64, size_bytes=100)


def _manifest() -> LoadTestManifest:
    return build_load_test_manifest(
        run_id=RUN_ID,
        event_id=EVENT_ID,
        event_slug="load-test-2026-09-03",
        scheduled_start_at=CREATED_AT + timedelta(hours=1),
        public_origin="https://quiz.aws.internal",
        source_commit="a" * 40,
        api_image=IMAGE,
        gateway_image=IMAGE,
        generator_image=IMAGE,
        scoring_policy_version="scoring-v1",
        scoring_base_points=10_000,
        scoring_initial_speed_bonus=500,
        scoring_speed_bucket_ms=100,
        scoring_speed_decrement_per_bucket=10,
        scoring_standard_duration_ms=10_000,
        scoring_is_approved=True,
        display_name_policy_version="display-v1",
        display_notice_version="notice-v1",
        display_max_code_points=20,
        display_reject_patterns_json="[]",
        created_at=CREATED_AT,
    )


def _state(
    manifest: LoadTestManifest,
    now: datetime,
    *,
    version: int = 1,
    event_version: int | None = None,
    round_number: int = 1,
    round_id: UUID | None = None,
) -> OperatorStateResponse:
    previews = [
        OperatorQuestionPreviewResponse(
            question_id=question.id,
            version=question.version,
            display_order=question.display_order,
            prompt=question.prompt,
            options=[
                OperatorOptionResponse(
                    option_id=option.id,
                    label=option.label,
                    content=option.content,
                    is_correct=option.label == "B",
                )
                for option in question.options
            ],
        )
        for question in manifest.questions
    ]
    question = manifest.questions[round_number - 1]
    current = OperatorRoundResponse(
        round_id=round_id or manifest.initial_round.id,
        version=version,
        state_version=version * 4,
        state=RoundState.PREPARING_ASSETS,
        open_at=None,
        standard_close_at=None,
        result_publication=RoundResultPublication.WITHHELD,
        result_failure_code=None,
        can_publish_results=False,
        statistics=OperatorRoundStatisticsResponse(
            answer_count=0,
            average_response_ms=None,
            options=[
                OperatorOptionStatisticsResponse(
                    option_id=option.id,
                    answer_count=0,
                    average_response_ms=None,
                )
                for option in question.options
            ],
        ),
        gate=OperatorGateResponse(
            target_count=0,
            ready_count=0,
            asset_failure_count=0,
            can_schedule=False,
            blocking_reasons=["preparation_target_required"],
        ),
        question=OperatorQuestionResponse(
            question_id=question.id,
            version=question.version,
            prompt=question.prompt,
            options=previews[round_number - 1].options,
        ),
    )
    return OperatorStateResponse(
        award_ceremony_phase=None,
        event_id=manifest.event.id,
        projected_at=now + timedelta(milliseconds=100),
        event_title=manifest.event.title,
        event_state=EventState.LOBBY_OPEN,
        event_version=event_version if event_version is not None else version,
        result_visibility=FORMAL_RESULT_VISIBILITY,
        start_delay_seconds=5,
        visibility_controls={
            target: {"mode": "manual", "initially_visible": visible, "changes": ()}
            for target, visible in (
                ("distribution", True),
                ("personal_ranking", False),
                ("overall_ranking", False),
            )
        },
        participant_count=600,
        available_questions=previews,
        used_question_ids=[used_question.id for used_question in manifest.questions[:round_number]],
        current_round=current,
        latest_ranking_snapshot_version=None,
    )


class FakeApi:
    def __init__(
        self,
        manifest: LoadTestManifest,
        now: datetime,
        *,
        account_id: UUID = ACCOUNT_ID,
        role: AdminRole = AdminRole.OPERATOR,
        fail_once: bool = False,
    ) -> None:
        self.manifest = manifest
        self.now = now
        self.account_id = account_id
        self.role = role
        self.fail_once = fail_once
        self.version = 1
        self.event_version = 1
        self.bodies: list[bytes] = []

    def login(self, username: str, _password: SecretStr) -> OperatorSessionResponse:
        return OperatorSessionResponse(
            operator=OperatorIdentityResponse(
                account_id=self.account_id,
                username=username.strip(),
                role=self.role,
            )
        )

    def get_session(self) -> OperatorSessionResponse:
        return self.login("operator", SecretStr("unused"))

    def get_liveness(self) -> LiveHealthResponse:
        return LiveHealthResponse(version="a" * 40)

    def get_readiness(self) -> ReadinessHealthResponse:
        return ReadinessHealthResponse(
            status="ready", database="ok", migration="current", redis="ok"
        )

    def get_server_time(self) -> TimeResponse:
        return TimeResponse(shared_sample_age_ms=0, server_time=self.now)

    def get_event_state(self) -> OperatorStateResponse:
        return _state(
            self.manifest,
            self.now,
            version=self.version,
            event_version=self.event_version,
        )

    def post_mutation(
        self,
        operation: str,
        round_id: UUID,
        body: bytes,
    ) -> OperatorMutationResponse:
        self.bodies.append(body)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("simulated ambiguous transport")
        assert operation == "begin_preparation"
        assert round_id == self.manifest.initial_round.id
        self.version = 2
        self.event_version = 2
        return OperatorMutationResponse(
            operation="begin_preparation",
            event_id=self.manifest.event.id,
            event_version=2,
            round_id=round_id,
            round_version=2,
            round_state=RoundState.PREPARING_ASSETS,
            result_visibility=FORMAL_RESULT_VISIBILITY,
        )


class WorkflowApi(FakeApi):
    def __init__(self, manifest: LoadTestManifest, now: datetime) -> None:
        super().__init__(manifest, now)
        self.stage = "preparing"
        self.round_number = 1
        self.round_id = manifest.initial_round.id

    def get_event_state(self) -> OperatorStateResponse:
        state = _state(
            self.manifest,
            self.now,
            version=self.version,
            event_version=self.event_version,
            round_number=self.round_number,
            round_id=self.round_id,
        )
        current = state.current_round
        assert current is not None
        slot_start = self.manifest.scheduled_start_at + timedelta(
            seconds=self.manifest.scenario.rounds[0].start_offset_seconds
        )
        expected = self.manifest.scenario.rounds[self.round_number - 1].expected_option_counts
        answer_counts = (expected.a, expected.b, expected.c, expected.d)
        gate = current.gate
        statistics = current.statistics
        updates: dict[str, object] = {}
        event_state = EventState.LOBBY_OPEN if self.round_number == 1 else EventState.IN_PROGRESS
        if self.stage == "ready":
            updates["state"] = RoundState.READY_TO_SCHEDULE
            gate = OperatorGateResponse(
                target_count=600,
                ready_count=588,
                asset_failure_count=11,
                can_schedule=True,
                blocking_reasons=[],
            )
        elif self.stage in {"scheduled", "awaiting", "aggregated", "reveal"}:
            event_state = EventState.IN_PROGRESS
            updates.update(
                state=(
                    RoundState.SCHEDULED
                    if self.stage == "scheduled"
                    else RoundState.AWAITING_VALIDATION
                    if self.stage == "awaiting"
                    else RoundState.READY_TO_REVEAL
                    if self.stage == "aggregated"
                    else RoundState.REVEAL
                ),
                open_at=slot_start,
                standard_close_at=slot_start + timedelta(seconds=10),
            )
            if self.stage in {"awaiting", "aggregated", "reveal"}:
                statistics = OperatorRoundStatisticsResponse(
                    answer_count=500,
                    average_response_ms=100.0,
                    options=[
                        OperatorOptionStatisticsResponse(
                            option_id=option.id,
                            answer_count=count,
                            average_response_ms=100.0,
                        )
                        for option, count in zip(
                            self.manifest.questions[self.round_number - 1].options,
                            answer_counts,
                            strict=True,
                        )
                    ],
                )
                updates.update(
                    statistics=statistics,
                    can_publish_results=self.stage == "aggregated",
                    result_publication=(
                        RoundResultPublication.WITHHELD
                        if self.stage in {"awaiting", "aggregated"}
                        else RoundResultPublication.PUBLISHED
                    ),
                )
        return state.model_copy(
            update={
                "event_state": event_state,
                "projected_at": self.now,
                "latest_ranking_snapshot_version": (
                    self.round_number
                    if self.stage in {"aggregated", "reveal"}
                    else self.round_number - 1 or None
                ),
                "current_round": current.model_copy(update={**updates, "gate": gate}),
            }
        )

    def aggregate_automatically(self) -> None:
        self.stage = "aggregated"
        self.version += 1
        self.event_version += 1

    def get_latest_ranking(self) -> OperatorLatestRankingResponse | None:
        if self.stage != "reveal":
            return None
        return OperatorLatestRankingResponse(
            snapshot_version=self.round_number,
            round_id=self.round_id,
            rows=[
                OperatorRankingRowResponse(
                    overall_rank=index,
                    public_label="".join(letters),
                    total_points=100,
                    correct_count=1,
                )
                for index, letters in enumerate(
                    islice(
                        product("ABCDEFGHIJKLMNOPQRSTUVWXYZ", repeat=3),
                        500 if self.round_number <= 2 else 502,
                    ),
                    start=1,
                )
            ],
        )

    def post_mutation(
        self,
        operation: str,
        round_id: UUID,
        body: bytes,
    ) -> OperatorMutationResponse:
        self.bodies.append(body)
        payload = json.loads(body)
        assert payload["execute_not_after"] is not None
        assert round_id == self.round_id
        self.version += 1
        self.event_version += 1
        if operation == "begin_preparation":
            self.stage = "preparing"
            response_state = RoundState.PREPARING_ASSETS
        elif operation == "schedule_round":
            self.stage = "scheduled"
            response_state = RoundState.SCHEDULED
        elif operation == "publish_round_results":
            self.stage = "reveal"
            response_state = RoundState.REVEAL
        elif operation == "advance_round":
            self.stage = "next_preparing"
            self.round_number += 1
            self.round_id = uuid5(self.manifest.run_id, f"runtime-round:{self.round_number}")
            response_state = RoundState.PREPARING_ASSETS
        else:
            raise AssertionError(operation)
        slot_start = self.manifest.scheduled_start_at + timedelta(
            seconds=self.manifest.scenario.rounds[0].start_offset_seconds
        )
        return OperatorMutationResponse(
            operation=operation,
            event_id=self.manifest.event.id,
            event_version=self.event_version,
            round_id=self.round_id,
            round_version=self.version,
            round_state=response_state,
            result_visibility=FORMAL_RESULT_VISIBILITY,
            open_at=slot_start if operation == "schedule_round" else None,
            standard_close_at=(
                slot_start + timedelta(seconds=10) if operation == "schedule_round" else None
            ),
        )


class UnusedVerificationProvider:
    def generator_identity(self, *_args: object, **_kwargs: object) -> ArtifactIdentity:
        raise AssertionError("not used")

    def load_receipt(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("not used")


def _write_evidence_files(
    tmp_path: Path,
    manifest: LoadTestManifest,
) -> Path:
    context = manifest_context(manifest)
    scenario = manifest.scenario.rounds[0]
    slot_start = manifest.scheduled_start_at + timedelta(seconds=scenario.start_offset_seconds)
    generator = GeneratorRoundEvidence(
        format="load-test-generator-round-evidence-v2",
        run_id=manifest.run_id,
        event_id=manifest.event.id,
        source_commit=manifest.artifacts.source_commit,
        schema_revision=manifest.schema_revision,
        manifest_sha256=context.manifest_sha256,
        scenario_sha256=context.scenario_sha256,
        round_number=1,
        round_id=manifest.initial_round.id,
        question_id=manifest.questions[0].id,
        scenario_condition=scenario.condition,
        answer_population="mixed",
        virtual_session_count=600,
        virtual_answer_count=500,
        virtual_option_counts=RoundOptionCounts(a=126, b=125, c=125, d=124),
        started_answer_count=500,
        resolved_answer_count=500,
        request_start_width_ms=1_000,
        unresolved_answer_count=0,
        missed_round_count=0,
        duplicate_answer_count=0,
        misleading_success_count=0,
        replay_attempt_count=0,
        replay_same_answer_id_count=0,
        replay_row_change_count=0,
        receipt_discard_count=0,
        answer_status_query_count=0,
        status_recovered_count=0,
        sealed_at=slot_start + timedelta(seconds=20),
    )
    directory = tmp_path / "generator"
    directory.mkdir(mode=0o700)
    path = directory / "generator-round-01.json"
    path.write_bytes(canonical_json_bytes(generator) + b"\n")
    path.chmod(0o600)
    return path


def _directories(tmp_path: Path) -> tuple[Path, Path, Path]:
    journal = tmp_path / "journal"
    checkpoints = tmp_path / "checkpoints"
    requests = tmp_path / "requests"
    journal.mkdir(mode=0o700)
    checkpoints.mkdir(mode=0o700)
    requests.mkdir(mode=0o700)
    os.chmod(journal, 0o700)
    os.chmod(checkpoints, 0o700)
    os.chmod(requests, 0o700)
    return journal, checkpoints, requests


def _controller(
    manifest: LoadTestManifest,
    journal: ControllerJournal,
    checkpoints: Path,
    requests: Path,
    api: FakeApi,
) -> LoadTestController:
    return LoadTestController(
        manifest,
        journal,
        checkpoints,
        requests,
        REPOSITORY_ROOT,
        api,
        cast("object", UnusedVerificationProvider()),  # type: ignore[arg-type]
    )


def _prepare_phrase(manifest: LoadTestManifest) -> str:
    return required_confirmation_phrase(manifest_context(manifest), 1, "prepare")


def test_prepare_seals_plan_and_never_journals_password(tmp_path: Path) -> None:
    manifest = _manifest()
    now = manifest.scheduled_start_at + timedelta(seconds=700)
    journal_dir, checkpoints, requests = _directories(tmp_path)
    api = FakeApi(manifest, now)
    journal_path = journal_dir / "controller.jsonl"

    with ControllerJournal(journal_path, REPOSITORY_ROOT) as journal:
        status = _controller(manifest, journal, checkpoints, requests, api).execute(
            "prepare",
            username="operator",
            password=SecretStr("do-not-record-this-password"),
            confirmation_phrase=_prepare_phrase(manifest),
        )
        plan = next(
            record.payload
            for record in journal.records
            if isinstance(record.payload, MutationPlannedPayload)
        )
        assert plan.request_sha256 == __import__("hashlib").sha256(api.bodies[0]).hexdigest()
        assert status.next_action == "schedule"
        assert status.journal_head_sha256 == journal.head_sha256
        request = json.loads(api.bodies[0])
        assert "reason" not in request
        assert request["execute_not_after"] == (
            manifest.scheduled_start_at + timedelta(seconds=894)
        ).isoformat().replace("+00:00", "Z")
        assert "target_open_at" not in request

    body = journal_path.read_text()
    assert "do-not-record-this-password" not in body
    assert stat_mode(journal_path) == 0o600


def test_ambiguous_prepare_resume_reuses_exact_body_and_key(tmp_path: Path) -> None:
    manifest = _manifest()
    now = manifest.scheduled_start_at + timedelta(seconds=700)
    journal_dir, checkpoints, requests = _directories(tmp_path)
    journal_path = journal_dir / "controller.jsonl"
    first = FakeApi(manifest, now, fail_once=True)
    with (
        ControllerJournal(journal_path, REPOSITORY_ROOT) as journal,
        pytest.raises(RuntimeError, match="simulated ambiguous"),
    ):
        _controller(manifest, journal, checkpoints, requests, first).execute(
            "prepare",
            username="operator",
            password=SecretStr("secret"),
            confirmation_phrase=_prepare_phrase(manifest),
        )

    second = FakeApi(manifest, now)
    with ControllerJournal(journal_path, REPOSITORY_ROOT) as journal:
        _controller(manifest, journal, checkpoints, requests, second).execute(
            "prepare",
            username="operator",
            password=SecretStr("different-secret"),
            confirmation_phrase=_prepare_phrase(manifest),
        )
    assert first.bodies == second.bodies
    assert (
        json.loads(first.bodies[0])["idempotency_key"]
        == json.loads(second.bodies[0])["idempotency_key"]
    )


@pytest.mark.parametrize("role", [AdminRole.SUPERVISOR])
def test_prepare_rejects_non_operator_before_mutation(tmp_path: Path, role: AdminRole) -> None:
    manifest = _manifest()
    journal_dir, checkpoints, requests = _directories(tmp_path)
    api = FakeApi(manifest, manifest.scheduled_start_at + timedelta(seconds=700), role=role)
    with (
        ControllerJournal(journal_dir / "controller.jsonl", REPOSITORY_ROOT) as journal,
        pytest.raises(ControllerRefusal, match="controller_operator_session_drift"),
    ):
        _controller(manifest, journal, checkpoints, requests, api).execute(
            "prepare",
            username="operator",
            password=SecretStr("secret"),
            confirmation_phrase=_prepare_phrase(manifest),
        )
    assert api.bodies == []


def test_prepare_rejects_login_session_username_mismatch_before_mutation(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    journal_dir, checkpoints, requests = _directories(tmp_path)
    api = FakeApi(manifest, manifest.scheduled_start_at + timedelta(seconds=700))
    with (
        ControllerJournal(journal_dir / "controller.jsonl", REPOSITORY_ROOT) as journal,
        pytest.raises(ControllerRefusal, match="controller_operator_session_drift"),
    ):
        _controller(manifest, journal, checkpoints, requests, api).execute(
            "prepare",
            username="different-operator",
            password=SecretStr("secret"),
            confirmation_phrase=_prepare_phrase(manifest),
        )
    assert api.bodies == []


def test_prepare_missed_deadline_has_no_mutation(tmp_path: Path) -> None:
    manifest = _manifest()
    journal_dir, checkpoints, requests = _directories(tmp_path)
    api = FakeApi(manifest, manifest.scheduled_start_at + timedelta(seconds=895))
    with (
        ControllerJournal(journal_dir / "controller.jsonl", REPOSITORY_ROOT) as journal,
        pytest.raises(ControllerRefusal, match="controller_prepare_deadline_missed"),
    ):
        _controller(manifest, journal, checkpoints, requests, api).execute(
            "prepare",
            username="operator",
            password=SecretStr("secret"),
            confirmation_phrase=_prepare_phrase(manifest),
        )
    assert api.bodies == []


@pytest.mark.parametrize(("round_version", "event_version"), [(2, 1), (1, 2)])
def test_empty_journal_rejects_initial_version_drift_before_mutation(
    tmp_path: Path,
    round_version: int,
    event_version: int,
) -> None:
    manifest = _manifest()
    journal_dir, checkpoints, requests = _directories(tmp_path)
    api = FakeApi(manifest, manifest.scheduled_start_at + timedelta(seconds=700))
    api.version = round_version
    api.event_version = event_version
    with (
        ControllerJournal(journal_dir / "controller.jsonl", REPOSITORY_ROOT) as journal,
        pytest.raises(ControllerRefusal, match="controller_live_state_drift"),
    ):
        _controller(manifest, journal, checkpoints, requests, api).execute(
            "prepare",
            username="operator",
            password=SecretStr("secret"),
            confirmation_phrase=_prepare_phrase(manifest),
        )
    assert api.bodies == []


def test_resume_rejects_out_of_band_version_bump_before_next_mutation(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    journal_dir, checkpoints, requests = _directories(tmp_path)
    journal_path = journal_dir / "controller.jsonl"
    api = FakeApi(manifest, manifest.scheduled_start_at + timedelta(seconds=700))
    with ControllerJournal(journal_path, REPOSITORY_ROOT) as journal:
        _controller(manifest, journal, checkpoints, requests, api).execute(
            "prepare",
            username="operator",
            password=SecretStr("secret"),
            confirmation_phrase=_prepare_phrase(manifest),
        )
    api.version = 3
    api.event_version = 3
    with (
        ControllerJournal(journal_path, REPOSITORY_ROOT) as journal,
        pytest.raises(ControllerRefusal, match="controller_live_state_drift"),
    ):
        _controller(manifest, journal, checkpoints, requests, api).execute(
            "schedule",
            username="operator",
            password=SecretStr("secret"),
            confirmation_phrase=required_confirmation_phrase(
                manifest_context(manifest), 1, "schedule"
            ),
        )
    assert len(api.bodies) == 1


def test_resume_rejects_different_operator_account_before_mutation(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    journal_dir, checkpoints, requests = _directories(tmp_path)
    journal_path = journal_dir / "controller.jsonl"
    first = FakeApi(manifest, manifest.scheduled_start_at + timedelta(seconds=700))
    with ControllerJournal(journal_path, REPOSITORY_ROOT) as journal:
        _controller(manifest, journal, checkpoints, requests, first).execute(
            "prepare",
            username="operator",
            password=SecretStr("secret"),
            confirmation_phrase=_prepare_phrase(manifest),
        )
    second = FakeApi(
        manifest,
        manifest.scheduled_start_at + timedelta(seconds=895),
        account_id=UUID(int=99),
    )
    with (
        ControllerJournal(journal_path, REPOSITORY_ROOT) as journal,
        pytest.raises(ControllerRefusal, match="controller_operator_identity_drift"),
    ):
        _controller(manifest, journal, checkpoints, requests, second).execute(
            "schedule",
            username="operator",
            password=SecretStr("secret"),
            confirmation_phrase=required_confirmation_phrase(
                manifest_context(manifest), 1, "schedule"
            ),
        )
    assert second.bodies == []


def test_journal_reducer_tracks_all_ten_rounds_to_completed(tmp_path: Path) -> None:
    manifest = _manifest()
    context = manifest_context(manifest)
    journal_dir, _, _ = _directories(tmp_path)
    round_id = manifest.initial_round.id
    event_version = 1
    round_version = 1
    observed_rounds: list[tuple[int, UUID, UUID]] = []

    with ControllerJournal(journal_dir / "controller.jsonl", REPOSITORY_ROOT) as journal:
        journal.append(
            run_id=context.run_id,
            event_id=context.event_id,
            payload=InitializedPayload(
                kind="initialized",
                context=context,
                operator_account_id=ACCOUNT_ID,
                server_time=CREATED_AT,
                event_version=event_version,
                round_version=round_version,
            ),
            recorded_at=CREATED_AT,
        )

        for round_number in range(1, 11):
            question_id = manifest.questions[round_number - 1].id
            generator = _artifact(f"generator-round-{round_number:02d}.json", "1")

            def append_mutation(
                operation: str,
                expected_version: int,
                response_round_id: UUID,
                response_version: int,
                response_state: RoundState,
                *,
                attempt: int = 1,
                current_round_number: int = round_number,
                current_round_id: UUID = round_id,
                current_question_id: UUID = question_id,
            ) -> MutationSucceededPayload:
                nonlocal event_version
                typed_operation = cast("ControllerOperation", operation)
                request = _mutation_request(
                    manifest,
                    round_number=current_round_number,
                    operation=typed_operation,
                    expected_version=expected_version,
                    attempt=attempt,
                )
                plan = MutationPlannedPayload(
                    kind="mutation_planned",
                    round_number=current_round_number,
                    round_id=current_round_id,
                    question_id=current_question_id,
                    operation=typed_operation,
                    attempt=attempt,
                    expected_version=expected_version,
                    idempotency_key=request.idempotency_key,
                    request_sha256=sha256(_request_body(request)).hexdigest(),
                )
                journal.append(
                    run_id=context.run_id,
                    event_id=context.event_id,
                    payload=plan,
                )
                event_version += 1
                response = OperatorMutationResponse(
                    operation=typed_operation,
                    event_id=context.event_id,
                    event_version=event_version,
                    round_id=response_round_id,
                    round_version=response_version,
                    round_state=response_state,
                    result_visibility=FORMAL_RESULT_VISIBILITY,
                )
                succeeded = MutationSucceededPayload(
                    kind="mutation_succeeded",
                    round_number=current_round_number,
                    target_round_id=current_round_id,
                    idempotency_key=request.idempotency_key,
                    response=response,
                )
                journal.append(
                    run_id=context.run_id,
                    event_id=context.event_id,
                    payload=succeeded,
                )
                return succeeded

            append_mutation(
                "begin_preparation",
                round_version,
                round_id,
                round_version + 1,
                RoundState.PREPARING_ASSETS,
            )
            append_mutation(
                "schedule_round",
                round_version + 2,
                round_id,
                round_version + 3,
                RoundState.SCHEDULED,
            )
            event_version += 1
            append_mutation(
                "publish_round_results",
                round_version + 4,
                round_id,
                round_version + 5,
                RoundState.REVEAL,
            )
            journal.append(
                run_id=context.run_id,
                event_id=context.event_id,
                payload=ResultsObservedPayload(
                    kind="results_observed",
                    round_number=round_number,
                    round_id=round_id,
                    question_id=question_id,
                    event_version=event_version,
                    round_version=round_version + 5,
                    ranking_snapshot_version=round_number,
                    generator_evidence=generator,
                ),
                recorded_at=manifest.scheduled_start_at
                + timedelta(
                    seconds=manifest.scenario.rounds[round_number - 1].start_offset_seconds + 30
                ),
            )
            requested = journal.append(
                run_id=context.run_id,
                event_id=context.event_id,
                payload=VerificationRequestedPayload(
                    kind="verification_requested",
                    round_number=round_number,
                    round_id=round_id,
                    question_id=question_id,
                    generator_evidence=generator,
                ),
            )
            request_artifact = _artifact(f"verification-request-round-{round_number:02d}.json", "3")
            journal.append(
                run_id=context.run_id,
                event_id=context.event_id,
                payload=VerificationRequestSealedPayload(
                    kind="verification_request_sealed",
                    round_number=round_number,
                    round_id=round_id,
                    verification_requested_head_sha256=requested.entry_sha256,
                    request_artifact=request_artifact,
                ),
            )
            journal.append(
                run_id=context.run_id,
                event_id=context.event_id,
                payload=ReceiptAcceptedPayload(
                    kind="receipt_accepted",
                    round_number=round_number,
                    round_id=round_id,
                    verification_id=verification_id(
                        context.run_id, round_number, requested.entry_sha256
                    ),
                    receipt_artifact=_artifact(f"receipt-round-{round_number:02d}.json", "4"),
                    report=_artifact(f"report-round-{round_number:02d}.json", "5"),
                    verification_requested_head_sha256=requested.entry_sha256,
                ),
            )
            next_round_id = (
                uuid5(manifest.run_id, f"runtime-round:{round_number + 1}")
                if round_number < 10
                else round_id
            )
            operation = "advance_round" if round_number < 10 else "complete_event"
            progression_attempt = 1
            progression_expected_version = round_version + 5
            if round_number == 1:
                failed_keys = []
                for attempt, failed_version in (
                    (1, round_version + 5),
                    (2, round_version + 7),
                ):
                    request = _mutation_request(
                        manifest,
                        round_number=round_number,
                        operation="advance_round",
                        expected_version=failed_version,
                        attempt=attempt,
                    )
                    journal.append(
                        run_id=context.run_id,
                        event_id=context.event_id,
                        payload=MutationPlannedPayload(
                            kind="mutation_planned",
                            round_number=round_number,
                            round_id=round_id,
                            question_id=question_id,
                            operation="advance_round",
                            attempt=attempt,
                            expected_version=failed_version,
                            idempotency_key=request.idempotency_key,
                            request_sha256=sha256(_request_body(request)).hexdigest(),
                        ),
                    )
                    journal.append(
                        run_id=context.run_id,
                        event_id=context.event_id,
                        payload=MutationFailedPayload(
                            kind="mutation_failed",
                            round_number=round_number,
                            target_round_id=round_id,
                            operation="advance_round",
                            attempt=attempt,
                            idempotency_key=request.idempotency_key,
                            error_code="round_backup_failed",
                        ),
                    )
                    failed_keys.append(request.idempotency_key)
                    assert (
                        _journal_progress(manifest, context, journal.records).next_attempt
                        == attempt + 1
                    )
                assert failed_keys[0] != failed_keys[1]
                progression_attempt = 3
                progression_expected_version = round_version + 10
            progression = append_mutation(
                operation,
                progression_expected_version,
                next_round_id,
                progression_expected_version + 1,
                (
                    RoundState.PREPARING_ASSETS
                    if round_number < 10
                    else RoundState.ROUND_BACKUP_VALIDATED
                ),
                attempt=progression_attempt,
            )
            journal.append(
                run_id=context.run_id,
                event_id=context.event_id,
                payload=CheckpointSealedPayload(
                    kind="checkpoint_sealed",
                    round_number=round_number,
                    checkpoint=_artifact(
                        f"controller-round-{round_number:02d}-{round_id}.json", "6"
                    ),
                ),
            )
            progress = _journal_progress(manifest, context, journal.records)
            observed_rounds.append((round_number, round_id, question_id))
            if round_number < 10:
                assert progress.round_number == round_number + 1
                assert progress.round_id == progression.response.round_id
                assert progress.question_id == manifest.questions[round_number].id
                round_id = next_round_id
                round_version = progression.response.round_version or 1
            else:
                assert progress.phase == "completed"

        assert (
            len(
                [record for record in journal.records if record.payload.kind == "checkpoint_sealed"]
            )
            == 10
        )
        assert [number for number, _, _ in observed_rounds] == list(range(1, 11))
        assert [question for _, _, question in observed_rounds] == [
            question.id for question in manifest.questions
        ]
        validated_checkpoints: list[int] = []

        def validate_checkpoint(
            checkpoint: ControllerCheckpoint, _artifact_value: ArtifactIdentity
        ) -> None:
            validated_checkpoints.append(checkpoint.round_number)

        assert (
            _journal_progress(
                manifest,
                context,
                journal.records,
                checkpoint_validator=validate_checkpoint,
            ).phase
            == "completed"
        )
        assert validated_checkpoints == list(range(1, 11))

        def reject_missing_checkpoint(
            _checkpoint: ControllerCheckpoint, _artifact_value: ArtifactIdentity
        ) -> None:
            raise ControllerRefusal("controller_checkpoint_invalid")

        with pytest.raises(ControllerRefusal, match="controller_checkpoint_invalid"):
            _journal_progress(
                manifest,
                context,
                journal.records,
                checkpoint_validator=reject_missing_checkpoint,
            )


def test_results_rechecks_server_deadline_after_slow_evidence_before_journal(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    journal_dir, checkpoints, requests = _directories(tmp_path)
    api = WorkflowApi(manifest, manifest.scheduled_start_at + timedelta(seconds=700))
    generator_directory = tmp_path / "slow-generator"
    generator_directory.mkdir(mode=0o700)

    class SlowProvider(UnusedVerificationProvider):
        def generator_identity(self, *_args: object, **_kwargs: object) -> ArtifactIdentity:
            api.now += timedelta(seconds=2)
            return _artifact("generator.json")

    with ControllerJournal(journal_dir / "controller.jsonl", REPOSITORY_ROOT) as journal:
        controller = LoadTestController(
            manifest,
            journal,
            checkpoints,
            requests,
            REPOSITORY_ROOT,
            api,
            SlowProvider(),
        )
        controller.execute(
            "prepare",
            username="operator",
            password=SecretStr("secret"),
            confirmation_phrase=_prepare_phrase(manifest),
        )
        api.stage = "ready"
        api.version += 1
        api.event_version += 1
        api.now = manifest.scheduled_start_at + timedelta(seconds=895)
        controller.execute(
            "schedule",
            username="operator",
            password=SecretStr("secret"),
            confirmation_phrase=required_confirmation_phrase(
                manifest_context(manifest), 1, "schedule"
            ),
        )
        api.aggregate_automatically()
        api.now = manifest.scheduled_start_at + timedelta(seconds=1_259)
        controller.execute(
            "publish",
            username="operator",
            password=SecretStr("secret"),
            confirmation_phrase=required_confirmation_phrase(
                manifest_context(manifest), 1, "publish"
            ),
        )
        with pytest.raises(ControllerRefusal, match="controller_results_deadline_missed"):
            controller.execute(
                "wait-results",
                username="operator",
                password=SecretStr("secret"),
                confirmation_phrase=required_confirmation_phrase(
                    manifest_context(manifest), 1, "wait-results"
                ),
                generator_evidence_path=generator_directory / "generator.json",
            )
    assert len(api.bodies) == 3


def test_real_file_provider_results_request_crash_resume_and_tamper_detection(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    journal_dir, checkpoints, requests = _directories(tmp_path)
    generator_path = _write_evidence_files(tmp_path, manifest)
    journal_path = journal_dir / "controller.jsonl"
    api = WorkflowApi(manifest, manifest.scheduled_start_at + timedelta(seconds=700))

    with ControllerJournal(journal_path, REPOSITORY_ROOT) as journal:
        controller = LoadTestController(
            manifest,
            journal,
            checkpoints,
            requests,
            REPOSITORY_ROOT,
            api,
            FileVerificationProvider(),
        )
        controller.execute(
            "prepare",
            username="operator",
            password=SecretStr("secret"),
            confirmation_phrase=_prepare_phrase(manifest),
        )
        api.stage = "ready"
        api.version += 1
        api.event_version += 1
        api.now = manifest.scheduled_start_at + timedelta(seconds=895)
        controller.execute(
            "schedule",
            username="operator",
            password=SecretStr("secret"),
            confirmation_phrase=required_confirmation_phrase(
                manifest_context(manifest), 1, "schedule"
            ),
        )

        api.aggregate_automatically()
        api.now = manifest.scheduled_start_at + timedelta(seconds=930)
        controller.execute(
            "publish",
            username="operator",
            password=SecretStr("secret"),
            confirmation_phrase=required_confirmation_phrase(
                manifest_context(manifest), 1, "publish"
            ),
        )

    class CrashAfterRequestSeal(FileVerificationProvider):
        def seal_verification_request(self, *args: object, **kwargs: object) -> ArtifactIdentity:
            super().seal_verification_request(*args, **kwargs)  # type: ignore[arg-type]
            raise RuntimeError("crash-after-request-fsync")

    api.now = manifest.scheduled_start_at + timedelta(seconds=930)
    with (
        ControllerJournal(journal_path, REPOSITORY_ROOT) as journal,
        pytest.raises(RuntimeError, match="crash-after-request-fsync"),
    ):
        LoadTestController(
            manifest,
            journal,
            checkpoints,
            requests,
            REPOSITORY_ROOT,
            api,
            CrashAfterRequestSeal(),
        ).execute(
            "wait-results",
            username="operator",
            password=SecretStr("secret"),
            confirmation_phrase=required_confirmation_phrase(
                manifest_context(manifest), 1, "wait-results"
            ),
            generator_evidence_path=generator_path,
        )
    assert len(api.bodies) == 3
    request_path = next(requests.iterdir())
    original_request = request_path.read_bytes()

    with ControllerJournal(journal_path, REPOSITORY_ROOT) as journal:
        status = LoadTestController(
            manifest,
            journal,
            checkpoints,
            requests,
            REPOSITORY_ROOT,
            api,
            FileVerificationProvider(),
        ).execute(
            "wait-results",
            username="operator",
            password=SecretStr("secret"),
            confirmation_phrase=required_confirmation_phrase(
                manifest_context(manifest), 1, "wait-results"
            ),
            generator_evidence_path=generator_path,
        )
        requested_record = next(
            record for record in journal.records if record.payload.kind == "verification_requested"
        )
        sealed_record = next(
            record
            for record in journal.records
            if record.payload.kind == "verification_request_sealed"
        )
        request = RoundVerificationRequest.model_validate_json(original_request)
        assert request.requested_at == api.now
        assert request.verification_requested_head_sha256 == requested_record.entry_sha256
        assert sealed_record.payload.verification_requested_head_sha256 == (
            requested_record.entry_sha256
        )
        assert status.next_action == "advance"
    assert len(api.bodies) == 3
    assert request_path.read_bytes() == original_request

    request_path.write_bytes(original_request + b" ")
    request_path.chmod(0o600)
    with (
        ControllerJournal(journal_path, REPOSITORY_ROOT) as journal,
        pytest.raises(VerificationHandoffError, match="verification_request_mismatch"),
    ):
        LoadTestController(
            manifest,
            journal,
            checkpoints,
            requests,
            REPOSITORY_ROOT,
            api,
            FileVerificationProvider(),
        ).confirmation_prompt("advance")


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
