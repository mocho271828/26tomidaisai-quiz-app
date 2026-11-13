from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlmodel import Session

from quiz_app.domain.errors import (
    DomainInvariantError,
    InvalidStateTransitionError,
    ResourceNotFoundError,
    VersionConflictError,
)
from quiz_app.domain.quiz_service import QuizDomainService
from quiz_app.models import Event, Question, QuestionOption, QuizRound
from quiz_app.models.enums import EventState, RoundState

EVENT_ID = UUID("70000000-0000-4000-8000-000000000001")
ROUND_ID = UUID("70000000-0000-4000-8000-000000000002")
CURRENT_QUESTION_ID = UUID("70000000-0000-4000-8000-000000000003")
TARGET_QUESTION_ID = UUID("70000000-0000-4000-8000-000000000004")
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


class FakeScalarResult:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def all(self) -> list[object]:
        return self._values


class FakeQuestionSelectionSession:
    def __init__(
        self,
        question: Question | None,
        options: list[QuestionOption],
        used_question_ids: list[UUID],
    ) -> None:
        self.question = question
        self.options = options
        self.used_question_ids = used_question_ids
        self.executed_statements: list[object] = []

    def get(self, model_type: type[Question], primary_key: UUID) -> Question | None:
        if model_type is Question and self.question is not None:
            return self.question if self.question.id == primary_key else None
        return None

    def scalars(self, statement: object) -> FakeScalarResult:
        if "quiz_rounds.question_id" in str(statement):
            return FakeScalarResult(list(self.used_question_ids))
        return FakeScalarResult(list(self.options))

    def execute(self, statement: object) -> None:
        self.executed_statements.append(statement)


def _event() -> Event:
    return Event(
        id=EVENT_ID,
        slug="demo-selection",
        title="[DEMO] 問題選択",
        state=EventState.LOBBY_OPEN,
        scoring_policy_version="score-v1",
        display_name_policy_version="name-v1",
        version=3,
    )


def _round(state: RoundState = RoundState.PREPARING_ASSETS) -> QuizRound:
    return QuizRound(
        id=ROUND_ID,
        event_id=EVENT_ID,
        question_id=CURRENT_QUESTION_ID,
        version=2,
        state=state,
    )


def _target_question(event_id: UUID = EVENT_ID) -> Question:
    return Question(
        id=TARGET_QUESTION_ID,
        event_id=event_id,
        version=2,
        prompt="[DEMO] 切替先",
        display_order=2,
    )


def _options(correct_count: int = 1) -> list[QuestionOption]:
    return [
        QuestionOption(
            id=UUID(int=index + 20),
            question_id=TARGET_QUESTION_ID,
            label=chr(ord("A") + index),
            content=str(index),
            display_order=index,
            is_correct=index < correct_count,
        )
        for index in range(2)
    ]


def _arrange_service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    round_: QuizRound | None = None,
    event: Event | None = None,
    question: Question | None = None,
    options: list[QuestionOption] | None = None,
    used_question_ids: list[UUID] | None = None,
) -> tuple[QuizDomainService, FakeQuestionSelectionSession, QuizRound, Event]:
    selected_round = round_ or _round()
    selected_event = event or _event()
    fake_session = FakeQuestionSelectionSession(
        question if question is not None else _target_question(),
        options if options is not None else _options(),
        used_question_ids if used_question_ids is not None else [CURRENT_QUESTION_ID],
    )
    service = QuizDomainService(cast(Engine, object()))
    monkeypatch.setattr(service, "_round_for_update", lambda _session, _id: selected_round)
    monkeypatch.setattr(service, "_database_time", lambda _session: NOW)
    monkeypatch.setattr(service, "_event_for_update", lambda _session, _id: selected_event)
    return service, fake_session, selected_round, selected_event


def test_question_selection_preserves_legacy_readiness_and_bumps_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, fake_session, round_, event = _arrange_service(monkeypatch)

    result = service.select_round_question_in_transaction(
        cast(Session, fake_session),
        ROUND_ID,
        expected_version=2,
        question_id=TARGET_QUESTION_ID,
    )

    assert round_.question_id == TARGET_QUESTION_ID
    assert round_.version == 3
    assert event.version == 4
    assert event.updated_at == NOW
    assert result.question_id == TARGET_QUESTION_ID
    assert result.round_version == 3
    assert fake_session.executed_statements == []


@pytest.mark.parametrize(
    ("state", "expected_message"),
    [
        (RoundState.READY_TO_SCHEDULE, "round_is_not_preparing_assets"),
        (RoundState.SCHEDULED, "round_is_not_preparing_assets"),
    ],
)
def test_question_selection_rejects_unsafe_round_state(
    monkeypatch: pytest.MonkeyPatch,
    state: RoundState,
    expected_message: str,
) -> None:
    service, fake_session, _, _ = _arrange_service(
        monkeypatch,
        round_=_round(state),
    )

    with pytest.raises(InvalidStateTransitionError, match=expected_message):
        service.select_round_question_in_transaction(
            cast(Session, fake_session),
            ROUND_ID,
            expected_version=2,
            question_id=TARGET_QUESTION_ID,
        )


def test_question_selection_rejects_stale_version_and_current_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, fake_session, _, _ = _arrange_service(monkeypatch)

    with pytest.raises(VersionConflictError, match="round_version_conflict"):
        service.select_round_question_in_transaction(
            cast(Session, fake_session), ROUND_ID, 1, TARGET_QUESTION_ID
        )
    with pytest.raises(InvalidStateTransitionError, match="question_is_already_selected"):
        service.select_round_question_in_transaction(
            cast(Session, fake_session), ROUND_ID, 2, CURRENT_QUESTION_ID
        )


def test_question_selection_rejects_question_used_by_previous_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, fake_session, _, _ = _arrange_service(
        monkeypatch,
        used_question_ids=[CURRENT_QUESTION_ID, TARGET_QUESTION_ID],
    )

    with pytest.raises(InvalidStateTransitionError, match="question_was_already_used"):
        service.select_round_question_in_transaction(
            cast(Session, fake_session), ROUND_ID, 2, TARGET_QUESTION_ID
        )


def test_question_selection_rejects_foreign_or_invalid_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    foreign_question = _target_question(UUID(int=999))
    service, fake_session, _, _ = _arrange_service(
        monkeypatch,
        question=foreign_question,
    )
    with pytest.raises(ResourceNotFoundError, match="question_not_found_for_event"):
        service.select_round_question_in_transaction(
            cast(Session, fake_session), ROUND_ID, 2, TARGET_QUESTION_ID
        )

    service, fake_session, _, _ = _arrange_service(monkeypatch, options=_options(0))
    with pytest.raises(
        DomainInvariantError,
        match="question_must_have_exactly_one_correct_option",
    ):
        service.select_round_question_in_transaction(
            cast(Session, fake_session), ROUND_ID, 2, TARGET_QUESTION_ID
        )
