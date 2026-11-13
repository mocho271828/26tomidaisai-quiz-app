from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from quiz_app.domain.errors import DomainInvariantError
from quiz_app.domain.quiz_service import QuizDomainService
from quiz_app.models import Answer, QuizRound


def test_elapsed_ms_uses_integer_timedelta_arithmetic() -> None:
    open_at = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
    round_ = QuizRound(
        event_id=uuid4(),
        question_id=uuid4(),
        version=1,
        open_at=open_at,
    )
    answer = Answer(
        participant_id=uuid4(),
        round_id=round_.id,
        option_id=uuid4(),
        client_answer_id=uuid4(),
        received_at=open_at + timedelta(microseconds=1_001_000),
    )

    assert QuizDomainService._elapsed_ms(round_, answer) == 1_001
    answer.client_elapsed_ms = 500
    assert QuizDomainService._elapsed_ms(round_, answer, "client_monotonic_v1") == 500
    assert QuizDomainService._elapsed_ms(round_, answer, "server_received_v1") == 1_001
    answer.client_elapsed_ms = None
    with pytest.raises(DomainInvariantError, match="client_elapsed_ms_missing"):
        QuizDomainService._elapsed_ms(round_, answer, "client_monotonic_v1")
