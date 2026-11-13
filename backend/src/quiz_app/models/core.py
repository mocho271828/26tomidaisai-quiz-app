from datetime import datetime
from typing import ClassVar
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Column, DateTime, Enum, Text, UniqueConstraint, text
from sqlmodel import Field, SQLModel

from quiz_app.models.enums import EventState, RankingVisibility


class ScoringPolicy(SQLModel, table=True):
    __tablename__: ClassVar[str] = "scoring_policies"
    __table_args__ = (
        CheckConstraint("base_points >= 0", name="ck_scoring_policies_base_points"),
        CheckConstraint("standard_duration_ms > 0", name="ck_scoring_policies_duration"),
        CheckConstraint("speed_bucket_ms > 0", name="ck_scoring_policies_bucket"),
        CheckConstraint(
            "timing_basis IN ('server_received_v1', 'client_monotonic_v1')",
            name="ck_scoring_policies_timing_basis",
        ),
        CheckConstraint("arrival_grace_ms >= 0", name="ck_scoring_policies_arrival_grace"),
        CheckConstraint("clock_tolerance_ms >= 0", name="ck_scoring_policies_clock_tolerance"),
        CheckConstraint(
            "client_ahead_tolerance_ms >= 0", name="ck_scoring_policies_client_ahead_tolerance"
        ),
        CheckConstraint(
            "timing_basis != 'server_received_v1' OR client_ahead_tolerance_ms = 0",
            name="ck_scoring_policies_legacy_client_ahead",
        ),
        CheckConstraint(
            "timing_basis != 'server_received_v1' OR "
            "(arrival_grace_ms = 0 AND clock_tolerance_ms = 0)",
            name="ck_scoring_policies_legacy_timing",
        ),
    )

    version: str = Field(primary_key=True, max_length=64)
    base_points: int
    initial_speed_bonus: int
    speed_bucket_ms: int
    speed_decrement_per_bucket: int
    standard_duration_ms: int
    timing_basis: str = Field(default="server_received_v1", max_length=32)
    arrival_grace_ms: int = Field(default=0)
    clock_tolerance_ms: int = Field(default=0)
    client_ahead_tolerance_ms: int = Field(default=0)
    is_approved: bool = Field(default=False)
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("clock_timestamp()"),
        ),
    )


class DisplayNamePolicy(SQLModel, table=True):
    __tablename__: ClassVar[str] = "display_name_policies"
    __table_args__ = (
        CheckConstraint("max_code_points BETWEEN 1 AND 100", name="ck_display_name_max_length"),
    )

    version: str = Field(primary_key=True, max_length=64)
    notice_version: str = Field(max_length=64)
    max_code_points: int = Field(default=20)
    reject_patterns_json: str = Field(default="[]", sa_column=Column(Text, nullable=False))
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("clock_timestamp()"),
        ),
    )


class Event(SQLModel, table=True):
    __tablename__: ClassVar[str] = "events"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_events_version"),
        UniqueConstraint("slug", name="uq_events_slug"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    slug: str = Field(max_length=80)
    title: str = Field(max_length=160)
    state: EventState = Field(
        default=EventState.DRAFT,
        sa_column=Column(
            Enum(
                EventState,
                name="event_state",
                native_enum=False,
                create_constraint=True,
                values_callable=lambda values: [value.value for value in values],
                length=32,
            ),
            nullable=False,
        ),
    )
    ranking_visibility: RankingVisibility = Field(
        default=RankingVisibility.HIDDEN_ALL,
        sa_column=Column(
            Enum(
                RankingVisibility,
                name="ranking_visibility",
                native_enum=False,
                create_constraint=True,
                values_callable=lambda values: [value.value for value in values],
                length=24,
            ),
            nullable=False,
        ),
    )
    scoring_policy_version: str = Field(foreign_key="scoring_policies.version", max_length=64)
    display_name_policy_version: str = Field(
        foreign_key="display_name_policies.version",
        max_length=64,
    )
    version: int = Field(default=1)
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("clock_timestamp()"),
        ),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("clock_timestamp()"),
        ),
    )
