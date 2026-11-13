from datetime import datetime
from typing import ClassVar
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, Index, Text, UniqueConstraint, text
from sqlmodel import Field, SQLModel


class OperationAudit(SQLModel, table=True):
    __tablename__: ClassVar[str] = "operation_audits"
    __table_args__ = (Index("ix_operation_audits_event_occurred", "event_id", "occurred_at"),)

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    event_id: UUID = Field(foreign_key="events.id", ondelete="RESTRICT")
    round_id: UUID | None = Field(default=None, foreign_key="quiz_rounds.id", ondelete="RESTRICT")
    account_id: UUID | None = Field(
        default=None,
        foreign_key="admin_accounts.id",
        ondelete="RESTRICT",
    )
    operation: str = Field(max_length=80)
    target_type: str = Field(max_length=64)
    target_id: str = Field(max_length=80)
    before_state: str | None = Field(default=None, max_length=80)
    after_state: str | None = Field(default=None, max_length=80)
    reason: str = Field(sa_column=Column(Text, nullable=False))
    outcome: str = Field(max_length=32)
    idempotency_key: UUID | None = None
    occurred_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("clock_timestamp()"),
        ),
    )


class IdempotencyRecord(SQLModel, table=True):
    __tablename__: ClassVar[str] = "idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "idempotency_key",
            name="uq_idempotency_account_key",
        ),
        Index("ix_idempotency_records_expires_at", "expires_at"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    account_id: UUID = Field(foreign_key="admin_accounts.id", ondelete="CASCADE")
    idempotency_key: UUID
    operation: str = Field(max_length=80)
    request_hash: str = Field(max_length=128)
    response_status: int
    response_json: str = Field(sa_column=Column(Text, nullable=False))
    resulting_version: int
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("clock_timestamp()"),
        ),
    )
    expires_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
