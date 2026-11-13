"""端末回答時間と受付検証設定を追加する（適用は人手）。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261015_0005"
down_revision: str | None = "20261002_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 既存台帳を再解釈せず、旧policyの方式と許容値だけを明示する。
    op.add_column("answers", sa.Column("client_elapsed_ms", sa.Integer(), nullable=True))
    op.create_check_constraint("ck_answers_client_elapsed", "answers", "client_elapsed_ms >= 0")
    op.add_column(
        "scoring_policies",
        sa.Column(
            "timing_basis",
            sa.String(length=32),
            nullable=False,
            server_default="server_received_v1",
        ),
    )
    op.add_column(
        "scoring_policies",
        sa.Column("arrival_grace_ms", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "scoring_policies",
        sa.Column("clock_tolerance_ms", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_scoring_policies_timing_basis",
        "scoring_policies",
        "timing_basis IN ('server_received_v1', 'client_monotonic_v1')",
    )
    op.create_check_constraint(
        "ck_scoring_policies_arrival_grace", "scoring_policies", "arrival_grace_ms >= 0"
    )
    op.create_check_constraint(
        "ck_scoring_policies_clock_tolerance", "scoring_policies", "clock_tolerance_ms >= 0"
    )
    op.create_check_constraint(
        "ck_scoring_policies_legacy_timing",
        "scoring_policies",
        "timing_basis != 'server_received_v1' OR (arrival_grace_ms = 0 AND clock_tolerance_ms = 0)",
    )


def downgrade() -> None:
    # 正式回答時間を消す操作は、自動rollbackで実行しない。
    raise RuntimeError("client_answer_timing_downgrade_requires_manual_review")
