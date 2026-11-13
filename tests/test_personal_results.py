"""本人結果の一括取得・進行との分離・古い公開版の破棄を確認する。"""

import asyncio
from datetime import UTC, datetime
from threading import Event as ThreadEvent
from types import MappingProxyType
from typing import cast
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, create_engine, event
from sqlmodel import Session, SQLModel

from quiz_app.audience.auth_service import ParticipantPrincipal
from quiz_app.audience.contracts import (
    ParticipantCommonStateResponse,
    PersonalResultDelivery,
    PersonalResultResponse,
    RankingRowResponse,
    ScoredParticipantOutcomeResponse,
)
from quiz_app.audience.result_batch import ResultBatch, ResultBatchKey, load_result_batch
from quiz_app.audience.result_delivery import PersonalResultService, result_revision
from quiz_app.domain.errors import DomainInvariantError
from quiz_app.models import (
    Answer,
    Event,
    QuizRound,
    RankSnapshot,
    RankSnapshotRow,
    ScoreLedger,
    ScoringPolicy,
)
from quiz_app.models.enums import RoundResultPublication, RoundState, SpeedCohort
from quiz_app.quiz_configuration import ResultVisibility

EVENT_ID, ROUND_ID, OPTION_ID, PERSON_ID = (UUID(int=value) for value in range(1, 5))
NOW = datetime(2026, 10, 2, tzinfo=UTC)


def common(version: int = 1, *, visible: bool = True) -> ParticipantCommonStateResponse:
    return ParticipantCommonStateResponse(
        award_ceremony=None,
        projected_at=NOW,
        event={
            "timing_basis": "server_received_v1",
            "arrival_grace_ms": 0,
            "clock_tolerance_ms": 0,
            "client_ahead_tolerance_ms": 0,
            "event_id": EVENT_ID,
            "event_title": "Fixture",
            "event_state": "InProgress",
            "event_version": version,
            "start_delay_seconds": 3,
        },
        current_round=None,
        latest_result={
            "round_id": ROUND_ID,
            "round_number": 1,
            "version": 1,
            "state_version": 8,
            "state": "Reveal",
            "open_at": None,
            "standard_close_at": None,
            "question": {"question_id": UUID(int=5), "prompt": "Fixture", "options": []},
            "result": {
                "publication": "Published",
                "correct_option_id": OPTION_ID,
                "distribution": None,
            },
        },
        result_visibility=ResultVisibility(
            distribution=False, personal_ranking=visible, overall_ranking=False
        ),
        latest_ranking_snapshot_version=1 if visible else None,
    )


def principal(participant_id: UUID = PERSON_ID, event_id: UUID = EVENT_ID) -> ParticipantPrincipal:
    return ParticipantPrincipal(participant_id, UUID(int=10), event_id, "ABC", True)


def batch(key: ResultBatchKey) -> ResultBatch:
    return ResultBatch(
        key,
        MappingProxyType({PERSON_ID: OPTION_ID}),
        MappingProxyType(
            {
                PERSON_ID: ScoredParticipantOutcomeResponse(
                    timing_basis="server_received_v1",
                    status="Scored",
                    is_correct=True,
                    base_points=400,
                    speed_points=100,
                    elapsed_ms=2450,
                    total_points=500,
                    scoring_policy_version="fixture",
                    speed_cohort=SpeedCohort.CORRECT,
                    speed_rank=1,
                    speed_cohort_size=600,
                )
            }
        ),
        MappingProxyType(
            {
                "ABC": RankingRowResponse(
                    public_label="ABC",
                    overall_rank=1,
                    total_points=500,
                    correct_count=1,
                )
            }
        ),
        (),
    )


async def wait_ready(service: PersonalResultService, state: ParticipantCommonStateResponse) -> None:
    async with asyncio.timeout(2):
        while service.delivery(state).status != "ready":
            generation = service.generation
            if service.delivery(state).status == "ready":
                return
            await service.wait_for_change(generation)


@pytest.mark.asyncio
async def test_600_principals_share_one_build_and_cold_results_do_not_wait() -> None:
    entered, release = ThreadEvent(), ThreadEvent()
    calls = 0

    def load(_engine: Engine, key: ResultBatchKey) -> ResultBatch:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(2)
        return batch(key)

    service = PersonalResultService(cast(Engine, object()), loader=load)
    service.start()
    state = common()
    service.observe(state)
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        for index in range(600):
            response = service.result_for(principal(UUID(int=index + 100)), state)
            assert response.delivery.status == "pending" and response.result is None
        assert calls == 1
        release.set()
        await wait_ready(service, state)
        own = service.result_for(principal(), state)
        assert own.result is not None and own.result.outcome is not None
        assert own.result.outcome.status == "Scored"
        assert own.result.outcome.base_points == 400
        assert own.result.outcome.speed_points == 100
        assert own.result.outcome.elapsed_ms == 2450
        other = service.result_for(principal(UUID(int=999)), state)
        assert other.result is not None and other.result.outcome is not None
        assert other.result.selected_option_id is None
        assert other.result.outcome.status == "Unanswered"
        assert calls == 1
        with pytest.raises(DomainInvariantError, match="principal_mismatch"):
            service.result_for(principal(event_id=UUID(int=1000)), state)
    finally:
        release.set()
        await service.close()


@pytest.mark.asyncio
async def test_invalidation_and_deferred_build_never_revive_old_visibility() -> None:
    calls = 0

    def load(_engine: Engine, key: ResultBatchKey) -> ResultBatch:
        nonlocal calls
        calls += 1
        return batch(key)

    service = PersonalResultService(cast(Engine, object()), loader=load)
    service.start()
    public = common()
    service.set_deferred(EVENT_ID, "participant", True)
    service.observe(public)
    assert service.delivery(public).status == "pending"
    assert calls == 0
    service.set_deferred(EVENT_ID, "participant", False)
    try:
        await wait_ready(service, public)
        service.invalidate(EVENT_ID)
        assert service.delivery(public).status == "unavailable"
        hidden = common(2, visible=False)
        service.observe(hidden)
        await wait_ready(service, hidden)
        service.observe(public)
        assert service.result_for(principal(), public).result is None
        protected = service.result_for(principal(), hidden).result
        assert protected is not None and protected.outcome is not None
        assert protected.outcome.status == "Scored"
        assert protected.outcome.speed_rank is None
        assert (protected.outcome.base_points, protected.outcome.speed_points) == (400, 100)
        assert protected.outcome.elapsed_ms == 2450
        assert protected.ranking is None
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "validation"])
async def test_build_failure_does_not_start_a_request_per_client(failure: str) -> None:
    now = [0.0]
    calls = 0

    def load(_engine: Engine, _key: ResultBatchKey) -> ResultBatch:
        nonlocal calls
        calls += 1
        if failure == "validation":
            RankingRowResponse.model_validate({"public_label": "invalid-fixture"})
        raise TimeoutError("fixture")

    service = PersonalResultService(cast(Engine, object()), loader=load, clock=lambda: now[0])
    service.start()
    state = common()
    before = service.generation
    service.observe(state)
    try:
        async with asyncio.timeout(2):
            while service._loading is not None:
                before = await service.wait_for_change(before)
        for _ in range(600):
            assert service.delivery(state).status == "pending"
        assert calls == 1
        now[0] = 5.0
        service.delivery(state)
        async with asyncio.timeout(2):
            while service._loading is not None:
                before = await service.wait_for_change(before)
        assert calls == 2
    finally:
        await service.close()


@pytest.mark.parametrize("elapsed_ms", [None, 0, 2450])
@pytest.mark.parametrize("is_correct,base_points,speed_points", [(True, 400, 100), (False, 0, 0)])
def test_batch_query_count_is_independent_of_recipient_count(
    elapsed_ms: int | None, is_correct: bool, base_points: int, speed_points: int
) -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    snapshot_id = UUID(int=6)
    with Session(engine) as session, session.begin():
        session.add(
            ScoringPolicy(
                version="fixture",
                base_points=400,
                initial_speed_bonus=100,
                speed_bucket_ms=100,
                speed_decrement_per_bucket=1,
                standard_duration_ms=10000,
                created_at=NOW,
            )
        )
        session.add(
            Event(
                id=EVENT_ID,
                slug="fixture",
                title="Fixture",
                scoring_policy_version="fixture",
                display_name_policy_version="fixture",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            QuizRound(
                id=ROUND_ID,
                event_id=EVENT_ID,
                question_id=UUID(int=5),
                version=1,
                state=RoundState.REVEAL,
                result_publication=RoundResultPublication.PUBLISHED,
                created_at=NOW,
            )
        )
        session.add(
            Answer(
                participant_id=PERSON_ID,
                round_id=ROUND_ID,
                option_id=OPTION_ID,
                client_answer_id=UUID(int=7),
                received_at=NOW,
            )
        )
        session.add(
            ScoreLedger(
                participant_id=PERSON_ID,
                round_id=ROUND_ID,
                scoring_policy_version="fixture",
                is_correct=is_correct,
                base_points=base_points,
                speed_points=speed_points,
                elapsed_ms=elapsed_ms,
                total_points=base_points + speed_points,
                speed_cohort=SpeedCohort.CORRECT if is_correct else SpeedCohort.ALL_VALID,
                speed_rank=1,
                speed_cohort_size=1,
            )
        )
        session.add(
            RankSnapshot(
                id=snapshot_id, event_id=EVENT_ID, round_id=ROUND_ID, version=1, generated_at=NOW
            )
        )
        session.add(
            RankSnapshotRow(
                snapshot_id=snapshot_id,
                public_label="ABC",
                overall_rank=1,
                total_points=500,
                correct_count=1,
            )
        )
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def record(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _many: bool,
    ) -> None:
        statements.append(statement)

    try:
        revision = result_revision(common())
        assert revision is not None
        loaded = load_result_batch(engine, ResultBatchKey.from_revision(revision))
        assert len(statements) == 6
        for _ in range(600):
            result = loaded.for_participant(principal(), revision)
            assert result.ranking is not None and result.ranking.total_points == 500
            assert isinstance(result.outcome, ScoredParticipantOutcomeResponse)
            assert result.outcome.is_correct is is_correct
            assert (result.outcome.base_points, result.outcome.speed_points) == (
                base_points,
                speed_points,
            )
            assert result.outcome.total_points == base_points + speed_points
            assert result.outcome.elapsed_ms == elapsed_ms
        assert len(statements) == 6
    finally:
        engine.dispose()


def test_ready_without_personal_payload_is_not_valid() -> None:
    with pytest.raises(ValidationError, match="readiness_mismatch"):
        PersonalResultResponse(
            delivery=PersonalResultDelivery(status="ready", revision=result_revision(common())),
            result=None,
        )


@pytest.mark.parametrize("mismatch", ["round", "visibility", "ranking", "top_rows"])
def test_private_wire_rejects_mismatched_revision_or_hidden_ranking(mismatch: str) -> None:
    revision = result_revision(common())
    assert revision is not None
    response = PersonalResultResponse(
        delivery=PersonalResultDelivery(status="ready", revision=revision),
        result=batch(ResultBatchKey.from_revision(revision)).for_participant(principal(), revision),
    ).model_dump(mode="json")
    if mismatch == "round":
        response["delivery"]["revision"]["result_round_version"] = None
    elif mismatch == "visibility":
        response["delivery"]["revision"]["result_visibility"]["personal_ranking"] = False
    elif mismatch == "ranking":
        response["result"]["ranking"]["snapshot_version"] = 2
    else:
        response["result"]["ranking"]["top_rows"] = []
    with pytest.raises(ValidationError):
        PersonalResultResponse.model_validate(response)
