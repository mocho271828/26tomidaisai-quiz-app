"""参加開始の再試行を同じparticipantへ収束させる。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260804_0002"
down_revision: str | None = "20260804_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "participants",
        sa.Column("join_attempt_hash", sa.String(length=128), nullable=True),
    )
    op.create_unique_constraint(
        "uq_participants_event_join_attempt_hash",
        "participants",
        ["event_id", "join_attempt_hash"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_participants_event_join_attempt_hash",
        "participants",
        type_="unique",
    )
    op.drop_column("participants", "join_attempt_hash")
