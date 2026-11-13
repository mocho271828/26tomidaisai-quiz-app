"""人手で準備した開催別固定リストを、DB sequenceの添字で参照する。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session

from quiz_app.audience.errors import AudienceServiceUnavailableError, ParticipantIdExhaustedError
from quiz_app.models.participant_ids import PARTICIPANT_ID_CAPACITY, PARTICIPANT_ID_POOL_VERSION
from quiz_app.public_label import PublicLabel

# nextvalを一度だけ評価する。text経由で遅延解決し、容量0で存在しないsequenceをbind時に参照しない。
ALLOCATE_PARTICIPANT_ID = text("""
    WITH allocation AS MATERIALIZED (
        SELECT capacity, version,
               CASE WHEN capacity BETWEEN 1 AND :maximum_capacity AND version = :pool_version
                    THEN nextval(CAST(CAST(:sequence_name AS text) AS regclass)) END AS ordinal
        FROM public.participant_id_pools WHERE event_id = :event_id
    )
    SELECT allocation.capacity, allocation.version, allocation.ordinal, slot.public_label
    FROM allocation
    LEFT JOIN public.participant_id_slots AS slot
      ON slot.event_id = :event_id AND slot.ordinal = allocation.ordinal
""")


class AllocatedParticipantId(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capacity: StrictInt = Field(ge=0, le=PARTICIPANT_ID_CAPACITY)
    version: StrictStr
    ordinal: StrictInt | None = Field(ge=1, le=PARTICIPANT_ID_CAPACITY)
    public_label: PublicLabel | None


def allocate_participant_id(session: Session, event_id: UUID) -> str:
    """新規参加だけ1往復で採番する。rollbackの欠番は回収せず、循環・再試行しない。"""

    try:
        row = (
            session.execute(
                ALLOCATE_PARTICIPANT_ID,
                {
                    "event_id": event_id,
                    "sequence_name": f"public.participant_id_{event_id.hex}",
                    "maximum_capacity": PARTICIPANT_ID_CAPACITY,
                    "pool_version": PARTICIPANT_ID_POOL_VERSION,
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise AudienceServiceUnavailableError("participant_id_pool_not_prepared")
        allocation = AllocatedParticipantId.model_validate(row)
        if allocation.version != PARTICIPANT_ID_POOL_VERSION:
            raise AudienceServiceUnavailableError("participant_id_pool_version_invalid")
        if allocation.capacity == 0:
            raise ParticipantIdExhaustedError("participant_id_exhausted")
        if allocation.ordinal is None or allocation.ordinal > allocation.capacity:
            raise AudienceServiceUnavailableError("participant_id_sequence_invalid")
        if allocation.public_label is None:
            raise AudienceServiceUnavailableError("participant_id_slot_missing")
        return allocation.public_label
    except ValidationError as error:
        raise AudienceServiceUnavailableError("participant_id_allocation_invalid") from error
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) == "2200H":
            raise ParticipantIdExhaustedError("participant_id_exhausted") from error
        # 未適用schema・未準備sequence・権限不足を正常な参加やID衝突に変換しない。
        raise AudienceServiceUnavailableError("participant_id_allocation_unavailable") from error
