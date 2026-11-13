from typing import cast
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from sqlalchemy import Select, select
from sqlalchemy.dialects import postgresql
from sqlmodel import Session

from quiz_app.domain.errors import ResourceNotFoundError
from quiz_app.domain.quiz_service import QuizDomainService
from quiz_app.domain.types import SubmitAnswerCommand
from quiz_app.models.quiz import QuizRound


def test_answer_round_lock_compiles_to_postgresql_for_share() -> None:
    session = MagicMock(spec=Session)
    session.execute.return_value.one_or_none.return_value = None
    command = SubmitAnswerCommand(
        participant_id=UUID(int=2),
        round_id=UUID(int=1),
        round_version=1,
        option_id=UUID(int=3),
        client_answer_id=UUID(int=4),
        client_elapsed_ms=1000,
    )
    with pytest.raises(ResourceNotFoundError):
        QuizDomainService._answer_context_for_share(cast(Session, session), command)
    statement = session.execute.call_args.args[0]
    assert isinstance(statement, Select)

    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert sql.endswith("FOR SHARE OF quiz_rounds")
    assert "LEFT OUTER JOIN participants" in sql
    assert "LEFT OUTER JOIN question_options" in sql


def test_reveal_round_lock_compiles_to_postgresql_for_update() -> None:
    statement = select(QuizRound).where(QuizRound.id == UUID(int=1)).with_for_update()

    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert sql.endswith("FOR UPDATE")
