"""運営が指定する最終問題を、開催設定の既存保存領域で管理する。"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlmodel import Session

from quiz_app.domain.errors import (
    DomainInvariantError,
    InvalidStateTransitionError,
    ResourceNotFoundError,
    VersionConflictError,
)
from quiz_app.models import Event, QuizRound
from quiz_app.models.enums import EventState, RoundState
from quiz_app.quiz_configuration import (
    ResultVisibility,
    require_event_configuration,
    visibility_state,
)
from quiz_app.quiz_configuration.persistence import set_final_round_configuration


@dataclass(frozen=True, slots=True)
class FinalRoundChange:
    event_id: UUID
    event_version: int
    previous_round_id: UUID | None
    final_round_id: UUID | None
    result_visibility: ResultVisibility


def set_final_round_in_transaction(
    session: Session,
    *,
    event_id: UUID,
    round_id: UUID,
    expected_version: int,
    is_final: bool,
) -> FinalRoundChange:
    """現在問の指定と解除だけを許可し、同じ指定の再要求で版を増やさない。"""

    event = session.scalar(select(Event).where(Event.id == event_id).with_for_update())
    if event is None:
        raise ResourceNotFoundError("event_not_found")
    if event.version != expected_version:
        raise VersionConflictError("event_version_conflict")
    if event.state is not EventState.IN_PROGRESS:
        raise InvalidStateTransitionError("final_round_requires_in_progress_event")
    # Roundを更新しないため追加の排他lockを取らず、既存進行処理のlock順と競合させない。
    current_round = session.scalar(
        select(QuizRound)
        .where(QuizRound.event_id == event_id)
        .order_by(QuizRound.version.desc())
        .limit(1)
    )
    if current_round is None or current_round.id != round_id:
        raise InvalidStateTransitionError("final_round_must_be_current")
    if current_round.state is RoundState.VOIDED:
        raise InvalidStateTransitionError("final_round_must_not_be_voided")
    configuration = require_event_configuration(session, event)
    previous_round_id = configuration.final_round_id
    if not is_final and previous_round_id not in {None, round_id}:
        raise InvalidStateTransitionError("final_round_clear_target_mismatch")
    final_round_id = round_id if is_final else None
    if previous_round_id != final_round_id:
        database_time = session.scalar(select(func.clock_timestamp()))
        if not isinstance(database_time, datetime):
            raise DomainInvariantError("database_clock_unavailable")
        set_final_round_configuration(configuration, final_round_id)
        event.version += 1
        event.updated_at = database_time
    return FinalRoundChange(
        event_id=event.id,
        event_version=event.version,
        previous_round_id=previous_round_id,
        final_round_id=final_round_id,
        result_visibility=visibility_state(configuration.record),
    )
