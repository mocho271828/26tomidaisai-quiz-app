"""開催ごとの固定IDリストを保存する（適用・開催準備は人手）。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261002_0004"
down_revision: str | None = "20260923_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "participant_id_pools",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.CheckConstraint("capacity BETWEEN 0 AND 17576", name="ck_participant_id_pools_capacity"),
        sa.CheckConstraint("version = 'shuffled-v1'", name="ck_participant_id_pools_version"),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_table(
        "participant_id_slots",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("public_label", sa.String(length=3), nullable=False),
        sa.CheckConstraint("ordinal BETWEEN 1 AND 17576", name="ck_participant_id_slots_ordinal"),
        sa.CheckConstraint("public_label ~ '^[A-Z]{3}$'", name="ck_participant_id_slots_label"),
        sa.ForeignKeyConstraint(
            ["event_id"], ["participant_id_pools.event_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("event_id", "ordinal"),
        sa.UniqueConstraint("event_id", "public_label", name="uq_participant_id_slots_event_label"),
    )


def downgrade() -> None:
    # 固定リストと採番元の消失による再割り当てを防ぎ、人手の復旧判断に分離する。
    raise RuntimeError("participant_id_pool_downgrade_requires_manual_review")
