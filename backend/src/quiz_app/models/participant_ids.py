from typing import ClassVar
from uuid import UUID

from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlmodel import Field, SQLModel

PARTICIPANT_ID_CAPACITY = 26**3
PARTICIPANT_ID_POOL_VERSION = "shuffled-v1"


class ParticipantIdPool(SQLModel, table=True):
    """人手の開催準備時に固定する表示IDリストの管理情報。"""

    __tablename__: ClassVar[str] = "participant_id_pools"
    __table_args__ = (
        CheckConstraint("capacity BETWEEN 0 AND 17576", name="ck_participant_id_pools_capacity"),
        CheckConstraint("version = 'shuffled-v1'", name="ck_participant_id_pools_version"),
    )

    event_id: UUID = Field(primary_key=True, foreign_key="events.id", ondelete="RESTRICT")
    capacity: int = Field(ge=0, le=PARTICIPANT_ID_CAPACITY)
    version: str = Field(default=PARTICIPANT_ID_POOL_VERSION, max_length=32)


class ParticipantIdSlot(SQLModel, table=True):
    """開催sequenceの1始まり添字に対応する不変の表示ID。"""

    __tablename__: ClassVar[str] = "participant_id_slots"
    __table_args__ = (
        UniqueConstraint("event_id", "public_label", name="uq_participant_id_slots_event_label"),
        CheckConstraint("ordinal BETWEEN 1 AND 17576", name="ck_participant_id_slots_ordinal"),
        # 既存SQLite単体fixtureではPostgreSQL専用の正規表現演算子を発行しない。
        CheckConstraint("public_label ~ '^[A-Z]{3}$'", name="ck_participant_id_slots_label").ddl_if(
            dialect="postgresql"
        ),
    )

    event_id: UUID = Field(
        primary_key=True, foreign_key="participant_id_pools.event_id", ondelete="RESTRICT"
    )
    ordinal: int = Field(primary_key=True, ge=1, le=PARTICIPANT_ID_CAPACITY)
    public_label: str = Field(min_length=3, max_length=3)
