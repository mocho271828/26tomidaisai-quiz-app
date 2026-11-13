"""開催設定と独立した結果表示状態を保存する（適用は人手）。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260923_0003"
down_revision: str | None = "20260804_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_quiz_configurations",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("settings_json", sa.Text(), nullable=False),
        sa.Column("distribution_visible", sa.Boolean(), nullable=False),
        sa.Column("personal_ranking_visible", sa.Boolean(), nullable=False),
        sa.Column("overall_ranking_visible", sa.Boolean(), nullable=False),
        sa.Column("last_applied_round", sa.Integer(), nullable=False),
        sa.CheckConstraint("last_applied_round >= 0", name="ck_event_quiz_configurations_round"),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("event_id"),
    )


def downgrade() -> None:
    # 設定の削除で進行を再解釈しないよう、自動rollbackは提供しない。
    raise RuntimeError("quiz_configuration_downgrade_requires_manual_review")
