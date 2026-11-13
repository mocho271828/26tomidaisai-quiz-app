from functools import lru_cache
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import Engine, select
from sqlmodel import Session

from quiz_app.db import get_engine
from quiz_app.domain.errors import DomainInvariantError, ResourceNotFoundError
from quiz_app.models import Event, QuizRound


class SingleEventConfigurationError(DomainInvariantError):
    """Eventが単一開催の前提を満たさず、安全に対象を選べない。"""


class SingleEventService:
    """外部入力にEvent識別子を要求せず、DB上の唯一Eventを解決する。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get_event_id(self) -> UUID:
        with Session(self._engine) as session:
            return resolve_single_event_id(session)

    def require_round(self, round_id: UUID) -> UUID:
        """Roundが唯一Eventに属する場合だけ、そのEvent IDを返す。"""

        with Session(self._engine) as session:
            event_id = resolve_single_event_id(session)
            round_event_id = session.scalar(
                select(QuizRound.event_id).where(QuizRound.id == round_id)
            )
            if round_event_id != event_id:
                raise ResourceNotFoundError("round_not_found")
            return event_id


def resolve_single_event_id(session: Session) -> UUID:
    """0件・複数件を区別せずfail-closedにし、唯一EventのIDを返す。"""

    event_ids = list(session.scalars(select(Event.id).order_by(Event.id).limit(2)).all())
    if len(event_ids) != 1:
        raise SingleEventConfigurationError("single_event_configuration_required")
    return event_ids[0]


@lru_cache
def get_single_event_service() -> SingleEventService:
    return SingleEventService(get_engine())


def require_single_event_id(
    service: Annotated[SingleEventService, Depends(get_single_event_service)],
) -> UUID:
    return service.get_event_id()


def require_single_event_round(
    round_id: UUID,
    service: Annotated[SingleEventService, Depends(get_single_event_service)],
) -> UUID:
    return service.require_round(round_id)
