"""運営が公開した表彰順位だけを、本人情報のない共通snapshotへ載せる。"""

from sqlalchemy import select
from sqlmodel import Session

from quiz_app.audience.contracts import AwardCeremonyRankingRowResponse, AwardCeremonyResponse
from quiz_app.domain.award_ceremony import require_award_snapshot
from quiz_app.models import Event, RankSnapshotRow
from quiz_app.models.enums import EventState
from quiz_app.quiz_configuration.contracts import AwardCeremonyPhase


def award_ceremony_projection(
    session: Session, event: Event, phase: AwardCeremonyPhase
) -> AwardCeremonyResponse | None:
    """公開前・終了後は順位行を読まず、通常順位の非公開設定を維持する。"""

    if event.state is not EventState.COMPLETED:
        return None
    if phase != "Announced":
        return AwardCeremonyResponse(phase=phase, top_rows=None)
    snapshot = require_award_snapshot(session, event.id)
    rows = session.execute(
        select(RankSnapshotRow.public_label, RankSnapshotRow.overall_rank)
        .where(RankSnapshotRow.snapshot_id == snapshot.id, RankSnapshotRow.overall_rank <= 5)
        .order_by(RankSnapshotRow.overall_rank, RankSnapshotRow.public_label)
    )
    return AwardCeremonyResponse(
        phase=phase,
        top_rows=[
            AwardCeremonyRankingRowResponse(
                public_label=public_label,
                overall_rank=overall_rank,
            )
            for public_label, overall_rank in rows
        ],
    )
