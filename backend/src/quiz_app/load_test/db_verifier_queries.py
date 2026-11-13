from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Final, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import AwareDatetime, Field, StrictBool, StrictInt, StrictStr, ValidationError
from sqlalchemy import text
from sqlalchemy.sql.elements import TextClause

from quiz_app.load_test.db_verifier_contracts import DbVerifierMode, StrictDbVerifierModel

QueryId = Literal[
    "round_identity",
    "scoring_policy",
    "readiness_summary",
    "answers",
    "score_ledgers",
    "option_stats",
    "ranking_consistency",
    "operation_audits",
    "idempotency_records",
    "final_event",
    "final_scoring_policy",
    "final_rounds",
    "final_readiness_summaries",
    "final_answers",
    "final_score_ledgers",
    "final_option_stats",
    "final_participant_summary",
    "final_integrity_summary",
    "final_ranking_consistency",
    "final_operation_audits",
    "final_idempotency_records",
    "lock_observation",
]
BindName = Literal["event_id", "round_id"]

# DB verifierの成果物へ値を流してはいけない列名。query内部のJOIN/集約での参照は許す。
PROHIBITED_RESULT_COLUMNS: Final = frozenset(
    {
        "password_hash",
        "token_hash",
        "csrf_hash",
        "request_hash",
        "response_json",
        "join_attempt_hash",
        "display_name",
        "display_name_uniqueness_key",
        "public_label",
        "reason",
        "prompt",
        "explanation",
        "content",
    }
)
# SELECT-only roleへ付与するpublic schemaの列権限正本。秘密列を持つtableも列単位に限定する。
APPLICATION_SELECT_COLUMN_ALLOWLIST: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("alembic_version", "version_num"),
        ("events", "id"),
        ("events", "version"),
        ("events", "state"),
        ("events", "ranking_visibility"),
        ("events", "scoring_policy_version"),
        ("events", "display_name_policy_version"),
        ("questions", "id"),
        ("questions", "event_id"),
        ("questions", "version"),
        ("question_options", "id"),
        ("question_options", "question_id"),
        ("question_options", "label"),
        ("question_options", "display_order"),
        ("question_options", "is_correct"),
        ("quiz_rounds", "id"),
        ("quiz_rounds", "event_id"),
        ("quiz_rounds", "question_id"),
        ("quiz_rounds", "version"),
        ("quiz_rounds", "state"),
        ("quiz_rounds", "result_publication"),
        ("quiz_rounds", "open_at"),
        ("quiz_rounds", "standard_close_at"),
        ("quiz_rounds", "revealed_at"),
        ("scoring_policies", "version"),
        ("scoring_policies", "base_points"),
        ("scoring_policies", "initial_speed_bonus"),
        ("scoring_policies", "speed_bucket_ms"),
        ("scoring_policies", "speed_decrement_per_bucket"),
        ("scoring_policies", "standard_duration_ms"),
        ("scoring_policies", "is_approved"),
        ("scoring_policies", "timing_basis"),
        ("scoring_policies", "arrival_grace_ms"),
        ("scoring_policies", "clock_tolerance_ms"),
        ("scoring_policies", "client_ahead_tolerance_ms"),
        ("participants", "id"),
        ("participants", "event_id"),
        ("participants", "public_label"),
        ("participants", "is_ranking_eligible"),
        ("participant_sessions", "participant_id"),
        ("participant_sessions", "expires_at"),
        ("participant_sessions", "revoked_at"),
        ("round_preparation_targets", "round_id"),
        ("round_preparation_targets", "participant_id"),
        ("readiness_reports", "participant_id"),
        ("readiness_reports", "round_id"),
        ("readiness_reports", "round_version"),
        ("readiness_reports", "is_ready"),
        ("readiness_reports", "has_asset_failure"),
        ("answers", "id"),
        ("answers", "participant_id"),
        ("answers", "round_id"),
        ("answers", "option_id"),
        ("answers", "client_answer_id"),
        ("answers", "received_at"),
        ("answers", "client_elapsed_ms"),
        ("score_ledgers", "id"),
        ("score_ledgers", "round_id"),
        ("score_ledgers", "participant_id"),
        ("score_ledgers", "scoring_policy_version"),
        ("score_ledgers", "is_correct"),
        ("score_ledgers", "base_points"),
        ("score_ledgers", "speed_points"),
        ("score_ledgers", "total_points"),
        ("score_ledgers", "elapsed_ms"),
        ("score_ledgers", "speed_bucket"),
        ("score_ledgers", "speed_rank"),
        ("score_ledgers", "speed_cohort_size"),
        ("score_ledgers", "speed_cohort"),
        ("score_ledgers", "exclusion_reason"),
        ("round_option_stats", "round_id"),
        ("round_option_stats", "option_id"),
        ("round_option_stats", "valid_response_count"),
        ("round_option_stats", "response_percentage"),
        ("rank_snapshots", "id"),
        ("rank_snapshots", "event_id"),
        ("rank_snapshots", "round_id"),
        ("rank_snapshots", "version"),
        ("rank_snapshot_rows", "id"),
        ("rank_snapshot_rows", "snapshot_id"),
        ("rank_snapshot_rows", "public_label"),
        ("rank_snapshot_rows", "overall_rank"),
        ("rank_snapshot_rows", "total_points"),
        ("rank_snapshot_rows", "correct_count"),
        ("operation_audits", "id"),
        ("operation_audits", "event_id"),
        ("operation_audits", "round_id"),
        ("operation_audits", "account_id"),
        ("operation_audits", "operation"),
        ("operation_audits", "before_state"),
        ("operation_audits", "after_state"),
        ("operation_audits", "outcome"),
        ("operation_audits", "idempotency_key"),
        ("operation_audits", "occurred_at"),
        ("operation_audits", "reason"),
        ("operation_audits", "target_type"),
        ("operation_audits", "target_id"),
        ("idempotency_records", "id"),
        ("idempotency_records", "account_id"),
        ("idempotency_records", "idempotency_key"),
        ("idempotency_records", "operation"),
        ("idempotency_records", "response_status"),
        ("idempotency_records", "resulting_version"),
        ("idempotency_records", "created_at"),
        ("idempotency_records", "expires_at"),
    }
)
_QUERY_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MUTATING_SQL_PATTERN = re.compile(
    r"\b(?:alter|call|comment|copy|create|delete|do|drop|execute|grant|insert|merge|"
    r"refresh|reindex|revoke|security|truncate|update|vacuum)\b",
    flags=re.IGNORECASE,
)
_ALLOWED_MODES: Final = frozenset({"per-round", "final", "observe-locks"})


class DbQueryDefinitionError(ValueError):
    """固定query定義の不整合を、SQL本文を含めずに表す。"""


class DbQueryValidationError(ValueError):
    """queryのbind値または結果行を、値を含めずに拒否する。"""


class RoundIdentityRow(StrictDbVerifierModel):
    event_id: UUID
    event_version: StrictInt = Field(ge=1)
    event_state: Literal["InProgress"]
    ranking_visibility: Literal["HiddenAll"]
    round_id: UUID
    round_version: StrictInt = Field(ge=1)
    round_state: Literal["Reveal"]
    result_publication: Literal["Published"]
    question_id: UUID
    question_version: StrictInt = Field(ge=1, le=10)
    open_at: AwareDatetime
    standard_close_at: AwareDatetime
    revealed_at: AwareDatetime


class ReadinessSummaryRow(StrictDbVerifierModel):
    round_id: UUID
    round_version: StrictInt = Field(ge=1)
    target_count: StrictInt = Field(ge=0)
    report_count: StrictInt = Field(ge=0)
    ready_count: StrictInt = Field(ge=0)
    asset_failure_count: StrictInt = Field(ge=0)
    stale_report_count: StrictInt = Field(ge=0)
    non_target_report_count: StrictInt = Field(ge=0)


class ScoringPolicyRow(StrictDbVerifierModel):
    scoring_policy_version: StrictStr = Field(min_length=1, max_length=64)
    base_points: StrictInt = Field(ge=0)
    initial_speed_bonus: StrictInt = Field(ge=0)
    speed_bucket_ms: StrictInt = Field(gt=0)
    speed_decrement_per_bucket: StrictInt = Field(ge=0)
    standard_duration_ms: StrictInt = Field(gt=0)
    is_approved: Literal[True]
    timing_basis: Literal["server_received_v1", "client_monotonic_v1"] = "server_received_v1"
    arrival_grace_ms: StrictInt = Field(default=0, ge=0)
    clock_tolerance_ms: StrictInt = Field(default=0, ge=0)
    client_ahead_tolerance_ms: StrictInt = Field(default=0, ge=0)


class AnswerRow(StrictDbVerifierModel):
    answer_id: UUID
    participant_id: UUID
    participant_event_id: UUID
    round_id: UUID
    option_id: UUID
    option_question_id: UUID
    option_label: Literal["A", "B", "C", "D"]
    option_is_correct: StrictBool
    client_answer_id: UUID
    received_at: AwareDatetime
    is_ranking_eligible: StrictBool
    client_elapsed_ms: StrictInt | None = Field(default=None, ge=0)


class ScoreLedgerRow(StrictDbVerifierModel):
    score_ledger_id: UUID
    round_id: UUID
    participant_id: UUID
    scoring_policy_version: StrictStr = Field(min_length=1, max_length=64)
    is_correct: StrictBool
    base_points: StrictInt = Field(ge=0)
    speed_points: StrictInt = Field(ge=0)
    total_points: StrictInt = Field(ge=0)
    elapsed_ms: StrictInt = Field(ge=0)
    speed_bucket: StrictInt = Field(ge=0)
    speed_rank: StrictInt | None = Field(default=None, ge=1)
    speed_cohort_size: StrictInt | None = Field(default=None, ge=1)
    speed_cohort: Literal["Correct", "AllValid", "Excluded"]
    exclusion_reason: Literal["None", "Wrong", "Void", "Ineligible"]


class OptionStatRow(StrictDbVerifierModel):
    round_id: UUID
    option_id: UUID
    option_question_id: UUID
    option_label: Literal["A", "B", "C", "D"]
    display_order: StrictInt = Field(ge=0, le=3)
    option_is_correct: StrictBool
    valid_response_count: StrictInt = Field(ge=0)
    response_percentage: Decimal = Field(ge=Decimal("0"), le=Decimal("100"))


class RankingConsistencyRow(StrictDbVerifierModel):
    participant_id: UUID | None
    snapshot_id: UUID | None
    snapshot_version: StrictInt | None = Field(default=None, ge=1)
    snapshot_round_id: UUID | None
    match_state: Literal["expected-and-stored", "expected-only", "stored-only"]
    stored_overall_rank: StrictInt | None = Field(default=None, ge=1)
    expected_overall_rank: StrictInt | None = Field(default=None, ge=1)
    stored_total_points: StrictInt | None = Field(default=None, ge=0)
    expected_total_points: StrictInt | None = Field(default=None, ge=0)
    stored_correct_count: StrictInt | None = Field(default=None, ge=0)
    expected_correct_count: StrictInt | None = Field(default=None, ge=0)


class OperationAuditRow(StrictDbVerifierModel):
    audit_id: UUID
    event_id: UUID
    round_id: UUID
    account_id: UUID | None
    operation: StrictStr = Field(min_length=1, max_length=80)
    is_automatic_aggregation: bool
    before_state: StrictStr | None = Field(default=None, max_length=80)
    after_state: StrictStr | None = Field(default=None, max_length=80)
    outcome: StrictStr = Field(min_length=1, max_length=32)
    idempotency_key: UUID | None
    occurred_at: AwareDatetime


class IdempotencyRecordRow(StrictDbVerifierModel):
    record_id: UUID
    account_id: UUID
    idempotency_key: UUID
    operation: StrictStr = Field(min_length=1, max_length=80)
    response_status: StrictInt = Field(ge=100, le=599)
    resulting_version: StrictInt = Field(ge=1)
    created_at: AwareDatetime
    expires_at: AwareDatetime


class FinalEventRow(StrictDbVerifierModel):
    event_id: UUID
    event_version: StrictInt = Field(ge=1)
    event_state: Literal["InProgress", "Completed"]
    ranking_visibility: Literal["HiddenAll"]
    scoring_policy_version: StrictStr = Field(min_length=1, max_length=64)
    display_name_policy_version: StrictStr = Field(min_length=1, max_length=64)


class FinalRoundRow(StrictDbVerifierModel):
    round_id: UUID
    round_version: StrictInt = Field(ge=1)
    round_state: Literal["Reveal", "RoundBackupValidated"]
    result_publication: Literal["Published"]
    question_id: UUID
    question_version: StrictInt = Field(ge=1, le=10)
    open_at: AwareDatetime
    standard_close_at: AwareDatetime
    revealed_at: AwareDatetime


class FinalParticipantSummaryRow(StrictDbVerifierModel):
    participant_count: StrictInt = Field(ge=0)
    distinct_participant_id_count: StrictInt = Field(ge=0)
    ranking_eligible_count: StrictInt = Field(ge=0)
    session_count: StrictInt = Field(ge=0)
    distinct_session_participant_count: StrictInt = Field(ge=0)
    active_session_count: StrictInt = Field(ge=0)
    revoked_session_count: StrictInt = Field(ge=0)


class FinalIntegritySummaryRow(StrictDbVerifierModel):
    round_count: StrictInt = Field(ge=0)
    distinct_round_id_count: StrictInt = Field(ge=0)
    distinct_question_id_count: StrictInt = Field(ge=0)
    preparation_target_count: StrictInt = Field(ge=0)
    distinct_preparation_target_count: StrictInt = Field(ge=0)
    readiness_report_count: StrictInt = Field(ge=0)
    distinct_readiness_report_count: StrictInt = Field(ge=0)
    answer_count: StrictInt = Field(ge=0)
    distinct_answer_id_count: StrictInt = Field(ge=0)
    distinct_answer_participant_round_count: StrictInt = Field(ge=0)
    distinct_client_answer_id_count: StrictInt = Field(ge=0)
    score_ledger_count: StrictInt = Field(ge=0)
    distinct_score_participant_round_count: StrictInt = Field(ge=0)
    option_stat_count: StrictInt = Field(ge=0)
    distinct_option_stat_count: StrictInt = Field(ge=0)
    rank_snapshot_count: StrictInt = Field(ge=0)
    distinct_snapshot_round_count: StrictInt = Field(ge=0)
    rank_snapshot_row_count: StrictInt = Field(ge=0)


class LockObservationRow(StrictDbVerifierModel):
    observed_at: AwareDatetime
    total_lock_count: StrictInt = Field(ge=0)
    waiting_lock_count: StrictInt = Field(ge=0)
    waiting_session_count: StrictInt = Field(ge=0)
    active_transaction_count: StrictInt = Field(ge=0)
    idle_in_transaction_count: StrictInt = Field(ge=0)
    oldest_transaction_age_ms: Decimal = Field(ge=Decimal("0"))


DbQueryRow = (
    RoundIdentityRow
    | ReadinessSummaryRow
    | ScoringPolicyRow
    | AnswerRow
    | ScoreLedgerRow
    | OptionStatRow
    | RankingConsistencyRow
    | OperationAuditRow
    | IdempotencyRecordRow
    | FinalEventRow
    | FinalRoundRow
    | FinalParticipantSummaryRow
    | FinalIntegritySummaryRow
    | LockObservationRow
)
RowT = TypeVar("RowT", bound=StrictDbVerifierModel, covariant=True)


@dataclass(frozen=True, slots=True)
class DbQuerySpec(Generic[RowT]):
    """実行層へ渡す、固定SELECTと結果型の不可変な組。"""

    query_id: QueryId
    mode: DbVerifierMode
    statement: TextClause
    bind_names: tuple[BindName, ...]
    selected_columns: tuple[str, ...]
    row_model: type[RowT]
    max_rows: int
    exact_rows: int | None = None

    def __post_init__(self) -> None:
        _validate_query_definition(self)


def _validate_query_definition(spec: DbQuerySpec[StrictDbVerifierModel]) -> None:
    if _QUERY_ID_PATTERN.fullmatch(spec.query_id) is None or spec.mode not in _ALLOWED_MODES:
        raise DbQueryDefinitionError("db_verifier_query_identity_invalid")
    if not isinstance(spec.statement, TextClause):
        raise DbQueryDefinitionError("db_verifier_query_statement_type_invalid")
    sql = str(spec.statement).strip()
    first_word = sql.split(maxsplit=1)[0].upper() if sql else ""
    if (
        first_word not in {"SELECT", "WITH"}
        or ";" in sql
        or "--" in sql
        or "/*" in sql
        or _MUTATING_SQL_PATTERN.search(sql) is not None
    ):
        raise DbQueryDefinitionError("db_verifier_query_not_select_only")
    compiled_bind_names = frozenset(spec.statement.compile().params)
    if len(spec.bind_names) != len(set(spec.bind_names)) or compiled_bind_names != frozenset(
        spec.bind_names
    ):
        raise DbQueryDefinitionError("db_verifier_query_bind_names_invalid")
    model_columns = tuple(spec.row_model.model_fields)
    if (
        spec.selected_columns != model_columns
        or len(spec.selected_columns) != len(set(spec.selected_columns))
        or not set(spec.selected_columns).isdisjoint(PROHIBITED_RESULT_COLUMNS)
    ):
        raise DbQueryDefinitionError("db_verifier_query_columns_invalid")
    if spec.max_rows < 1 or (
        spec.exact_rows is not None and (spec.exact_rows < 0 or spec.exact_rows > spec.max_rows)
    ):
        raise DbQueryDefinitionError("db_verifier_query_row_limit_invalid")


def validate_query_parameters(
    spec: DbQuerySpec[RowT],
    parameters: Mapping[str, object],
) -> dict[str, UUID]:
    """bind名とUUID型を完全一致で検証し、余分な値をSQLへ渡さない。"""

    if frozenset(parameters) != frozenset(spec.bind_names):
        raise DbQueryValidationError(f"db_verifier_query_parameters_invalid:{spec.query_id}")
    validated: dict[str, UUID] = {}
    for name in spec.bind_names:
        value = parameters[name]
        if not isinstance(value, UUID):
            raise DbQueryValidationError(f"db_verifier_query_parameters_invalid:{spec.query_id}")
        validated[name] = value
    return validated


def validate_query_rows(
    spec: DbQuerySpec[RowT],
    rows: Iterable[Mapping[str, object]],
) -> tuple[RowT, ...]:
    """driver結果をexact columnsかつstrict Pydantic rowへ変換する。"""

    validated: list[RowT] = []
    expected_columns = frozenset(spec.selected_columns)
    for row in rows:
        if len(validated) >= spec.max_rows:
            raise DbQueryValidationError(f"db_verifier_query_row_limit_exceeded:{spec.query_id}")
        if frozenset(row) != expected_columns:
            raise DbQueryValidationError(f"db_verifier_query_column_mismatch:{spec.query_id}")
        try:
            validated.append(spec.row_model.model_validate(row, strict=True))
        except ValidationError:
            raise DbQueryValidationError(f"db_verifier_query_row_invalid:{spec.query_id}") from None
    if spec.exact_rows is not None and len(validated) != spec.exact_rows:
        raise DbQueryValidationError(f"db_verifier_query_row_count_invalid:{spec.query_id}")
    return tuple(validated)


ROUND_IDENTITY_SQL: Final = text(
    """
    SELECT
        e.id AS event_id,
        e.version AS event_version,
        e.state AS event_state,
        e.ranking_visibility AS ranking_visibility,
        qr.id AS round_id,
        qr.version AS round_version,
        qr.state AS round_state,
        qr.result_publication AS result_publication,
        q.id AS question_id,
        q.version AS question_version,
        qr.open_at AS open_at,
        qr.standard_close_at AS standard_close_at,
        qr.revealed_at AS revealed_at
    FROM public.events AS e
    JOIN public.quiz_rounds AS qr ON qr.event_id = e.id
    JOIN public.questions AS q ON q.id = qr.question_id AND q.event_id = e.id
    WHERE e.id = :event_id AND qr.id = :round_id
    """
)

SCORING_POLICY_SQL: Final = text(
    """
    SELECT
        sp.version AS scoring_policy_version,
        sp.base_points AS base_points,
        sp.initial_speed_bonus AS initial_speed_bonus,
        sp.speed_bucket_ms AS speed_bucket_ms,
        sp.speed_decrement_per_bucket AS speed_decrement_per_bucket,
        sp.standard_duration_ms AS standard_duration_ms,
        sp.is_approved AS is_approved,
        sp.timing_basis AS timing_basis,
        sp.arrival_grace_ms AS arrival_grace_ms,
        sp.clock_tolerance_ms AS clock_tolerance_ms,
        sp.client_ahead_tolerance_ms AS client_ahead_tolerance_ms
    FROM public.events AS e
    JOIN public.scoring_policies AS sp ON sp.version = e.scoring_policy_version
    JOIN public.quiz_rounds AS qr ON qr.event_id = e.id
    WHERE e.id = :event_id AND qr.id = :round_id
    """
)

READINESS_SUMMARY_SQL: Final = text(
    """
    SELECT
        qr.id AS round_id,
        qr.version AS round_version,
        (SELECT COUNT(*) FROM public.round_preparation_targets AS rpt
         WHERE rpt.round_id = qr.id) AS target_count,
        (SELECT COUNT(*) FROM public.readiness_reports AS rr
         WHERE rr.round_id = qr.id) AS report_count,
        (SELECT COUNT(*) FROM public.readiness_reports AS rr
         WHERE rr.round_id = qr.id
           AND rr.round_version = qr.version
           AND rr.is_ready IS TRUE
           AND rr.has_asset_failure IS FALSE) AS ready_count,
        (SELECT COUNT(*) FROM public.readiness_reports AS rr
         WHERE rr.round_id = qr.id
           AND rr.round_version = qr.version
           AND rr.has_asset_failure IS TRUE) AS asset_failure_count,
        (SELECT COUNT(*) FROM public.readiness_reports AS rr
         WHERE rr.round_id = qr.id
           AND rr.round_version <> qr.version) AS stale_report_count,
        (SELECT COUNT(*) FROM public.readiness_reports AS rr
         WHERE rr.round_id = qr.id
           AND NOT EXISTS (
               SELECT 1 FROM public.round_preparation_targets AS rpt
               WHERE rpt.round_id = rr.round_id
                 AND rpt.participant_id = rr.participant_id
           )) AS non_target_report_count
    FROM public.quiz_rounds AS qr
    WHERE qr.event_id = :event_id AND qr.id = :round_id
    """
)

ANSWERS_SQL: Final = text(
    """
    SELECT
        a.id AS answer_id,
        a.participant_id AS participant_id,
        p.event_id AS participant_event_id,
        a.round_id AS round_id,
        a.option_id AS option_id,
        qo.question_id AS option_question_id,
        qo.label AS option_label,
        qo.is_correct AS option_is_correct,
        a.client_answer_id AS client_answer_id,
        a.received_at AS received_at,
        p.is_ranking_eligible AS is_ranking_eligible,
        a.client_elapsed_ms AS client_elapsed_ms
    FROM public.quiz_rounds AS qr
    JOIN public.answers AS a ON a.round_id = qr.id
    JOIN public.participants AS p ON p.id = a.participant_id
    JOIN public.question_options AS qo ON qo.id = a.option_id
    WHERE qr.event_id = :event_id AND qr.id = :round_id
    ORDER BY a.participant_id, a.id
    """
)

SCORE_LEDGERS_SQL: Final = text(
    """
    SELECT
        sl.id AS score_ledger_id,
        sl.round_id AS round_id,
        sl.participant_id AS participant_id,
        sl.scoring_policy_version AS scoring_policy_version,
        sl.is_correct AS is_correct,
        sl.base_points AS base_points,
        sl.speed_points AS speed_points,
        sl.total_points AS total_points,
        sl.elapsed_ms AS elapsed_ms,
        sl.speed_bucket AS speed_bucket,
        sl.speed_rank AS speed_rank,
        sl.speed_cohort_size AS speed_cohort_size,
        sl.speed_cohort AS speed_cohort,
        sl.exclusion_reason AS exclusion_reason
    FROM public.quiz_rounds AS qr
    JOIN public.score_ledgers AS sl ON sl.round_id = qr.id
    WHERE qr.event_id = :event_id AND qr.id = :round_id
    ORDER BY sl.participant_id, sl.id
    """
)

OPTION_STATS_SQL: Final = text(
    """
    SELECT
        qr.id AS round_id,
        qo.id AS option_id,
        qo.question_id AS option_question_id,
        qo.label AS option_label,
        qo.display_order AS display_order,
        qo.is_correct AS option_is_correct,
        ros.valid_response_count AS valid_response_count,
        ros.response_percentage AS response_percentage
    FROM public.quiz_rounds AS qr
    JOIN public.question_options AS qo ON qo.question_id = qr.question_id
    LEFT JOIN public.round_option_stats AS ros
      ON ros.round_id = qr.id AND ros.option_id = qo.id
    WHERE qr.event_id = :event_id AND qr.id = :round_id
    ORDER BY qo.display_order, qo.id
    """
)

_RANKING_EXPECTED_CTE: Final = """
    score_totals AS (
        SELECT
            p.id AS source_participant_id,
            p.public_label AS source_public_label,
            CAST(SUM(sl.total_points) AS bigint) AS computed_total_points,
            CAST(COUNT(*) FILTER (WHERE sl.is_correct IS TRUE) AS bigint)
                AS computed_correct_count,
            CAST(COALESCE(SUM(sl.elapsed_ms) FILTER (WHERE sl.is_correct IS TRUE), 0) AS bigint)
                AS computed_correct_elapsed_ms
        FROM public.participants AS p
        JOIN public.score_ledgers AS sl ON sl.participant_id = p.id
        JOIN public.quiz_rounds AS scored_round ON scored_round.id = sl.round_id
        WHERE p.event_id = :event_id
          AND scored_round.event_id = :event_id
          AND p.is_ranking_eligible IS TRUE
        GROUP BY p.id, p.public_label
    ),
    expected AS (
        SELECT
            source_participant_id AS expected_participant_id,
            source_public_label AS expected_public_label,
            computed_total_points,
            computed_correct_count,
            CAST(RANK() OVER (
                ORDER BY computed_total_points DESC,
                         computed_correct_count DESC,
                         computed_correct_elapsed_ms ASC
            ) AS bigint) AS computed_overall_rank
        FROM score_totals
    )
"""


def _ranking_sql(*, final: bool) -> TextClause:
    # SQLはこのmodule内の固定literalだけから組み立て、外部入力を連結しない。
    snapshot_filter = (
        "rs.event_id = :event_id AND rs.version = "
        "(SELECT MAX(latest.version) FROM public.rank_snapshots AS latest "
        "WHERE latest.event_id = :event_id)"
        if final
        else "rs.event_id = :event_id AND rs.round_id = :round_id"
    )
    return text(
        "WITH "
        + _RANKING_EXPECTED_CTE
        + """,
        stored AS (
            SELECT
                rs.id AS stored_snapshot_id_value,
                rs.version AS stored_snapshot_version_value,
                rs.round_id AS stored_snapshot_round_id_value,
                rsr.id AS stored_rank_row_id,
                rsr.public_label AS stored_public_label,
                rsr.overall_rank AS stored_rank_value,
                rsr.total_points AS stored_points_value,
                rsr.correct_count AS stored_correct_value
            FROM public.rank_snapshots AS rs
            JOIN public.rank_snapshot_rows AS rsr ON rsr.snapshot_id = rs.id
            WHERE """
        + snapshot_filter
        + """
        )
        SELECT
            expected.expected_participant_id AS participant_id,
            stored.stored_snapshot_id_value AS snapshot_id,
            stored.stored_snapshot_version_value AS snapshot_version,
            stored.stored_snapshot_round_id_value AS snapshot_round_id,
            CASE
                WHEN expected.expected_participant_id IS NOT NULL
                     AND stored.stored_rank_row_id IS NOT NULL
                    THEN 'expected-and-stored'
                WHEN expected.expected_participant_id IS NOT NULL THEN 'expected-only'
                ELSE 'stored-only'
            END AS match_state,
            stored.stored_rank_value AS stored_overall_rank,
            expected.computed_overall_rank AS expected_overall_rank,
            stored.stored_points_value AS stored_total_points,
            expected.computed_total_points AS expected_total_points,
            stored.stored_correct_value AS stored_correct_count,
            expected.computed_correct_count AS expected_correct_count
        FROM expected
        FULL OUTER JOIN stored
          ON stored.stored_public_label = expected.expected_public_label
        ORDER BY
            COALESCE(expected.computed_overall_rank, stored.stored_rank_value),
            COALESCE(
                CAST(expected.expected_participant_id AS text),
                CAST(stored.stored_rank_row_id AS text)
            )
        """
    )


RANKING_CONSISTENCY_SQL: Final = _ranking_sql(final=False)
FINAL_RANKING_CONSISTENCY_SQL: Final = _ranking_sql(final=True)

OPERATION_AUDITS_SQL: Final = text(
    """
    SELECT
        oa.id AS audit_id,
        oa.event_id AS event_id,
        oa.round_id AS round_id,
        oa.account_id AS account_id,
        oa.operation AS operation,
        COALESCE(oa.operation = 'aggregate_round_results'
            AND oa.account_id IS NULL AND oa.idempotency_key IS NULL
            AND oa.reason = 'automatic_after_answer_deadline'
            AND oa.target_type = 'quiz_round'
            AND oa.target_id = CAST(oa.round_id AS text), FALSE) AS is_automatic_aggregation,
        oa.before_state AS before_state,
        oa.after_state AS after_state,
        oa.outcome AS outcome,
        oa.idempotency_key AS idempotency_key,
        oa.occurred_at AS occurred_at
    FROM public.operation_audits AS oa
    WHERE oa.event_id = :event_id AND oa.round_id = :round_id
    ORDER BY oa.occurred_at, oa.id
    """
)

IDEMPOTENCY_RECORDS_SQL: Final = text(
    """
    SELECT DISTINCT
        ir.id AS record_id,
        ir.account_id AS account_id,
        ir.idempotency_key AS idempotency_key,
        ir.operation AS operation,
        ir.response_status AS response_status,
        ir.resulting_version AS resulting_version,
        ir.created_at AS created_at,
        ir.expires_at AS expires_at
    FROM public.idempotency_records AS ir
    JOIN public.operation_audits AS oa
      ON oa.account_id = ir.account_id AND oa.idempotency_key = ir.idempotency_key
    WHERE oa.event_id = :event_id AND oa.round_id = :round_id
    ORDER BY ir.created_at, ir.id
    """
)

FINAL_EVENT_SQL: Final = text(
    """
    SELECT
        e.id AS event_id,
        e.version AS event_version,
        e.state AS event_state,
        e.ranking_visibility AS ranking_visibility,
        e.scoring_policy_version AS scoring_policy_version,
        e.display_name_policy_version AS display_name_policy_version
    FROM public.events AS e
    WHERE e.id = :event_id
    """
)

FINAL_SCORING_POLICY_SQL: Final = text(
    """
    SELECT
        sp.version AS scoring_policy_version,
        sp.base_points AS base_points,
        sp.initial_speed_bonus AS initial_speed_bonus,
        sp.speed_bucket_ms AS speed_bucket_ms,
        sp.speed_decrement_per_bucket AS speed_decrement_per_bucket,
        sp.standard_duration_ms AS standard_duration_ms,
        sp.is_approved AS is_approved,
        sp.timing_basis AS timing_basis,
        sp.arrival_grace_ms AS arrival_grace_ms,
        sp.clock_tolerance_ms AS clock_tolerance_ms,
        sp.client_ahead_tolerance_ms AS client_ahead_tolerance_ms
    FROM public.events AS e
    JOIN public.scoring_policies AS sp ON sp.version = e.scoring_policy_version
    WHERE e.id = :event_id
    """
)

FINAL_ROUNDS_SQL: Final = text(
    """
    SELECT
        qr.id AS round_id,
        qr.version AS round_version,
        qr.state AS round_state,
        qr.result_publication AS result_publication,
        q.id AS question_id,
        q.version AS question_version,
        qr.open_at AS open_at,
        qr.standard_close_at AS standard_close_at,
        qr.revealed_at AS revealed_at
    FROM public.quiz_rounds AS qr
    JOIN public.questions AS q ON q.id = qr.question_id AND q.event_id = qr.event_id
    WHERE qr.event_id = :event_id
    ORDER BY q.version, qr.id
    """
)

FINAL_READINESS_SUMMARIES_SQL: Final = text(
    """
    SELECT
        qr.id AS round_id,
        qr.version AS round_version,
        (SELECT COUNT(*) FROM public.round_preparation_targets AS rpt
         WHERE rpt.round_id = qr.id) AS target_count,
        (SELECT COUNT(*) FROM public.readiness_reports AS rr
         WHERE rr.round_id = qr.id) AS report_count,
        (SELECT COUNT(*) FROM public.readiness_reports AS rr
         WHERE rr.round_id = qr.id
           AND rr.round_version = qr.version
           AND rr.is_ready IS TRUE
           AND rr.has_asset_failure IS FALSE) AS ready_count,
        (SELECT COUNT(*) FROM public.readiness_reports AS rr
         WHERE rr.round_id = qr.id
           AND rr.round_version = qr.version
           AND rr.has_asset_failure IS TRUE) AS asset_failure_count,
        (SELECT COUNT(*) FROM public.readiness_reports AS rr
         WHERE rr.round_id = qr.id
           AND rr.round_version <> qr.version) AS stale_report_count,
        (SELECT COUNT(*) FROM public.readiness_reports AS rr
         WHERE rr.round_id = qr.id
           AND NOT EXISTS (
               SELECT 1 FROM public.round_preparation_targets AS rpt
               WHERE rpt.round_id = rr.round_id
                 AND rpt.participant_id = rr.participant_id
           )) AS non_target_report_count
    FROM public.quiz_rounds AS qr
    JOIN public.questions AS q ON q.id = qr.question_id AND q.event_id = qr.event_id
    WHERE qr.event_id = :event_id
    ORDER BY q.version, qr.id
    """
)

FINAL_ANSWERS_SQL: Final = text(
    """
    SELECT
        a.id AS answer_id,
        a.participant_id AS participant_id,
        p.event_id AS participant_event_id,
        a.round_id AS round_id,
        a.option_id AS option_id,
        qo.question_id AS option_question_id,
        qo.label AS option_label,
        qo.is_correct AS option_is_correct,
        a.client_answer_id AS client_answer_id,
        a.received_at AS received_at,
        p.is_ranking_eligible AS is_ranking_eligible,
        a.client_elapsed_ms AS client_elapsed_ms
    FROM public.quiz_rounds AS qr
    JOIN public.questions AS q ON q.id = qr.question_id AND q.event_id = qr.event_id
    JOIN public.answers AS a ON a.round_id = qr.id
    JOIN public.participants AS p ON p.id = a.participant_id
    JOIN public.question_options AS qo ON qo.id = a.option_id
    WHERE qr.event_id = :event_id
    ORDER BY q.version, a.participant_id, a.id
    """
)

FINAL_SCORE_LEDGERS_SQL: Final = text(
    """
    SELECT
        sl.id AS score_ledger_id,
        sl.round_id AS round_id,
        sl.participant_id AS participant_id,
        sl.scoring_policy_version AS scoring_policy_version,
        sl.is_correct AS is_correct,
        sl.base_points AS base_points,
        sl.speed_points AS speed_points,
        sl.total_points AS total_points,
        sl.elapsed_ms AS elapsed_ms,
        sl.speed_bucket AS speed_bucket,
        sl.speed_rank AS speed_rank,
        sl.speed_cohort_size AS speed_cohort_size,
        sl.speed_cohort AS speed_cohort,
        sl.exclusion_reason AS exclusion_reason
    FROM public.quiz_rounds AS qr
    JOIN public.questions AS q ON q.id = qr.question_id AND q.event_id = qr.event_id
    JOIN public.score_ledgers AS sl ON sl.round_id = qr.id
    WHERE qr.event_id = :event_id
    ORDER BY q.version, sl.participant_id, sl.id
    """
)

FINAL_OPTION_STATS_SQL: Final = text(
    """
    SELECT
        qr.id AS round_id,
        qo.id AS option_id,
        qo.question_id AS option_question_id,
        qo.label AS option_label,
        qo.display_order AS display_order,
        qo.is_correct AS option_is_correct,
        ros.valid_response_count AS valid_response_count,
        ros.response_percentage AS response_percentage
    FROM public.quiz_rounds AS qr
    JOIN public.questions AS q ON q.id = qr.question_id AND q.event_id = qr.event_id
    JOIN public.question_options AS qo ON qo.question_id = qr.question_id
    LEFT JOIN public.round_option_stats AS ros
      ON ros.round_id = qr.id AND ros.option_id = qo.id
    WHERE qr.event_id = :event_id
    ORDER BY q.version, qo.display_order, qo.id
    """
)

FINAL_PARTICIPANT_SUMMARY_SQL: Final = text(
    """
    SELECT
        (SELECT COUNT(*) FROM public.participants AS p
         WHERE p.event_id = e.id) AS participant_count,
        (SELECT COUNT(DISTINCT p.id) FROM public.participants AS p
         WHERE p.event_id = e.id) AS distinct_participant_id_count,
        (SELECT COUNT(*) FROM public.participants AS p
         WHERE p.event_id = e.id AND p.is_ranking_eligible IS TRUE)
            AS ranking_eligible_count,
        (SELECT COUNT(*) FROM public.participant_sessions AS ps
         JOIN public.participants AS p ON p.id = ps.participant_id
         WHERE p.event_id = e.id) AS session_count,
        (SELECT COUNT(DISTINCT ps.participant_id) FROM public.participant_sessions AS ps
         JOIN public.participants AS p ON p.id = ps.participant_id
         WHERE p.event_id = e.id) AS distinct_session_participant_count,
        (SELECT COUNT(*) FROM public.participant_sessions AS ps
         JOIN public.participants AS p ON p.id = ps.participant_id
         WHERE p.event_id = e.id
           AND ps.revoked_at IS NULL
           AND ps.expires_at > transaction_timestamp()) AS active_session_count,
        (SELECT COUNT(*) FROM public.participant_sessions AS ps
         JOIN public.participants AS p ON p.id = ps.participant_id
         WHERE p.event_id = e.id AND ps.revoked_at IS NOT NULL) AS revoked_session_count
    FROM public.events AS e
    WHERE e.id = :event_id
    """
)

FINAL_INTEGRITY_SUMMARY_SQL: Final = text(
    """
    SELECT
        (SELECT COUNT(*) FROM public.quiz_rounds AS qr
         WHERE qr.event_id = e.id) AS round_count,
        (SELECT COUNT(DISTINCT qr.id) FROM public.quiz_rounds AS qr
         WHERE qr.event_id = e.id) AS distinct_round_id_count,
        (SELECT COUNT(DISTINCT qr.question_id) FROM public.quiz_rounds AS qr
         WHERE qr.event_id = e.id) AS distinct_question_id_count,
        (SELECT COUNT(*) FROM public.round_preparation_targets AS rpt
         JOIN public.quiz_rounds AS qr ON qr.id = rpt.round_id
         WHERE qr.event_id = e.id) AS preparation_target_count,
        (SELECT COUNT(DISTINCT (rpt.round_id, rpt.participant_id))
         FROM public.round_preparation_targets AS rpt
         JOIN public.quiz_rounds AS qr ON qr.id = rpt.round_id
         WHERE qr.event_id = e.id) AS distinct_preparation_target_count,
        (SELECT COUNT(*) FROM public.readiness_reports AS rr
         JOIN public.quiz_rounds AS qr ON qr.id = rr.round_id
         WHERE qr.event_id = e.id) AS readiness_report_count,
        (SELECT COUNT(DISTINCT (rr.round_id, rr.participant_id))
         FROM public.readiness_reports AS rr
         JOIN public.quiz_rounds AS qr ON qr.id = rr.round_id
         WHERE qr.event_id = e.id) AS distinct_readiness_report_count,
        (SELECT COUNT(*) FROM public.answers AS a
         JOIN public.quiz_rounds AS qr ON qr.id = a.round_id
         WHERE qr.event_id = e.id) AS answer_count,
        (SELECT COUNT(DISTINCT a.id) FROM public.answers AS a
         JOIN public.quiz_rounds AS qr ON qr.id = a.round_id
         WHERE qr.event_id = e.id) AS distinct_answer_id_count,
        (SELECT COUNT(DISTINCT (a.participant_id, a.round_id)) FROM public.answers AS a
         JOIN public.quiz_rounds AS qr ON qr.id = a.round_id
         WHERE qr.event_id = e.id) AS distinct_answer_participant_round_count,
        (SELECT COUNT(DISTINCT a.client_answer_id) FROM public.answers AS a
         JOIN public.quiz_rounds AS qr ON qr.id = a.round_id
         WHERE qr.event_id = e.id) AS distinct_client_answer_id_count,
        (SELECT COUNT(*) FROM public.score_ledgers AS sl
         JOIN public.quiz_rounds AS qr ON qr.id = sl.round_id
         WHERE qr.event_id = e.id) AS score_ledger_count,
        (SELECT COUNT(DISTINCT (sl.participant_id, sl.round_id)) FROM public.score_ledgers AS sl
         JOIN public.quiz_rounds AS qr ON qr.id = sl.round_id
         WHERE qr.event_id = e.id) AS distinct_score_participant_round_count,
        (SELECT COUNT(*) FROM public.round_option_stats AS ros
         JOIN public.quiz_rounds AS qr ON qr.id = ros.round_id
         WHERE qr.event_id = e.id) AS option_stat_count,
        (SELECT COUNT(DISTINCT (ros.round_id, ros.option_id)) FROM public.round_option_stats AS ros
         JOIN public.quiz_rounds AS qr ON qr.id = ros.round_id
         WHERE qr.event_id = e.id) AS distinct_option_stat_count,
        (SELECT COUNT(*) FROM public.rank_snapshots AS rs
         WHERE rs.event_id = e.id) AS rank_snapshot_count,
        (SELECT COUNT(DISTINCT rs.round_id) FROM public.rank_snapshots AS rs
         WHERE rs.event_id = e.id) AS distinct_snapshot_round_count,
        (SELECT COUNT(*) FROM public.rank_snapshot_rows AS rsr
         JOIN public.rank_snapshots AS rs ON rs.id = rsr.snapshot_id
         WHERE rs.event_id = e.id) AS rank_snapshot_row_count
    FROM public.events AS e
    WHERE e.id = :event_id
    """
)

FINAL_OPERATION_AUDITS_SQL: Final = text(
    """
    SELECT
        oa.id AS audit_id,
        oa.event_id AS event_id,
        oa.round_id AS round_id,
        oa.account_id AS account_id,
        oa.operation AS operation,
        COALESCE(oa.operation = 'aggregate_round_results'
            AND oa.account_id IS NULL AND oa.idempotency_key IS NULL
            AND oa.reason = 'automatic_after_answer_deadline'
            AND oa.target_type = 'quiz_round'
            AND oa.target_id = CAST(oa.round_id AS text), FALSE) AS is_automatic_aggregation,
        oa.before_state AS before_state,
        oa.after_state AS after_state,
        oa.outcome AS outcome,
        oa.idempotency_key AS idempotency_key,
        oa.occurred_at AS occurred_at
    FROM public.operation_audits AS oa
    WHERE oa.event_id = :event_id AND oa.round_id IS NOT NULL
    ORDER BY oa.occurred_at, oa.id
    """
)

FINAL_IDEMPOTENCY_RECORDS_SQL: Final = text(
    """
    SELECT DISTINCT
        ir.id AS record_id,
        ir.account_id AS account_id,
        ir.idempotency_key AS idempotency_key,
        ir.operation AS operation,
        ir.response_status AS response_status,
        ir.resulting_version AS resulting_version,
        ir.created_at AS created_at,
        ir.expires_at AS expires_at
    FROM public.idempotency_records AS ir
    JOIN public.operation_audits AS oa
      ON oa.account_id = ir.account_id AND oa.idempotency_key = ir.idempotency_key
    WHERE oa.event_id = :event_id
    ORDER BY ir.created_at, ir.id
    """
)

LOCK_OBSERVATION_SQL: Final = text(
    """
    SELECT
        clock_timestamp() AS observed_at,
        (SELECT COUNT(*) FROM pg_catalog.pg_locks AS l
         JOIN pg_catalog.pg_stat_activity AS a ON a.pid = l.pid
         WHERE a.datname = current_database()) AS total_lock_count,
        (SELECT COUNT(*) FROM pg_catalog.pg_locks AS l
         JOIN pg_catalog.pg_stat_activity AS a ON a.pid = l.pid
         WHERE a.datname = current_database()
           AND l.granted IS FALSE) AS waiting_lock_count,
        (SELECT COUNT(DISTINCT l.pid) FROM pg_catalog.pg_locks AS l
         JOIN pg_catalog.pg_stat_activity AS a ON a.pid = l.pid
         WHERE a.datname = current_database()
           AND l.granted IS FALSE) AS waiting_session_count,
        (SELECT COUNT(*) FROM pg_catalog.pg_stat_activity AS a
         WHERE a.datname = current_database()
           AND a.xact_start IS NOT NULL
           AND a.state = 'active') AS active_transaction_count,
        (SELECT COUNT(*) FROM pg_catalog.pg_stat_activity AS a
         WHERE a.datname = current_database()
           AND a.state LIKE 'idle in transaction%') AS idle_in_transaction_count,
        CAST(COALESCE((
            SELECT EXTRACT(EPOCH FROM (clock_timestamp() - MIN(a.xact_start))) * 1000
            FROM pg_catalog.pg_stat_activity AS a
            WHERE a.datname = current_database() AND a.xact_start IS NOT NULL
        ), 0) AS numeric) AS oldest_transaction_age_ms
    """
)


def _columns(model: type[StrictDbVerifierModel]) -> tuple[str, ...]:
    return tuple(model.model_fields)


PER_ROUND_QUERY_SPECS: Final[tuple[DbQuerySpec[StrictDbVerifierModel], ...]] = (
    DbQuerySpec(
        "round_identity",
        "per-round",
        ROUND_IDENTITY_SQL,
        ("event_id", "round_id"),
        _columns(RoundIdentityRow),
        RoundIdentityRow,
        1,
        1,
    ),
    DbQuerySpec(
        "scoring_policy",
        "per-round",
        SCORING_POLICY_SQL,
        ("event_id", "round_id"),
        _columns(ScoringPolicyRow),
        ScoringPolicyRow,
        1,
        1,
    ),
    DbQuerySpec(
        "readiness_summary",
        "per-round",
        READINESS_SUMMARY_SQL,
        ("event_id", "round_id"),
        _columns(ReadinessSummaryRow),
        ReadinessSummaryRow,
        1,
        1,
    ),
    DbQuerySpec(
        "answers",
        "per-round",
        ANSWERS_SQL,
        ("event_id", "round_id"),
        _columns(AnswerRow),
        AnswerRow,
        600,
    ),
    DbQuerySpec(
        "score_ledgers",
        "per-round",
        SCORE_LEDGERS_SQL,
        ("event_id", "round_id"),
        _columns(ScoreLedgerRow),
        ScoreLedgerRow,
        600,
    ),
    DbQuerySpec(
        "option_stats",
        "per-round",
        OPTION_STATS_SQL,
        ("event_id", "round_id"),
        _columns(OptionStatRow),
        OptionStatRow,
        4,
        4,
    ),
    DbQuerySpec(
        "ranking_consistency",
        "per-round",
        RANKING_CONSISTENCY_SQL,
        ("event_id", "round_id"),
        _columns(RankingConsistencyRow),
        RankingConsistencyRow,
        600,
    ),
    DbQuerySpec(
        "operation_audits",
        "per-round",
        OPERATION_AUDITS_SQL,
        ("event_id", "round_id"),
        _columns(OperationAuditRow),
        OperationAuditRow,
        100,
    ),
    DbQuerySpec(
        "idempotency_records",
        "per-round",
        IDEMPOTENCY_RECORDS_SQL,
        ("event_id", "round_id"),
        _columns(IdempotencyRecordRow),
        IdempotencyRecordRow,
        100,
    ),
)

FINAL_QUERY_SPECS: Final[tuple[DbQuerySpec[StrictDbVerifierModel], ...]] = (
    DbQuerySpec(
        "final_event",
        "final",
        FINAL_EVENT_SQL,
        ("event_id",),
        _columns(FinalEventRow),
        FinalEventRow,
        1,
        1,
    ),
    DbQuerySpec(
        "final_scoring_policy",
        "final",
        FINAL_SCORING_POLICY_SQL,
        ("event_id",),
        _columns(ScoringPolicyRow),
        ScoringPolicyRow,
        1,
        1,
    ),
    DbQuerySpec(
        "final_rounds",
        "final",
        FINAL_ROUNDS_SQL,
        ("event_id",),
        _columns(FinalRoundRow),
        FinalRoundRow,
        10,
        10,
    ),
    DbQuerySpec(
        "final_readiness_summaries",
        "final",
        FINAL_READINESS_SUMMARIES_SQL,
        ("event_id",),
        _columns(ReadinessSummaryRow),
        ReadinessSummaryRow,
        10,
        10,
    ),
    DbQuerySpec(
        "final_answers",
        "final",
        FINAL_ANSWERS_SQL,
        ("event_id",),
        _columns(AnswerRow),
        AnswerRow,
        6_000,
    ),
    DbQuerySpec(
        "final_score_ledgers",
        "final",
        FINAL_SCORE_LEDGERS_SQL,
        ("event_id",),
        _columns(ScoreLedgerRow),
        ScoreLedgerRow,
        6_000,
    ),
    DbQuerySpec(
        "final_option_stats",
        "final",
        FINAL_OPTION_STATS_SQL,
        ("event_id",),
        _columns(OptionStatRow),
        OptionStatRow,
        40,
        40,
    ),
    DbQuerySpec(
        "final_participant_summary",
        "final",
        FINAL_PARTICIPANT_SUMMARY_SQL,
        ("event_id",),
        _columns(FinalParticipantSummaryRow),
        FinalParticipantSummaryRow,
        1,
        1,
    ),
    DbQuerySpec(
        "final_integrity_summary",
        "final",
        FINAL_INTEGRITY_SUMMARY_SQL,
        ("event_id",),
        _columns(FinalIntegritySummaryRow),
        FinalIntegritySummaryRow,
        1,
        1,
    ),
    DbQuerySpec(
        "final_ranking_consistency",
        "final",
        FINAL_RANKING_CONSISTENCY_SQL,
        ("event_id",),
        _columns(RankingConsistencyRow),
        RankingConsistencyRow,
        600,
    ),
    DbQuerySpec(
        "final_operation_audits",
        "final",
        FINAL_OPERATION_AUDITS_SQL,
        ("event_id",),
        _columns(OperationAuditRow),
        OperationAuditRow,
        500,
    ),
    DbQuerySpec(
        "final_idempotency_records",
        "final",
        FINAL_IDEMPOTENCY_RECORDS_SQL,
        ("event_id",),
        _columns(IdempotencyRecordRow),
        IdempotencyRecordRow,
        500,
    ),
)

OBSERVE_LOCKS_QUERY_SPECS: Final[tuple[DbQuerySpec[StrictDbVerifierModel], ...]] = (
    DbQuerySpec(
        "lock_observation",
        "observe-locks",
        LOCK_OBSERVATION_SQL,
        (),
        _columns(LockObservationRow),
        LockObservationRow,
        1,
        1,
    ),
)

ALL_QUERY_SPECS: Final = (
    *PER_ROUND_QUERY_SPECS,
    *FINAL_QUERY_SPECS,
    *OBSERVE_LOCKS_QUERY_SPECS,
)
_QUERY_SPECS_BY_ID = {spec.query_id: spec for spec in ALL_QUERY_SPECS}
if len(_QUERY_SPECS_BY_ID) != len(ALL_QUERY_SPECS):
    raise DbQueryDefinitionError("db_verifier_query_id_duplicate")
QUERY_SPECS_BY_ID: Final = MappingProxyType(_QUERY_SPECS_BY_ID)


def query_specs_for_mode(mode: DbVerifierMode) -> tuple[DbQuerySpec[StrictDbVerifierModel], ...]:
    """modeごとの固定queryを、検証順のまま返す。"""

    if mode == "per-round":
        return PER_ROUND_QUERY_SPECS
    if mode == "final":
        return FINAL_QUERY_SPECS
    if mode == "observe-locks":
        return OBSERVE_LOCKS_QUERY_SPECS
    raise DbQueryValidationError("db_verifier_query_mode_invalid")


def query_spec_by_id(query_id: str) -> DbQuerySpec[StrictDbVerifierModel]:
    """未登録query IDを動的SQLへ変換せず、固定catalogからだけ解決する。"""

    try:
        return QUERY_SPECS_BY_ID[query_id]
    except KeyError:
        raise DbQueryValidationError("db_verifier_query_id_unknown") from None
