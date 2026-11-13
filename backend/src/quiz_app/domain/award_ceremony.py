"""終了後の表彰公開を、採点と通常順位の表示設定から独立して進める。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlmodel import Session

from quiz_app.domain.errors import (
    DomainInvariantError,
    InvalidStateTransitionError,
    ResourceNotFoundError,
    VersionConflictError,
)
from quiz_app.models import Event, QuizRound, RankSnapshot
from quiz_app.models.enums import EventState, RoundResultPublication, RoundState
from quiz_app.quiz_configuration import (
    ResultVisibility,
    require_event_configuration,
    visibility_state,
)
from quiz_app.quiz_configuration.contracts import AwardCeremonyPhase
from quiz_app.quiz_configuration.persistence import set_award_ceremony_configuration


@dataclass(frozen=True, slots=True)
class AwardCeremonyChange:
    event_id: UUID
    event_version: int
    previous_phase: AwardCeremonyPhase
    phase: AwardCeremonyPhase
    result_visibility: ResultVisibility


def require_award_snapshot(session: Session, event_id: UUID) -> RankSnapshot:
    """最後に終了した公開済み問題の保存順位だけを表彰の正本にする。"""

    round_ = session.scalar(
        select(QuizRound)
        .where(QuizRound.event_id == event_id)
        .order_by(QuizRound.version.desc())
        .limit(1)
    )
    if (
        round_ is None
        or round_.state is not RoundState.ROUND_BACKUP_VALIDATED
        or round_.result_publication is not RoundResultPublication.PUBLISHED
    ):
        raise DomainInvariantError("award_ceremony_final_result_unavailable")
    snapshot = session.scalar(
        select(RankSnapshot)
        .where(RankSnapshot.event_id == event_id)
        .order_by(RankSnapshot.version.desc())
        .limit(1)
    )
    if snapshot is None or snapshot.round_id != round_.id:
        raise DomainInvariantError("award_ceremony_ranking_unavailable")
    return snapshot


def set_award_ceremony_in_transaction(
    session: Session,
    *,
    event_id: UUID,
    expected_version: int,
    phase: Literal["Announced", "Finished"],
) -> AwardCeremonyChange:
    """Completed内で待機・呼出し・終了の順序を固定し、再要求で版を増やさない。"""

    event = session.scalar(select(Event).where(Event.id == event_id).with_for_update())
    if event is None:
        raise ResourceNotFoundError("event_not_found")
    if event.version != expected_version:
        raise VersionConflictError("event_version_conflict")
    if event.state is not EventState.COMPLETED:
        raise InvalidStateTransitionError("award_ceremony_requires_completed_event")
    configuration = require_event_configuration(session, event)
    previous_phase = configuration.award_ceremony_phase
    if previous_phase != phase:
        if (previous_phase, phase) not in {("Waiting", "Announced"), ("Announced", "Finished")}:
            raise InvalidStateTransitionError("award_ceremony_invalid_transition")
        if phase == "Announced":
            require_award_snapshot(session, event_id)
        database_time = session.scalar(select(func.clock_timestamp()))
        if not isinstance(database_time, datetime):
            raise DomainInvariantError("database_clock_unavailable")
        set_award_ceremony_configuration(configuration, phase)
        event.version += 1
        event.updated_at = database_time
    return AwardCeremonyChange(
        event_id=event.id,
        event_version=event.version,
        previous_phase=previous_phase,
        phase=phase,
        result_visibility=visibility_state(configuration.record),
    )
