from __future__ import annotations

import re
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID

import pytest
from pydantic import StrictInt, ValidationError
from sqlalchemy import text
from sqlalchemy.sql.elements import TextClause

from quiz_app.load_test.db_verifier_contracts import (
    DbVerifierMode,
    StrictDbVerifierModel,
)
from quiz_app.load_test.db_verifier_queries import (
    ALL_QUERY_SPECS,
    APPLICATION_SELECT_COLUMN_ALLOWLIST,
    FINAL_QUERY_SPECS,
    OBSERVE_LOCKS_QUERY_SPECS,
    PER_ROUND_QUERY_SPECS,
    PROHIBITED_RESULT_COLUMNS,
    QUERY_SPECS_BY_ID,
    AnswerRow,
    BindName,
    DbQueryDefinitionError,
    DbQuerySpec,
    DbQueryValidationError,
    FinalEventRow,
    FinalIntegritySummaryRow,
    FinalParticipantSummaryRow,
    FinalRoundRow,
    IdempotencyRecordRow,
    LockObservationRow,
    OperationAuditRow,
    OptionStatRow,
    QueryId,
    RankingConsistencyRow,
    ReadinessSummaryRow,
    RoundIdentityRow,
    ScoreLedgerRow,
    ScoringPolicyRow,
    query_spec_by_id,
    query_specs_for_mode,
    validate_query_parameters,
    validate_query_rows,
)

EVENT_ID = UUID("10000000-0000-4000-8000-000000000001")
ROUND_ID = UUID("20000000-0000-4000-8000-000000000001")
QUESTION_ID = UUID("30000000-0000-4000-8000-000000000001")
OPTION_ID = UUID("40000000-0000-4000-8000-000000000001")
PARTICIPANT_ID = UUID("50000000-0000-4000-8000-000000000001")
ANSWER_ID = UUID("60000000-0000-4000-8000-000000000001")
CLIENT_ANSWER_ID = UUID("70000000-0000-4000-8000-000000000001")
ROW_ID = UUID("80000000-0000-4000-8000-000000000001")
ACCOUNT_ID = UUID("90000000-0000-4000-8000-000000000001")
IDEMPOTENCY_KEY = UUID("a0000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)


def _sample_for(model: type[StrictDbVerifierModel]) -> dict[str, object]:
    if model is RoundIdentityRow:
        return {
            "event_id": EVENT_ID,
            "event_version": 8,
            "event_state": "InProgress",
            "ranking_visibility": "HiddenAll",
            "round_id": ROUND_ID,
            "round_version": 7,
            "round_state": "Reveal",
            "result_publication": "Published",
            "question_id": QUESTION_ID,
            "question_version": 1,
            "open_at": NOW,
            "standard_close_at": NOW + timedelta(seconds=10),
            "revealed_at": NOW + timedelta(seconds=16),
        }
    if model is ReadinessSummaryRow:
        return {
            "round_id": ROUND_ID,
            "round_version": 7,
            "target_count": 600,
            "report_count": 600,
            "ready_count": 600,
            "asset_failure_count": 0,
            "stale_report_count": 0,
            "non_target_report_count": 0,
        }
    if model is ScoringPolicyRow:
        return {
            "scoring_policy_version": "load-test-v1",
            "base_points": 100,
            "initial_speed_bonus": 50,
            "speed_bucket_ms": 100,
            "speed_decrement_per_bucket": 1,
            "standard_duration_ms": 10000,
            "is_approved": True,
            "timing_basis": "server_received_v1",
            "arrival_grace_ms": 0,
            "clock_tolerance_ms": 0,
            "client_ahead_tolerance_ms": 0,
        }
    if model is AnswerRow:
        return {
            "answer_id": ANSWER_ID,
            "participant_id": PARTICIPANT_ID,
            "participant_event_id": EVENT_ID,
            "round_id": ROUND_ID,
            "option_id": OPTION_ID,
            "option_question_id": QUESTION_ID,
            "option_label": "B",
            "option_is_correct": True,
            "client_answer_id": CLIENT_ANSWER_ID,
            "received_at": NOW + timedelta(seconds=1),
            "is_ranking_eligible": True,
            "client_elapsed_ms": None,
        }
    if model is ScoreLedgerRow:
        return {
            "score_ledger_id": ROW_ID,
            "round_id": ROUND_ID,
            "participant_id": PARTICIPANT_ID,
            "scoring_policy_version": "load-test-v1",
            "is_correct": True,
            "base_points": 100,
            "speed_points": 50,
            "total_points": 150,
            "elapsed_ms": 1000,
            "speed_bucket": 10,
            "speed_rank": 1,
            "speed_cohort_size": 126,
            "speed_cohort": "Correct",
            "exclusion_reason": "None",
        }
    if model is OptionStatRow:
        return {
            "round_id": ROUND_ID,
            "option_id": OPTION_ID,
            "option_question_id": QUESTION_ID,
            "option_label": "B",
            "display_order": 1,
            "option_is_correct": True,
            "valid_response_count": 125,
            "response_percentage": Decimal("25.00"),
        }
    if model is RankingConsistencyRow:
        return {
            "participant_id": PARTICIPANT_ID,
            "snapshot_id": ROW_ID,
            "snapshot_version": 1,
            "snapshot_round_id": ROUND_ID,
            "match_state": "expected-and-stored",
            "stored_overall_rank": 1,
            "expected_overall_rank": 1,
            "stored_total_points": 150,
            "expected_total_points": 150,
            "stored_correct_count": 1,
            "expected_correct_count": 1,
        }
    if model is OperationAuditRow:
        return {
            "is_automatic_aggregation": False,
            "audit_id": ROW_ID,
            "event_id": EVENT_ID,
            "round_id": ROUND_ID,
            "account_id": ACCOUNT_ID,
            "operation": "publish_round_results",
            "before_state": "AwaitingValidation",
            "after_state": "Reveal",
            "outcome": "Succeeded",
            "idempotency_key": IDEMPOTENCY_KEY,
            "occurred_at": NOW,
        }
    if model is IdempotencyRecordRow:
        return {
            "record_id": ROW_ID,
            "account_id": ACCOUNT_ID,
            "idempotency_key": IDEMPOTENCY_KEY,
            "operation": "publish_round_results",
            "response_status": 200,
            "resulting_version": 7,
            "created_at": NOW,
            "expires_at": NOW + timedelta(hours=1),
        }
    if model is FinalEventRow:
        return {
            "event_id": EVENT_ID,
            "event_version": 42,
            "event_state": "InProgress",
            "ranking_visibility": "HiddenAll",
            "scoring_policy_version": "load-test-v1",
            "display_name_policy_version": "load-test-v1",
        }
    if model is FinalRoundRow:
        return {
            "round_id": ROUND_ID,
            "round_version": 7,
            "round_state": "RoundBackupValidated",
            "result_publication": "Published",
            "question_id": QUESTION_ID,
            "question_version": 1,
            "open_at": NOW,
            "standard_close_at": NOW + timedelta(seconds=10),
            "revealed_at": NOW + timedelta(seconds=16),
        }
    if model is FinalParticipantSummaryRow:
        return {
            "participant_count": 600,
            "distinct_participant_id_count": 600,
            "ranking_eligible_count": 600,
            "session_count": 600,
            "distinct_session_participant_count": 600,
            "active_session_count": 600,
            "revoked_session_count": 0,
        }
    if model is FinalIntegritySummaryRow:
        return {
            "round_count": 10,
            "distinct_round_id_count": 10,
            "distinct_question_id_count": 10,
            "preparation_target_count": 6000,
            "distinct_preparation_target_count": 6000,
            "readiness_report_count": 6000,
            "distinct_readiness_report_count": 6000,
            "answer_count": 5000,
            "distinct_answer_id_count": 5000,
            "distinct_answer_participant_round_count": 5000,
            "distinct_client_answer_id_count": 5000,
            "score_ledger_count": 5000,
            "distinct_score_participant_round_count": 5000,
            "option_stat_count": 40,
            "distinct_option_stat_count": 40,
            "rank_snapshot_count": 10,
            "distinct_snapshot_round_count": 10,
            "rank_snapshot_row_count": 5018,
        }
    if model is LockObservationRow:
        return {
            "observed_at": NOW,
            "total_lock_count": 4,
            "waiting_lock_count": 0,
            "waiting_session_count": 0,
            "active_transaction_count": 1,
            "idle_in_transaction_count": 0,
            "oldest_transaction_age_ms": Decimal("12.5"),
        }
    raise AssertionError(f"sample missing for {model.__name__}")


def _outer_select_aliases(sql: str) -> tuple[str, ...]:
    """固定SQLの最外SELECT projectionだけからaliasを読むtest helper。"""

    def keyword_at(index: int, keyword: str) -> bool:
        before = sql[index - 1] if index else " "
        after_index = index + len(keyword)
        after = sql[after_index] if after_index < len(sql) else " "
        return (
            sql[index:after_index].upper() == keyword
            and not (before.isalnum() or before == "_")
            and not (after.isalnum() or after == "_")
        )

    depth = 0
    in_string = False
    projection_start: int | None = None
    projection_end = len(sql)
    index = 0
    while index < len(sql):
        character = sql[index]
        if character == "'":
            if in_string and index + 1 < len(sql) and sql[index + 1] == "'":
                index += 2
                continue
            in_string = not in_string
        elif not in_string:
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
            elif depth == 0 and projection_start is None and keyword_at(index, "SELECT"):
                projection_start = index + len("SELECT")
                index = projection_start
                continue
            elif depth == 0 and projection_start is not None and keyword_at(index, "FROM"):
                projection_end = index
                break
        index += 1
    assert projection_start is not None

    projection = sql[projection_start:projection_end]
    expressions: list[str] = []
    expression_start = 0
    depth = 0
    in_string = False
    for index, character in enumerate(projection):
        if character == "'":
            if in_string and index + 1 < len(projection) and projection[index + 1] == "'":
                continue
            in_string = not in_string
        elif not in_string:
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
            elif character == "," and depth == 0:
                expressions.append(projection[expression_start:index].strip())
                expression_start = index + 1
    expressions.append(projection[expression_start:].strip())

    aliases: list[str] = []
    for expression in expressions:
        match = re.search(r"\bAS\s+([a-z][a-z0-9_]*)\s*$", expression, re.IGNORECASE)
        assert match is not None, expression
        aliases.append(match.group(1))
    return tuple(aliases)


def test_query_catalog_is_partitioned_by_exact_modes_and_unique_ids() -> None:
    assert query_specs_for_mode("per-round") is PER_ROUND_QUERY_SPECS
    assert query_specs_for_mode("final") is FINAL_QUERY_SPECS
    assert query_specs_for_mode("observe-locks") is OBSERVE_LOCKS_QUERY_SPECS
    assert tuple(QUERY_SPECS_BY_ID) == tuple(spec.query_id for spec in ALL_QUERY_SPECS)
    assert len(QUERY_SPECS_BY_ID) == len(ALL_QUERY_SPECS)
    for spec in ALL_QUERY_SPECS:
        assert query_spec_by_id(spec.query_id) is spec


def test_unknown_mode_and_query_id_fail_closed() -> None:
    with pytest.raises(DbQueryValidationError, match="^db_verifier_query_mode_invalid$"):
        query_specs_for_mode(cast(DbVerifierMode, "adhoc"))
    with pytest.raises(DbQueryValidationError, match="^db_verifier_query_id_unknown$"):
        query_spec_by_id("caller_supplied_sql")


def test_every_statement_is_fixed_select_only_text_with_exact_bind_names() -> None:
    mutation = re.compile(
        r"\b(insert|update|delete|merge|alter|drop|truncate|create|grant|revoke|copy|call|do)\b",
        re.IGNORECASE,
    )
    for spec in ALL_QUERY_SPECS:
        assert isinstance(spec.statement, TextClause)
        sql = str(spec.statement).strip()
        assert sql.split(maxsplit=1)[0].upper() in {"SELECT", "WITH"}
        assert ";" not in sql
        assert "--" not in sql
        assert "/*" not in sql
        assert mutation.search(sql) is None
        assert frozenset(spec.statement.compile().params) == frozenset(spec.bind_names)
        assert set(spec.bind_names) <= {"event_id", "round_id"}


def test_every_result_allowlist_exactly_matches_strict_typed_row() -> None:
    for spec in ALL_QUERY_SPECS:
        assert spec.row_model.model_config["strict"] is True
        assert spec.row_model.model_config["extra"] == "forbid"
        assert spec.row_model.model_config["frozen"] is True
        assert spec.selected_columns == tuple(spec.row_model.model_fields)
        assert len(spec.selected_columns) == len(set(spec.selected_columns))
        assert set(spec.selected_columns).isdisjoint(PROHIBITED_RESULT_COLUMNS)
        sql = str(spec.statement)
        assert _outer_select_aliases(sql) == spec.selected_columns


def test_catalog_never_returns_secret_or_personal_value_columns() -> None:
    selected = {column for spec in ALL_QUERY_SPECS for column in spec.selected_columns}
    assert selected.isdisjoint(
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
    audit_columns = set(query_spec_by_id("operation_audits").selected_columns)
    idempotency_columns = set(query_spec_by_id("idempotency_records").selected_columns)
    assert {"reason", "target_id"}.isdisjoint(audit_columns)
    assert {"request_hash", "response_json"}.isdisjoint(idempotency_columns)

    forbidden_references = (
        "password_hash",
        "token_hash",
        "csrf_hash",
        "request_hash",
        "response_json",
        "join_attempt_hash",
        "display_name",
        "display_name_uniqueness_key",
    )
    for spec in ALL_QUERY_SPECS:
        sql = str(spec.statement)
        for column in forbidden_references:
            assert re.search(rf"\b{column}\b", sql, re.IGNORECASE) is None
        if spec.query_id not in {"ranking_consistency", "final_ranking_consistency"}:
            assert re.search(r"\bpublic_label\b", sql, re.IGNORECASE) is None


def test_application_column_privileges_are_exact_and_exclude_secret_columns() -> None:
    assert ("alembic_version", "version_num") in APPLICATION_SELECT_COLUMN_ALLOWLIST
    assert {table for table, _ in APPLICATION_SELECT_COLUMN_ALLOWLIST} == {
        "alembic_version",
        "answers",
        "events",
        "idempotency_records",
        "operation_audits",
        "participant_sessions",
        "participants",
        "question_options",
        "questions",
        "quiz_rounds",
        "rank_snapshot_rows",
        "rank_snapshots",
        "readiness_reports",
        "round_option_stats",
        "round_preparation_targets",
        "score_ledgers",
        "scoring_policies",
    }
    assert APPLICATION_SELECT_COLUMN_ALLOWLIST.isdisjoint(
        {
            ("admin_accounts", "password_hash"),
            ("admin_sessions", "token_hash"),
            ("admin_sessions", "csrf_hash"),
            ("participant_sessions", "token_hash"),
            ("participants", "join_attempt_hash"),
            ("participants", "display_name"),
            ("participants", "display_name_uniqueness_key"),
            ("idempotency_records", "request_hash"),
            ("idempotency_records", "response_json"),
        }
    )


def test_expected_schema_relations_and_lock_views_are_fixed() -> None:
    per_round_sql = "\n".join(str(spec.statement) for spec in PER_ROUND_QUERY_SPECS)
    final_sql = "\n".join(str(spec.statement) for spec in FINAL_QUERY_SPECS)
    lock_sql = str(query_spec_by_id("lock_observation").statement)
    for relation in (
        "events",
        "questions",
        "question_options",
        "quiz_rounds",
        "participants",
        "answers",
        "score_ledgers",
        "readiness_reports",
        "round_preparation_targets",
        "round_option_stats",
        "rank_snapshots",
        "rank_snapshot_rows",
        "operation_audits",
        "idempotency_records",
    ):
        assert relation in per_round_sql or relation in final_sql
    assert "pg_catalog.pg_locks" in lock_sql
    assert "l.granted IS FALSE" in lock_sql
    assert "pg_catalog.pg_stat_activity" in lock_sql
    assert lock_sql.count("JOIN pg_catalog.pg_stat_activity AS a ON a.pid = l.pid") == 3
    assert "l.database" not in lock_sql
    assert "query" not in query_spec_by_id("lock_observation").selected_columns
    assert "pid" not in query_spec_by_id("lock_observation").selected_columns


def test_all_application_relations_are_public_schema_qualified() -> None:
    application_relations = {
        table for table, _ in APPLICATION_SELECT_COLUMN_ALLOWLIST if table != "alembic_version"
    }
    for spec in ALL_QUERY_SPECS:
        sql = str(spec.statement)
        for relation in application_relations:
            assert (
                re.search(
                    rf"\b(?:FROM|JOIN)\s+(?!public\.){re.escape(relation)}\b",
                    sql,
                    re.IGNORECASE,
                )
                is None
            )
            for keyword in ("FROM", "JOIN"):
                unqualified = f"{keyword} {relation}"
                qualified = f"{keyword} public.{relation}"
                if unqualified in sql or qualified in sql:
                    assert qualified in sql


def test_ranking_queries_compare_stored_snapshot_to_domain_order_without_returning_label() -> None:
    for query_id in ("ranking_consistency", "final_ranking_consistency"):
        spec = query_spec_by_id(query_id)
        sql = str(spec.statement)
        assert "RANK() OVER" in sql
        assert "computed_total_points DESC" in sql
        assert "computed_correct_count DESC" in sql
        assert "computed_correct_elapsed_ms ASC" in sql
        assert "FULL OUTER JOIN stored" in sql
        assert "public_label" not in spec.selected_columns


def test_final_integrity_query_counts_uniqueness_without_selecting_identifiers() -> None:
    spec = query_spec_by_id("final_integrity_summary")
    sql = str(spec.statement)
    assert "COUNT(DISTINCT (a.participant_id, a.round_id))" in sql
    assert "COUNT(DISTINCT a.client_answer_id)" in sql
    assert "COUNT(DISTINCT (sl.participant_id, sl.round_id))" in sql
    assert "COUNT(DISTINCT (ros.round_id, ros.option_id))" in sql
    assert spec.exact_rows == 1


def test_validate_parameters_accepts_only_exact_uuid_bind_mapping() -> None:
    spec = query_spec_by_id("answers")
    raw_parameters = {"event_id": EVENT_ID, "round_id": ROUND_ID}
    parameters = validate_query_parameters(
        spec,
        raw_parameters,
    )
    assert parameters == {"event_id": EVENT_ID, "round_id": ROUND_ID}
    assert parameters is not raw_parameters

    for invalid in (
        {"event_id": EVENT_ID},
        {"event_id": EVENT_ID, "round_id": ROUND_ID, "limit": 1},
        {"event_id": str(EVENT_ID), "round_id": ROUND_ID},
        {"event_id": EVENT_ID, "round_id": str(ROUND_ID)},
    ):
        with pytest.raises(
            DbQueryValidationError,
            match="^db_verifier_query_parameters_invalid:answers$",
        ):
            validate_query_parameters(spec, invalid)


def test_parameterless_lock_query_rejects_all_caller_values() -> None:
    spec = query_spec_by_id("lock_observation")
    assert validate_query_parameters(spec, {}) == {}
    with pytest.raises(
        DbQueryValidationError,
        match="^db_verifier_query_parameters_invalid:lock_observation$",
    ):
        validate_query_parameters(spec, {"event_id": EVENT_ID})


def test_all_query_row_models_accept_driver_native_types() -> None:
    for spec in ALL_QUERY_SPECS:
        row = spec.row_model.model_validate(_sample_for(spec.row_model), strict=True)
        assert type(row) is spec.row_model


def test_validate_rows_returns_typed_immutable_tuple_for_every_spec() -> None:
    for spec in ALL_QUERY_SPECS:
        sample = _sample_for(spec.row_model)
        count = spec.exact_rows if spec.exact_rows is not None else 1
        validated = validate_query_rows(spec, [dict(sample) for _ in range(count)])
        assert isinstance(validated, tuple)
        assert len(validated) == count
        assert all(type(row) is spec.row_model for row in validated)


def test_validate_rows_rejects_missing_extra_and_coerced_columns_without_values() -> None:
    spec = query_spec_by_id("answers")
    sample = _sample_for(AnswerRow)
    missing = dict(sample)
    missing.pop("answer_id")
    extra = {**sample, "token_hash": "must-not-be-returned"}
    coerced = {**sample, "option_is_correct": 1}

    with pytest.raises(
        DbQueryValidationError,
        match="^db_verifier_query_column_mismatch:answers$",
    ):
        validate_query_rows(spec, [missing])
    with pytest.raises(
        DbQueryValidationError,
        match="^db_verifier_query_column_mismatch:answers$",
    ):
        validate_query_rows(spec, [extra])
    with pytest.raises(
        DbQueryValidationError,
        match="^db_verifier_query_row_invalid:answers$",
    ):
        validate_query_rows(spec, [coerced])


def test_validate_rows_rejects_naive_datetime_and_string_decimal() -> None:
    identity = _sample_for(RoundIdentityRow)
    identity["open_at"] = NOW.replace(tzinfo=None)
    with pytest.raises(
        DbQueryValidationError,
        match="^db_verifier_query_row_invalid:round_identity$",
    ):
        validate_query_rows(query_spec_by_id("round_identity"), [identity])

    option = _sample_for(OptionStatRow)
    option["response_percentage"] = "25.00"
    with pytest.raises(
        DbQueryValidationError,
        match="^db_verifier_query_row_invalid:option_stats$",
    ):
        validate_query_rows(query_spec_by_id("option_stats"), [option] * 4)


def test_validate_rows_enforces_exact_and_maximum_counts() -> None:
    identity = _sample_for(RoundIdentityRow)
    spec = query_spec_by_id("round_identity")
    with pytest.raises(
        DbQueryValidationError,
        match="^db_verifier_query_row_count_invalid:round_identity$",
    ):
        validate_query_rows(spec, [])
    with pytest.raises(
        DbQueryValidationError,
        match="^db_verifier_query_row_limit_exceeded:round_identity$",
    ):
        validate_query_rows(spec, [identity, identity])

    answer_spec = query_spec_by_id("answers")
    answer = _sample_for(AnswerRow)
    with pytest.raises(
        DbQueryValidationError,
        match="^db_verifier_query_row_limit_exceeded:answers$",
    ):
        validate_query_rows(answer_spec, (answer for _ in range(601)))


def test_strict_rows_reject_invalid_domain_values() -> None:
    invalid_identity = _sample_for(RoundIdentityRow)
    invalid_identity["round_state"] = "AwaitingValidation"
    with pytest.raises(ValidationError):
        RoundIdentityRow.model_validate(invalid_identity, strict=True)

    invalid_score = _sample_for(ScoreLedgerRow)
    invalid_score["total_points"] = -1
    with pytest.raises(ValidationError):
        ScoreLedgerRow.model_validate(invalid_score, strict=True)

    invalid_lock = _sample_for(LockObservationRow)
    invalid_lock["waiting_lock_count"] = -1
    with pytest.raises(ValidationError):
        LockObservationRow.model_validate(invalid_lock, strict=True)


class _OneColumnRow(StrictDbVerifierModel):
    value: StrictInt


def _spec(
    statement: TextClause,
    *,
    bind_names: tuple[BindName, ...] = (),
    columns: tuple[str, ...] = ("value",),
    max_rows: int = 1,
    exact_rows: int | None = None,
    mode: DbVerifierMode = "per-round",
) -> DbQuerySpec[_OneColumnRow]:
    return DbQuerySpec(
        query_id=cast(QueryId, "round_identity"),
        mode=mode,
        statement=statement,
        bind_names=bind_names,
        selected_columns=columns,
        row_model=_OneColumnRow,
        max_rows=max_rows,
        exact_rows=exact_rows,
    )


@pytest.mark.parametrize(
    "statement",
    (
        text("UPDATE answers SET received_at = clock_timestamp()"),
        text("DELETE FROM answers"),
        text("SELECT 1 AS value; SELECT 2 AS value"),
        text("SELECT 1 AS value -- injected"),
    ),
)
def test_query_spec_rejects_non_select_or_multi_statement_sql(
    statement: TextClause,
) -> None:
    with pytest.raises(DbQueryDefinitionError, match="^db_verifier_query_not_select_only$"):
        _spec(statement)


def test_query_spec_rejects_bind_column_and_limit_definition_drift() -> None:
    with pytest.raises(DbQueryDefinitionError, match="^db_verifier_query_bind_names_invalid$"):
        _spec(text("SELECT :event_id AS value"))
    with pytest.raises(DbQueryDefinitionError, match="^db_verifier_query_columns_invalid$"):
        _spec(text("SELECT 1 AS value"), columns=("token_hash",))
    with pytest.raises(DbQueryDefinitionError, match="^db_verifier_query_row_limit_invalid$"):
        _spec(text("SELECT 1 AS value"), max_rows=0)
    with pytest.raises(DbQueryDefinitionError, match="^db_verifier_query_row_limit_invalid$"):
        _spec(text("SELECT 1 AS value"), max_rows=1, exact_rows=2)


def test_query_spec_is_immutable() -> None:
    spec = _spec(text("SELECT 1 AS value"))
    with pytest.raises(FrozenInstanceError):
        spec.max_rows = 2  # type: ignore[misc]


def test_custom_sleep_select_is_not_a_catalog_singleton() -> None:
    custom = _spec(text("SELECT pg_sleep(1) AS value"))
    assert query_spec_by_id(custom.query_id) is not custom


def test_idempotency_queries_have_one_event_filter_and_compile_exactly() -> None:
    for query_id, bind_names in (
        ("idempotency_records", {"event_id", "round_id"}),
        ("final_idempotency_records", {"event_id"}),
    ):
        statement = query_spec_by_id(query_id).statement
        sql = str(statement)
        assert len(re.findall(r"\bWHERE\s+oa\.event_id\b", sql, re.IGNORECASE)) == 1
        assert set(statement.compile().params) == bind_names
