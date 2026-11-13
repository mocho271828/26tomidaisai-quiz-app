"""補助回答を廃止し、集計完了と手動公開を分離する。"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261015_0007"
down_revision: str | None = "20261015_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 旧補助回答を通常回答へ読み替えたり、開催中の回答期限を変更したりしない。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM participants WHERE is_assisted IS TRUE)
               OR EXISTS (SELECT 1 FROM score_ledgers WHERE exclusion_reason = 'Assisted')
            THEN
                RAISE EXCEPTION 'manual_results_retired_data_requires_review';
            END IF;
            IF EXISTS (SELECT 1 FROM events WHERE state IN
                ('InProgress', 'Suspended', 'DataStoreUnavailable', 'ServiceLost'))
               OR EXISTS (SELECT 1 FROM quiz_rounds WHERE state IN
                ('Scheduled', 'Open', 'StandardClosedAssistedOpen', 'AwaitingValidation'))
            THEN
                RAISE EXCEPTION 'manual_results_active_event_requires_review';
            END IF;
        END $$;
        """
    )
    op.drop_constraint("round_state", "quiz_rounds", type_="check")
    op.create_check_constraint(
        "round_state",
        "quiz_rounds",
        "state IN ('PreparingAssets', 'ReadyToSchedule', 'Scheduled', 'Open', "
        "'AwaitingValidation', 'ReadyToReveal', 'VoidCandidate', 'Reveal', 'Voided', "
        "'RoundBackupPending', 'RoundBackupValidated', 'RoundBackupFailed', 'RoundBackupRetrying')",
    )
    op.drop_constraint("score_exclusion_reason", "score_ledgers", type_="check")
    op.create_check_constraint(
        "score_exclusion_reason",
        "score_ledgers",
        "exclusion_reason IN ('None', 'Wrong', 'Void', 'Ineligible')",
    )
    op.drop_constraint("ck_quiz_rounds_assisted_close", "quiz_rounds", type_="check")
    op.drop_column("quiz_rounds", "assisted_close_at")
    op.drop_column("participants", "is_assisted")


def downgrade() -> None:
    # 廃止した参加者区分や受付期限を推測して復元しない。
    raise RuntimeError("manual_results_downgrade_requires_manual_review")
