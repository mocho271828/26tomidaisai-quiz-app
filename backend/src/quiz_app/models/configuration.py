from typing import ClassVar
from uuid import UUID

from sqlalchemy import CheckConstraint, Column, Text
from sqlmodel import Field, SQLModel


class EventQuizConfiguration(SQLModel, table=True):
    __tablename__: ClassVar[str] = "event_quiz_configurations"
    __table_args__ = (
        CheckConstraint("last_applied_round >= 0", name="ck_event_quiz_configurations_round"),
    )

    event_id: UUID = Field(primary_key=True, foreign_key="events.id", ondelete="RESTRICT")
    settings_json: str = Field(sa_column=Column(Text, nullable=False))
    distribution_visible: bool
    personal_ranking_visible: bool
    overall_ranking_visible: bool
    last_applied_round: int = Field(default=0)
