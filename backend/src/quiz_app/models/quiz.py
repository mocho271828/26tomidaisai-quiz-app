from datetime import datetime
from decimal import Decimal
from typing import ClassVar
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlmodel import Field, SQLModel

from quiz_app.models.enums import (
    RoundResultPublication,
    RoundState,
    ScoreExclusionReason,
    SpeedCohort,
)


class Question(SQLModel, table=True):
    __tablename__: ClassVar[str] = "questions"
    __table_args__ = (
        UniqueConstraint("event_id", "version", name="uq_questions_event_version"),
        CheckConstraint("version > 0", name="ck_questions_version"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    event_id: UUID = Field(foreign_key="events.id", ondelete="CASCADE")
    version: int
    prompt: str = Field(sa_column=Column(Text, nullable=False))
    explanation: str = Field(default="", sa_column=Column(Text, nullable=False))
    image_url: str | None = Field(default=None, max_length=512)
    image_alt: str | None = Field(default=None, max_length=300)
    display_order: int
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("clock_timestamp()"),
        ),
    )


class QuestionOption(SQLModel, table=True):
    __tablename__: ClassVar[str] = "question_options"
    __table_args__ = (
        UniqueConstraint("question_id", "label", name="uq_question_options_label"),
        UniqueConstraint("question_id", "display_order", name="uq_question_options_order"),
        CheckConstraint("display_order >= 0", name="ck_question_options_order"),
        Index(
            "uq_question_options_one_correct",
            "question_id",
            unique=True,
            postgresql_where=text("is_correct"),
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    question_id: UUID = Field(foreign_key="questions.id", ondelete="CASCADE")
    label: str = Field(max_length=8)
    content: str = Field(sa_column=Column(Text, nullable=False))
    image_url: str | None = Field(default=None, max_length=512)
    image_alt: str | None = Field(default=None, max_length=300)
    display_order: int
    is_correct: bool = Field(default=False)


class QuizRound(SQLModel, table=True):
    __tablename__: ClassVar[str] = "quiz_rounds"
    __table_args__ = (
        UniqueConstraint("event_id", "version", name="uq_quiz_rounds_event_version"),
        CheckConstraint("version > 0", name="ck_quiz_rounds_version"),
        CheckConstraint(
            "standard_close_at IS NULL OR open_at IS NULL OR standard_close_at > open_at",
            name="ck_quiz_rounds_standard_close",
        ),
        Index(
            "uq_quiz_rounds_one_active_per_event",
            "event_id",
            unique=True,
            postgresql_where=text("state NOT IN ('Voided', 'RoundBackupValidated')"),
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    event_id: UUID = Field(foreign_key="events.id", ondelete="CASCADE")
    question_id: UUID = Field(foreign_key="questions.id", ondelete="RESTRICT")
    version: int
    state: RoundState = Field(
        default=RoundState.PREPARING_ASSETS,
        sa_column=Column(
            Enum(
                RoundState,
                name="round_state",
                native_enum=False,
                create_constraint=True,
                values_callable=lambda values: [value.value for value in values],
                length=40,
            ),
            nullable=False,
        ),
    )
    result_publication: RoundResultPublication = Field(
        default=RoundResultPublication.WITHHELD,
        sa_column=Column(
            Enum(
                RoundResultPublication,
                name="round_result_publication",
                native_enum=False,
                create_constraint=True,
                values_callable=lambda values: [value.value for value in values],
                length=16,
            ),
            nullable=False,
        ),
    )
    open_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    standard_close_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    revealed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    void_reason: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("clock_timestamp()"),
        ),
    )


class ReadinessReport(SQLModel, table=True):
    __tablename__: ClassVar[str] = "readiness_reports"
    __table_args__ = (
        UniqueConstraint("participant_id", "round_id", name="uq_readiness_participant_round"),
        CheckConstraint("round_version > 0", name="ck_readiness_round_version"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    participant_id: UUID = Field(foreign_key="participants.id", ondelete="CASCADE")
    round_id: UUID = Field(foreign_key="quiz_rounds.id", ondelete="CASCADE")
    round_version: int
    is_ready: bool
    has_asset_failure: bool = Field(default=False)
    reported_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("clock_timestamp()"),
        ),
    )
    last_seen_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("clock_timestamp()"),
        ),
    )


class RoundPreparationTarget(SQLModel, table=True):
    __tablename__: ClassVar[str] = "round_preparation_targets"
    __table_args__ = (
        UniqueConstraint(
            "round_id",
            "participant_id",
            name="uq_round_preparation_target",
        ),
        Index("ix_round_preparation_targets_round", "round_id"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    round_id: UUID = Field(foreign_key="quiz_rounds.id", ondelete="CASCADE")
    participant_id: UUID = Field(foreign_key="participants.id", ondelete="CASCADE")
    captured_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("clock_timestamp()"),
        ),
    )


class Answer(SQLModel, table=True):
    __tablename__: ClassVar[str] = "answers"
    __table_args__ = (
        UniqueConstraint("participant_id", "round_id", name="uq_answers_participant_round"),
        UniqueConstraint(
            "participant_id",
            "client_answer_id",
            name="uq_answers_participant_client_id",
        ),
        Index("ix_answers_round_received", "round_id", "received_at"),
        CheckConstraint(
            "client_elapsed_ms IS NULL OR client_elapsed_ms >= 0",
            name="ck_answers_client_elapsed",
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    participant_id: UUID = Field(foreign_key="participants.id", ondelete="RESTRICT")
    round_id: UUID = Field(foreign_key="quiz_rounds.id", ondelete="RESTRICT")
    option_id: UUID = Field(foreign_key="question_options.id", ondelete="RESTRICT")
    client_answer_id: UUID
    client_elapsed_ms: int | None = Field(default=None)
    received_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("clock_timestamp()"),
        ),
    )


class ScoreLedger(SQLModel, table=True):
    __tablename__: ClassVar[str] = "score_ledgers"
    __table_args__ = (
        UniqueConstraint("round_id", "participant_id", name="uq_score_ledgers_round_participant"),
        CheckConstraint("base_points >= 0", name="ck_score_ledgers_base"),
        CheckConstraint("speed_points >= 0", name="ck_score_ledgers_speed"),
        CheckConstraint("total_points = base_points + speed_points", name="ck_score_ledgers_total"),
        CheckConstraint(
            "speed_rank IS NULL OR speed_rank > 0",
            name="ck_score_ledgers_speed_rank",
        ),
        CheckConstraint(
            "speed_cohort_size IS NULL OR speed_cohort_size > 0",
            name="ck_score_ledgers_cohort_size",
        ),
        CheckConstraint(
            "speed_rank IS NULL OR speed_cohort_size IS NULL OR speed_rank <= speed_cohort_size",
            name="ck_score_ledgers_rank_within_cohort",
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    round_id: UUID = Field(foreign_key="quiz_rounds.id", ondelete="RESTRICT")
    participant_id: UUID = Field(foreign_key="participants.id", ondelete="RESTRICT")
    scoring_policy_version: str = Field(foreign_key="scoring_policies.version", max_length=64)
    is_correct: bool
    base_points: int
    speed_points: int
    total_points: int
    elapsed_ms: int | None = None
    speed_bucket: int | None = None
    speed_rank: int | None = None
    speed_cohort_size: int | None = None
    speed_cohort: SpeedCohort = Field(
        sa_column=Column(
            Enum(
                SpeedCohort,
                name="speed_cohort",
                native_enum=False,
                create_constraint=True,
                values_callable=lambda values: [value.value for value in values],
                length=16,
            ),
            nullable=False,
        ),
    )
    exclusion_reason: ScoreExclusionReason = Field(
        default=ScoreExclusionReason.NONE,
        sa_column=Column(
            Enum(
                ScoreExclusionReason,
                name="score_exclusion_reason",
                native_enum=False,
                create_constraint=True,
                values_callable=lambda values: [value.value for value in values],
                length=16,
            ),
            nullable=False,
        ),
    )


class RoundOptionStat(SQLModel, table=True):
    __tablename__: ClassVar[str] = "round_option_stats"
    __table_args__ = (
        UniqueConstraint("round_id", "option_id", name="uq_round_option_stats"),
        CheckConstraint("valid_response_count >= 0", name="ck_round_option_stats_count"),
        CheckConstraint(
            "response_percentage >= 0 AND response_percentage <= 100",
            name="ck_round_option_stats_percentage",
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    round_id: UUID = Field(foreign_key="quiz_rounds.id", ondelete="CASCADE")
    option_id: UUID = Field(foreign_key="question_options.id", ondelete="RESTRICT")
    valid_response_count: int
    response_percentage: Decimal = Field(
        sa_column=Column(Numeric(5, 2), nullable=False),
    )


class RankSnapshot(SQLModel, table=True):
    __tablename__: ClassVar[str] = "rank_snapshots"
    __table_args__ = (
        UniqueConstraint("event_id", "version", name="uq_rank_snapshots_event_version"),
        CheckConstraint("version > 0", name="ck_rank_snapshots_version"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    event_id: UUID = Field(foreign_key="events.id", ondelete="CASCADE")
    round_id: UUID = Field(foreign_key="quiz_rounds.id", ondelete="RESTRICT")
    version: int
    generated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("clock_timestamp()"),
        ),
    )


class RankSnapshotRow(SQLModel, table=True):
    __tablename__: ClassVar[str] = "rank_snapshot_rows"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "public_label", name="uq_rank_rows_public_label"),
        CheckConstraint("overall_rank > 0", name="ck_rank_rows_rank"),
        CheckConstraint("total_points >= 0", name="ck_rank_rows_points"),
        CheckConstraint("correct_count >= 0", name="ck_rank_rows_correct_count"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    snapshot_id: UUID = Field(foreign_key="rank_snapshots.id", ondelete="CASCADE")
    public_label: str = Field(max_length=80)
    overall_rank: int
    total_points: int
    correct_count: int
