"""端末先行の許容差を分離し、保存済みpolicyの下限を保持する（適用は人手）。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261015_0006"
down_revision: str | None = "20261015_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 保存済みpolicyは旧下限のまま保持し、新しい2500msを遡及適用しない。
    op.add_column(
        "scoring_policies",
        sa.Column("client_ahead_tolerance_ms", sa.Integer(), nullable=True),
    )
    op.execute("UPDATE scoring_policies SET client_ahead_tolerance_ms = clock_tolerance_ms")
    op.alter_column(
        "scoring_policies",
        "client_ahead_tolerance_ms",
        existing_type=sa.Integer(),
        nullable=False,
        server_default="0",
    )
    op.create_check_constraint(
        "ck_scoring_policies_client_ahead_tolerance",
        "scoring_policies",
        "client_ahead_tolerance_ms >= 0",
    )
    op.create_check_constraint(
        "ck_scoring_policies_legacy_client_ahead",
        "scoring_policies",
        "timing_basis != 'server_received_v1' OR client_ahead_tolerance_ms = 0",
    )


def downgrade() -> None:
    # 下限を分離したpolicyを旧ロジックで再解釈する変更は自動実行しない。
    raise RuntimeError("client_ahead_tolerance_downgrade_requires_manual_review")
