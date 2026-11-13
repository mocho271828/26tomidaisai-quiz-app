"""確定済み結果を一括読取りし、送信先principalだけのDTOへ変換する。"""

from collections.abc import Mapping
from dataclasses import dataclass
from time import monotonic
from types import MappingProxyType
from uuid import UUID

from sqlalchemy import Engine, select
from sqlmodel import Session

from quiz_app.audience.auth_service import ParticipantPrincipal
from quiz_app.audience.contracts import (
    ParticipantRankingResponse,
    ParticipantTopRankingRowResponse,
    PersonalResultPayload,
    PersonalResultRevision,
    RankingRowResponse,
    ScoredParticipantOutcomeResponse,
    UnansweredParticipantOutcomeResponse,
)
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
from quiz_app.models.enums import RoundResultPublication
from quiz_app.realtime.projection_budget import get_projection_read_budget
from quiz_app.realtime.state_cache import configure_projection_read_timeout


@dataclass(frozen=True, slots=True)
class ResultBatchKey:
    event_id: UUID
    round_id: UUID | None
    round_version: int | None
    ranking_version: int | None

    @classmethod
    def from_revision(cls, revision: PersonalResultRevision) -> "ResultBatchKey":
        return cls(
            revision.event_id,
            revision.result_round_id,
            revision.result_round_version,
            revision.ranking_snapshot_version,
        )


@dataclass(frozen=True, slots=True)
class ResultBatch:
    key: ResultBatchKey
    choices: Mapping[UUID, UUID]
    outcomes: Mapping[UUID, ScoredParticipantOutcomeResponse]
    rankings: Mapping[str, RankingRowResponse]
    top_rows: tuple[ParticipantTopRankingRowResponse, ...]

    def for_participant(
        self, principal: ParticipantPrincipal, revision: PersonalResultRevision
    ) -> PersonalResultPayload:
        if principal.event_id != self.key.event_id:
            raise DomainInvariantError("personal_result_principal_mismatch")
        if ResultBatchKey.from_revision(revision) != self.key:
            raise DomainInvariantError("personal_result_batch_mismatch")
        outcome = None
        if self.key.round_id is not None:
            stored = self.outcomes.get(principal.participant_id)
            if stored is None:
                outcome = UnansweredParticipantOutcomeResponse(status="Unanswered")
            else:
                outcome = stored.model_copy(deep=True)
                if not revision.result_visibility.personal_ranking:
                    outcome.speed_rank = None
                    outcome.speed_cohort_size = None
        ranking = None
        row = self.rankings.get(principal.public_label)
        if row is not None and revision.result_visibility.personal_ranking:
            if self.key.ranking_version is None:
                raise DomainInvariantError("personal_ranking_version_missing")
            ranking = ParticipantRankingResponse(
                snapshot_version=self.key.ranking_version,
                overall_rank=row.overall_rank,
                total_points=row.total_points,
                correct_count=row.correct_count,
                top_rows=(
                    [value.model_copy(deep=True) for value in self.top_rows]
                    if revision.result_visibility.overall_ranking
                    else None
                ),
            )
        return PersonalResultPayload(
            selected_option_id=self.choices.get(principal.participant_id),
            outcome=outcome,
            ranking=ranking,
        )


def load_result_batch(engine: Engine, key: ResultBatchKey) -> ResultBatch:
    """不変の確定版を固定回数のSELECTで取得し、長いEvent lockを取らない。"""

    started = monotonic()
    choices: dict[UUID, UUID] = {}
    outcomes: dict[UUID, ScoredParticipantOutcomeResponse] = {}
    rankings: dict[str, RankingRowResponse] = {}
    with get_projection_read_budget().read(normal=True), Session(engine) as session:
        configure_projection_read_timeout(session)
        if session.get(Event, key.event_id) is None:
            raise DomainInvariantError("personal_result_event_missing")
        if key.round_id is not None:
            round_ = session.get(QuizRound, key.round_id)
            if (
                round_ is None
                or round_.event_id != key.event_id
                or round_.version != key.round_version
                or round_.result_publication is not RoundResultPublication.PUBLISHED
            ):
                raise DomainInvariantError("personal_result_not_published")
            choices = {
                participant_id: option_id
                for participant_id, option_id in session.execute(
                    select(Answer.participant_id, Answer.option_id).where(
                        Answer.round_id == key.round_id
                    )
                )
            }
            for ledger, policy in session.execute(
                select(ScoreLedger, ScoringPolicy)
                .join(ScoringPolicy, ScoringPolicy.version == ScoreLedger.scoring_policy_version)
                .where(ScoreLedger.round_id == key.round_id)
            ):
                outcomes[ledger.participant_id] = ScoredParticipantOutcomeResponse(
                    status="Scored",
                    is_correct=ledger.is_correct,
                    base_points=ledger.base_points,
                    speed_points=ledger.speed_points,
                    total_points=ledger.total_points,
                    elapsed_ms=ledger.elapsed_ms,
                    timing_basis=policy.timing_basis,
                    scoring_policy_version=ledger.scoring_policy_version,
                    speed_cohort=ledger.speed_cohort,
                    speed_rank=ledger.speed_rank,
                    speed_cohort_size=ledger.speed_cohort_size,
                )
        if monotonic() - started >= 5.0:
            raise TimeoutError("personal_result_build_expired")
        if key.ranking_version is not None:
            snapshot = session.scalar(
                select(RankSnapshot)
                .join(QuizRound, QuizRound.id == RankSnapshot.round_id)
                .where(
                    QuizRound.result_publication == RoundResultPublication.PUBLISHED,
                    RankSnapshot.event_id == key.event_id,
                    RankSnapshot.version == key.ranking_version,
                )
            )
            if snapshot is None:
                raise DomainInvariantError("personal_result_ranking_missing")
            for row in session.scalars(
                select(RankSnapshotRow).where(RankSnapshotRow.snapshot_id == snapshot.id)
            ):
                rankings[row.public_label] = RankingRowResponse(
                    public_label=row.public_label,
                    overall_rank=row.overall_rank,
                    total_points=row.total_points,
                    correct_count=row.correct_count,
                )
    if monotonic() - started >= 5.0:
        raise TimeoutError("personal_result_build_expired")
    top = sorted(
        (row for row in rankings.values() if row.overall_rank <= 3),
        key=lambda row: (row.overall_rank, row.public_label),
    )
    return ResultBatch(
        key,
        MappingProxyType(choices),
        MappingProxyType(outcomes),
        MappingProxyType(rankings),
        tuple(ParticipantTopRankingRowResponse(**row.model_dump()) for row in top),
    )
