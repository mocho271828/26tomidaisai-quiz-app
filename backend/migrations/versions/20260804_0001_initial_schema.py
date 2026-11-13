"""初回production sliceのPostgreSQL schemaを作成する。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260804_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
DB_NOW = sa.text("clock_timestamp()")


def _timestamps() -> list[sa.Column[object]]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=DB_NOW, nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "scoring_policies",
        sa.Column("version", sa.String(length=64), primary_key=True),
        sa.Column("base_points", sa.Integer(), nullable=False),
        sa.Column("initial_speed_bonus", sa.Integer(), nullable=False),
        sa.Column("speed_bucket_ms", sa.Integer(), nullable=False),
        sa.Column("speed_decrement_per_bucket", sa.Integer(), nullable=False),
        sa.Column("standard_duration_ms", sa.Integer(), nullable=False),
        sa.Column("is_approved", sa.Boolean(), server_default=sa.false(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("base_points >= 0", name="ck_scoring_policies_base_points"),
        sa.CheckConstraint("standard_duration_ms > 0", name="ck_scoring_policies_duration"),
        sa.CheckConstraint("speed_bucket_ms > 0", name="ck_scoring_policies_bucket"),
    )
    op.create_table(
        "display_name_policies",
        sa.Column("version", sa.String(length=64), primary_key=True),
        sa.Column("notice_version", sa.String(length=64), nullable=False),
        sa.Column("max_code_points", sa.Integer(), server_default="20", nullable=False),
        sa.Column("reject_patterns_json", sa.Text(), server_default="[]", nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "max_code_points BETWEEN 1 AND 100",
            name="ck_display_name_max_length",
        ),
    )
    op.create_table(
        "events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("ranking_visibility", sa.String(length=24), nullable=False),
        sa.Column("scoring_policy_version", sa.String(length=64), nullable=False),
        sa.Column("display_name_policy_version", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        *_timestamps(),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=DB_NOW, nullable=False),
        sa.ForeignKeyConstraint(["scoring_policy_version"], ["scoring_policies.version"]),
        sa.ForeignKeyConstraint(
            ["display_name_policy_version"],
            ["display_name_policies.version"],
        ),
        sa.UniqueConstraint("slug", name="uq_events_slug"),
        sa.CheckConstraint("version > 0", name="ck_events_version"),
        sa.CheckConstraint(
            "state IN ('Draft','Checked','LobbyOpen','InProgress','Suspended',"
            "'DataStoreUnavailable','ServiceLost','Completed','Aborted','Archived')",
            name="event_state",
        ),
        sa.CheckConstraint(
            "ranking_visibility IN ('HiddenAll','ParticipantOnly','Public')",
            name="ranking_visibility",
        ),
    )
    op.create_table(
        "questions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("event_id", UUID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("explanation", sa.Text(), server_default="", nullable=False),
        sa.Column("image_url", sa.String(length=512), nullable=True),
        sa.Column("image_alt", sa.String(length=300), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("event_id", "version", name="uq_questions_event_version"),
        sa.CheckConstraint("version > 0", name="ck_questions_version"),
    )
    op.create_table(
        "question_options",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("question_id", UUID, nullable=False),
        sa.Column("label", sa.String(length=8), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("image_url", sa.String(length=512), nullable=True),
        sa.Column("image_alt", sa.String(length=300), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("is_correct", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.ForeignKeyConstraint(["question_id"], ["questions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("question_id", "label", name="uq_question_options_label"),
        sa.UniqueConstraint("question_id", "display_order", name="uq_question_options_order"),
        sa.CheckConstraint("display_order >= 0", name="ck_question_options_order"),
    )
    op.create_index(
        "uq_question_options_one_correct",
        "question_options",
        ["question_id"],
        unique=True,
        postgresql_where=sa.text("is_correct"),
    )
    op.create_table(
        "quiz_rounds",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("event_id", UUID, nullable=False),
        sa.Column("question_id", UUID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("result_publication", sa.String(length=16), nullable=False),
        sa.Column("open_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("standard_close_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assisted_close_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revealed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("void_reason", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["question_id"], ["questions.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("event_id", "version", name="uq_quiz_rounds_event_version"),
        sa.CheckConstraint("version > 0", name="ck_quiz_rounds_version"),
        sa.CheckConstraint(
            "state IN ('PreparingAssets','ReadyToSchedule','Scheduled','Open',"
            "'StandardClosedAssistedOpen','AwaitingValidation','VoidCandidate','Reveal',"
            "'Voided','RoundBackupPending','RoundBackupValidated','RoundBackupFailed',"
            "'RoundBackupRetrying')",
            name="round_state",
        ),
        sa.CheckConstraint(
            "result_publication IN ('Withheld','Published')",
            name="round_result_publication",
        ),
        sa.CheckConstraint(
            "standard_close_at IS NULL OR open_at IS NULL OR standard_close_at > open_at",
            name="ck_quiz_rounds_standard_close",
        ),
        sa.CheckConstraint(
            "assisted_close_at IS NULL OR standard_close_at IS NULL "
            "OR assisted_close_at > standard_close_at",
            name="ck_quiz_rounds_assisted_close",
        ),
    )
    op.create_index(
        "uq_quiz_rounds_one_active_per_event",
        "quiz_rounds",
        ["event_id"],
        unique=True,
        postgresql_where=sa.text("state NOT IN ('Voided', 'RoundBackupValidated')"),
    )
    op.create_table(
        "participants",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("event_id", UUID, nullable=False),
        sa.Column("public_label", sa.String(length=32), nullable=False),
        sa.Column("public_label_kind", sa.String(length=24), nullable=False),
        sa.Column("display_name", sa.String(length=80), nullable=True),
        sa.Column("display_name_uniqueness_key", sa.String(length=160), nullable=True),
        sa.Column("is_ranking_eligible", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_assisted", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=DB_NOW, nullable=False),
        sa.Column("first_answer_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("event_id", "public_label", name="uq_participants_event_public_label"),
        sa.UniqueConstraint(
            "event_id",
            "display_name_uniqueness_key",
            name="uq_participants_event_display_name_key",
        ),
        sa.CheckConstraint(
            "public_label_kind IN ('RandomId','DisplayName')",
            name="public_label_kind",
        ),
    )
    op.create_index(
        "ix_participants_event_joined_at",
        "participants",
        ["event_id", "joined_at"],
    )
    op.create_table(
        "display_name_consents",
        sa.Column("participant_id", UUID, primary_key=True),
        sa.Column("notice_version", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column(
            "consented_at", sa.DateTime(timezone=True), server_default=DB_NOW, nullable=False
        ),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["policy_version"], ["display_name_policies.version"]),
    )
    _create_participant_sessions()
    _create_admin_tables()
    _create_round_data_tables()
    _create_audit_tables()


def _create_participant_sessions() -> None:
    op.create_table(
        "participant_sessions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("participant_id", UUID, nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), server_default=DB_NOW, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=DB_NOW, nullable=False
        ),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("token_hash", name="uq_participant_sessions_token_hash"),
    )
    op.create_index(
        "ix_participant_sessions_participant_expires",
        "participant_sessions",
        ["participant_id", "expires_at"],
    )


def _create_admin_tables() -> None:
    op.create_table(
        "admin_accounts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("username", sa.String(length=80), nullable=False),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
        sa.Column("role", sa.String(length=24), nullable=False),
        sa.Column("password_policy_version", sa.String(length=64), nullable=False),
        sa.Column("is_demo", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_revoked", sa.Boolean(), server_default=sa.false(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("username", name="uq_admin_accounts_username"),
        sa.CheckConstraint("role IN ('Operator','Supervisor')", name="admin_role"),
    )
    op.create_table(
        "admin_sessions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("account_id", UUID, nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("csrf_hash", sa.String(length=128), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), server_default=DB_NOW, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_action_at", sa.DateTime(timezone=True), server_default=DB_NOW, nullable=False
        ),
        sa.ForeignKeyConstraint(["account_id"], ["admin_accounts.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("token_hash", name="uq_admin_sessions_token_hash"),
        sa.CheckConstraint("expires_at > issued_at", name="ck_admin_sessions_expiry"),
    )
    op.create_index(
        "ix_admin_sessions_account_expires",
        "admin_sessions",
        ["account_id", "expires_at"],
    )


def _create_round_data_tables() -> None:
    op.create_table(
        "round_preparation_targets",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("round_id", UUID, nullable=False),
        sa.Column("participant_id", UUID, nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=DB_NOW, nullable=False),
        sa.ForeignKeyConstraint(["round_id"], ["quiz_rounds.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "round_id",
            "participant_id",
            name="uq_round_preparation_target",
        ),
    )
    op.create_index(
        "ix_round_preparation_targets_round",
        "round_preparation_targets",
        ["round_id"],
    )
    op.create_table(
        "readiness_reports",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("participant_id", UUID, nullable=False),
        sa.Column("round_id", UUID, nullable=False),
        sa.Column("round_version", sa.Integer(), nullable=False),
        sa.Column("is_ready", sa.Boolean(), nullable=False),
        sa.Column("has_asset_failure", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), server_default=DB_NOW, nullable=False),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=DB_NOW, nullable=False
        ),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["round_id"], ["quiz_rounds.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("participant_id", "round_id", name="uq_readiness_participant_round"),
        sa.CheckConstraint("round_version > 0", name="ck_readiness_round_version"),
    )
    op.create_table(
        "answers",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("participant_id", UUID, nullable=False),
        sa.Column("round_id", UUID, nullable=False),
        sa.Column("option_id", UUID, nullable=False),
        sa.Column("client_answer_id", UUID, nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=DB_NOW, nullable=False),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["round_id"], ["quiz_rounds.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["option_id"], ["question_options.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("participant_id", "round_id", name="uq_answers_participant_round"),
        sa.UniqueConstraint(
            "participant_id",
            "client_answer_id",
            name="uq_answers_participant_client_id",
        ),
    )
    op.create_index("ix_answers_round_received", "answers", ["round_id", "received_at"])
    op.create_table(
        "score_ledgers",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("round_id", UUID, nullable=False),
        sa.Column("participant_id", UUID, nullable=False),
        sa.Column("scoring_policy_version", sa.String(length=64), nullable=False),
        sa.Column("is_correct", sa.Boolean(), nullable=False),
        sa.Column("base_points", sa.Integer(), nullable=False),
        sa.Column("speed_points", sa.Integer(), nullable=False),
        sa.Column("total_points", sa.Integer(), nullable=False),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True),
        sa.Column("speed_bucket", sa.Integer(), nullable=True),
        sa.Column("speed_rank", sa.Integer(), nullable=True),
        sa.Column("speed_cohort_size", sa.Integer(), nullable=True),
        sa.Column("speed_cohort", sa.String(length=16), nullable=False),
        sa.Column("exclusion_reason", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(["round_id"], ["quiz_rounds.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["scoring_policy_version"], ["scoring_policies.version"]),
        sa.UniqueConstraint(
            "round_id",
            "participant_id",
            name="uq_score_ledgers_round_participant",
        ),
        sa.CheckConstraint("base_points >= 0", name="ck_score_ledgers_base"),
        sa.CheckConstraint("speed_points >= 0", name="ck_score_ledgers_speed"),
        sa.CheckConstraint(
            "total_points = base_points + speed_points",
            name="ck_score_ledgers_total",
        ),
        sa.CheckConstraint(
            "speed_rank IS NULL OR speed_rank > 0",
            name="ck_score_ledgers_speed_rank",
        ),
        sa.CheckConstraint(
            "speed_cohort_size IS NULL OR speed_cohort_size > 0",
            name="ck_score_ledgers_cohort_size",
        ),
        sa.CheckConstraint(
            "speed_rank IS NULL OR speed_cohort_size IS NULL OR speed_rank <= speed_cohort_size",
            name="ck_score_ledgers_rank_within_cohort",
        ),
        sa.CheckConstraint(
            "speed_cohort IN ('Correct','AllValid','Excluded')",
            name="speed_cohort",
        ),
        sa.CheckConstraint(
            "exclusion_reason IN ('None','Wrong','Assisted','Void','Ineligible')",
            name="score_exclusion_reason",
        ),
    )
    op.create_table(
        "round_option_stats",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("round_id", UUID, nullable=False),
        sa.Column("option_id", UUID, nullable=False),
        sa.Column("valid_response_count", sa.Integer(), nullable=False),
        sa.Column("response_percentage", sa.Numeric(5, 2), nullable=False),
        sa.ForeignKeyConstraint(["round_id"], ["quiz_rounds.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["option_id"], ["question_options.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("round_id", "option_id", name="uq_round_option_stats"),
        sa.CheckConstraint("valid_response_count >= 0", name="ck_round_option_stats_count"),
        sa.CheckConstraint(
            "response_percentage >= 0 AND response_percentage <= 100",
            name="ck_round_option_stats_percentage",
        ),
    )
    op.create_table(
        "rank_snapshots",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("event_id", UUID, nullable=False),
        sa.Column("round_id", UUID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "generated_at", sa.DateTime(timezone=True), server_default=DB_NOW, nullable=False
        ),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["round_id"], ["quiz_rounds.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("event_id", "version", name="uq_rank_snapshots_event_version"),
        sa.CheckConstraint("version > 0", name="ck_rank_snapshots_version"),
    )
    op.create_table(
        "rank_snapshot_rows",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("snapshot_id", UUID, nullable=False),
        sa.Column("public_label", sa.String(length=80), nullable=False),
        sa.Column("overall_rank", sa.Integer(), nullable=False),
        sa.Column("total_points", sa.Integer(), nullable=False),
        sa.Column("correct_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["snapshot_id"], ["rank_snapshots.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("snapshot_id", "public_label", name="uq_rank_rows_public_label"),
        sa.CheckConstraint("overall_rank > 0", name="ck_rank_rows_rank"),
        sa.CheckConstraint("total_points >= 0", name="ck_rank_rows_points"),
        sa.CheckConstraint("correct_count >= 0", name="ck_rank_rows_correct_count"),
    )


def _create_audit_tables() -> None:
    op.create_table(
        "operation_audits",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("event_id", UUID, nullable=False),
        sa.Column("round_id", UUID, nullable=True),
        sa.Column("account_id", UUID, nullable=True),
        sa.Column("operation", sa.String(length=80), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=80), nullable=False),
        sa.Column("before_state", sa.String(length=80), nullable=True),
        sa.Column("after_state", sa.String(length=80), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", UUID, nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=DB_NOW, nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["round_id"], ["quiz_rounds.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["account_id"], ["admin_accounts.id"], ondelete="RESTRICT"),
    )
    op.create_index(
        "ix_operation_audits_event_occurred",
        "operation_audits",
        ["event_id", "occurred_at"],
    )
    op.create_table(
        "idempotency_records",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("account_id", UUID, nullable=False),
        sa.Column("idempotency_key", UUID, nullable=False),
        sa.Column("operation", sa.String(length=80), nullable=False),
        sa.Column("request_hash", sa.String(length=128), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column("resulting_version", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["admin_accounts.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "account_id",
            "idempotency_key",
            name="uq_idempotency_account_key",
        ),
    )
    op.create_index(
        "ix_idempotency_records_expires_at",
        "idempotency_records",
        ["expires_at"],
    )


def downgrade() -> None:
    for table_name in (
        "idempotency_records",
        "operation_audits",
        "rank_snapshot_rows",
        "rank_snapshots",
        "round_option_stats",
        "score_ledgers",
        "answers",
        "readiness_reports",
        "round_preparation_targets",
        "admin_sessions",
        "admin_accounts",
        "participant_sessions",
        "display_name_consents",
        "participants",
        "quiz_rounds",
        "question_options",
        "questions",
        "events",
        "display_name_policies",
        "scoring_policies",
    ):
        op.drop_table(table_name)
