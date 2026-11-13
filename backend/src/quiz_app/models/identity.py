from datetime import datetime
from typing import ClassVar
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Column, DateTime, Enum, Index, UniqueConstraint, text
from sqlmodel import Field, SQLModel

from quiz_app.models.enums import AdminRole, PublicLabelKind


class Participant(SQLModel, table=True):
    __tablename__: ClassVar[str] = "participants"
    __table_args__ = (
        UniqueConstraint("event_id", "public_label", name="uq_participants_event_public_label"),
        UniqueConstraint(
            "event_id",
            "display_name_uniqueness_key",
            name="uq_participants_event_display_name_key",
        ),
        UniqueConstraint(
            "event_id",
            "join_attempt_hash",
            name="uq_participants_event_join_attempt_hash",
        ),
        Index("ix_participants_event_joined_at", "event_id", "joined_at"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    event_id: UUID = Field(foreign_key="events.id", ondelete="CASCADE")
    public_label: str = Field(max_length=32)
    public_label_kind: PublicLabelKind = Field(
        default=PublicLabelKind.RANDOM_ID,
        sa_column=Column(
            Enum(
                PublicLabelKind,
                name="public_label_kind",
                native_enum=False,
                create_constraint=True,
                values_callable=lambda values: [value.value for value in values],
                length=24,
            ),
            nullable=False,
        ),
    )
    display_name: str | None = Field(default=None, max_length=80)
    display_name_uniqueness_key: str | None = Field(default=None, max_length=160)
    join_attempt_hash: str | None = Field(default=None, max_length=128)
    is_ranking_eligible: bool = Field(default=True)
    joined_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("clock_timestamp()"),
        ),
    )
    first_answer_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )


class DisplayNameConsent(SQLModel, table=True):
    __tablename__: ClassVar[str] = "display_name_consents"

    participant_id: UUID = Field(
        primary_key=True,
        foreign_key="participants.id",
        ondelete="CASCADE",
    )
    notice_version: str = Field(max_length=64)
    policy_version: str = Field(foreign_key="display_name_policies.version", max_length=64)
    consented_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("clock_timestamp()"),
        ),
    )


class ParticipantSession(SQLModel, table=True):
    __tablename__: ClassVar[str] = "participant_sessions"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_participant_sessions_token_hash"),
        Index("ix_participant_sessions_participant_expires", "participant_id", "expires_at"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    participant_id: UUID = Field(foreign_key="participants.id", ondelete="CASCADE")
    token_hash: str = Field(max_length=128)
    issued_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("clock_timestamp()"),
        ),
    )
    expires_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    revoked_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    last_seen_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("clock_timestamp()"),
        ),
    )


class AdminAccount(SQLModel, table=True):
    __tablename__: ClassVar[str] = "admin_accounts"
    __table_args__ = (UniqueConstraint("username", name="uq_admin_accounts_username"),)

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    username: str = Field(max_length=80)
    password_hash: str = Field(max_length=256)
    role: AdminRole = Field(
        default=AdminRole.OPERATOR,
        sa_column=Column(
            Enum(
                AdminRole,
                name="admin_role",
                native_enum=False,
                create_constraint=True,
                values_callable=lambda values: [value.value for value in values],
                length=24,
            ),
            nullable=False,
        ),
    )
    password_policy_version: str = Field(max_length=64)
    is_demo: bool = Field(default=False)
    is_revoked: bool = Field(default=False)
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("clock_timestamp()"),
        ),
    )


class AdminSession(SQLModel, table=True):
    __tablename__: ClassVar[str] = "admin_sessions"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_admin_sessions_token_hash"),
        Index("ix_admin_sessions_account_expires", "account_id", "expires_at"),
        CheckConstraint("expires_at > issued_at", name="ck_admin_sessions_expiry"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    account_id: UUID = Field(foreign_key="admin_accounts.id", ondelete="CASCADE")
    token_hash: str = Field(max_length=128)
    csrf_hash: str = Field(max_length=128)
    issued_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("clock_timestamp()"),
        ),
    )
    expires_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    revoked_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    last_action_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("clock_timestamp()"),
        ),
    )
