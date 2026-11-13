from functools import lru_cache
from uuid import UUID

from sqlalchemy import Engine, select
from sqlmodel import Session

from quiz_app.db import get_engine
from quiz_app.domain.errors import DomainInvariantError, ResourceNotFoundError
from quiz_app.models import Event, QuizRound
from quiz_app.operator.final_round_contracts import FinalRoundResponse
from quiz_app.quiz_configuration import require_event_configuration
from quiz_app.realtime.projection_budget import get_projection_read_budget
from quiz_app.realtime.state_cache import configure_projection_read_timeout
from quiz_app.single_event import resolve_single_event_id


class FinalRoundService:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get_final_round(self, event_id: UUID) -> FinalRoundResponse:
        """Event版と最終問を同じ共有lock内で読み、運営stateとの照合に使う。"""

        with (
            get_projection_read_budget().read(normal=True),
            Session(self._engine) as session,
        ):
            configure_projection_read_timeout(session)
            if resolve_single_event_id(session) != event_id:
                raise ResourceNotFoundError("event_not_found")
            event = session.get(Event, event_id, with_for_update={"read": True})
            if event is None:
                raise ResourceNotFoundError("event_not_found")
            configuration = require_event_configuration(session, event)
            final_round_id = configuration.final_round_id
            if final_round_id is not None:
                round_event_id = session.scalar(
                    select(QuizRound.event_id).where(QuizRound.id == final_round_id)
                )
                if round_event_id != event_id:
                    raise DomainInvariantError("final_round_event_mismatch")
            return FinalRoundResponse(
                event_id=event.id, event_version=event.version, final_round_id=final_round_id
            )


@lru_cache
def get_final_round_service() -> FinalRoundService:
    return FinalRoundService(get_engine())
