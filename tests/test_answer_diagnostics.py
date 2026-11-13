import asyncio
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from io import StringIO
from types import SimpleNamespace, TracebackType
from typing import cast
from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session
from starlette.types import Message, Receive, Scope, Send

import quiz_app.observability.answer_diagnostics as diagnostics_module
from quiz_app.audience.auth_service import ParticipantAuthService
from quiz_app.audience.errors import ParticipantSessionInvalidError
from quiz_app.audience.projection import AudienceProjectionService
from quiz_app.config import Settings
from quiz_app.domain.errors import (
    AnswerContentConflictError,
    AnswerWindowClosedError,
    ClientReceiptDifferenceError,
    ClientSelectionTimeError,
    DomainInvariantError,
    InvalidStateTransitionError,
    PublicationTooEarlyError,
    ResourceNotFoundError,
    VersionConflictError,
)
from quiz_app.models import Answer, ScoringPolicy
from quiz_app.observability.answer_diagnostics import (
    AnswerDiagnostic,
    AnswerDiagnosticRecord,
    DiagnosticEvent,
    DiagnosticReason,
    bind_answer_diagnostic,
    current_answer_diagnostic,
    diagnostic_reason,
    preserve_diagnostic_codes,
    record_answer_api_error,
)
from quiz_app.observability.metrics import QuizMetrics
from quiz_app.observability.middleware import PrometheusMiddleware
from quiz_app.observability.request_timing import current_request_timing

SESSION_ID = UUID("3192e5da-588e-4610-9081-69bb590f74f1")
CLIENT_ANSWER_ID = UUID("374d45dd-3491-407c-8c6c-f060c981e99c")
SIGNING_FIXTURE = SecretStr("diagnostic-secret-must-not-appear")
BODY_FIXTURE = "question-and-answer-body-must-not-appear"
ANSWER_PATH = f"/api/v1/participant/rounds/{CLIENT_ANSWER_ID}/answers"


@pytest.fixture
def diagnostic_records(monkeypatch: pytest.MonkeyPatch) -> list[AnswerDiagnosticRecord]:
    records: list[AnswerDiagnosticRecord] = []

    def capture(_message: str, *, record: str, **_fields: str) -> None:
        records.append(AnswerDiagnosticRecord.model_validate_json(record))

    monkeypatch.setattr(diagnostics_module.logfire, "info", capture)
    monkeypatch.setattr(
        diagnostics_module,
        "get_settings",
        lambda: SimpleNamespace(session_secret=SIGNING_FIXTURE),
    )
    return records


@pytest.fixture
def answer_diagnostic() -> Iterator[AnswerDiagnostic]:
    diagnostic = AnswerDiagnostic("answer_post")
    token = current_answer_diagnostic.set(diagnostic)
    try:
        yield diagnostic
    finally:
        current_answer_diagnostic.reset(token)


def test_hmac_matches_post_and_status_but_separates_session_and_answer(
    diagnostic_records: list[AnswerDiagnosticRecord],
) -> None:
    post = AnswerDiagnostic("answer_post")
    status = AnswerDiagnostic("answer_status")
    another_session = AnswerDiagnostic("answer_post")
    another_answer = AnswerDiagnostic("answer_status")
    post.bind(SESSION_ID, CLIENT_ANSWER_ID)
    status.bind(SESSION_ID, CLIENT_ANSWER_ID)
    another_session.bind(UUID(int=42), CLIENT_ANSWER_ID)
    another_answer.bind(SESSION_ID, UUID(int=43))

    assert post.answer_trace == status.answer_trace
    assert len({post.answer_trace, another_session.answer_trace, another_answer.answer_trace}) == 3
    assert post.request_id != status.request_id
    assert post.clock_domain == status.clock_domain
    assert len(diagnostic_records) == 4
    serialized = "\n".join(record.model_dump_json() for record in diagnostic_records)
    for raw_value in (
        str(SESSION_ID),
        SESSION_ID.hex,
        str(CLIENT_ANSWER_ID),
        CLIENT_ANSWER_ID.hex,
        SIGNING_FIXTURE.get_secret_value(),
        BODY_FIXTURE,
    ):
        assert raw_value not in serialized


def test_native_logfire_console_keeps_complete_safe_json(
    diagnostic_records: list[AnswerDiagnosticRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SDKの長い引数省略を通り、console回収でも相関JSONが欠けないことを確認する。"""

    del diagnostic_records
    output = StringIO()
    logger = diagnostics_module.logfire.configure(
        local=True,
        send_to_logfire=False,
        scrubbing=diagnostics_module.logfire.ScrubbingOptions(callback=preserve_diagnostic_codes),
        console=diagnostics_module.logfire.ConsoleOptions(
            colors="never",
            include_timestamps=False,
            output=output,
        ),
    )
    monkeypatch.setattr(diagnostics_module.logfire, "info", logger.info)
    trace = AnswerDiagnostic("answer_post")
    error = app_frame_error()
    auth_error = auth_frame_error()
    token = current_answer_diagnostic.set(trace)
    try:
        trace.bind(SESSION_ID, CLIENT_ANSWER_ID)
        trace.emit("commit_started", is_replay=False)
        trace.emit("commit_acknowledged", is_replay=False)
        trace.emit(
            "api_error",
            reason="authentication_required",
            api_code="participant_authentication_required",
            http_status=401,
        )
        diagnostics_module.record_answer_failure_stack(error, "api_error")
        diagnostics_module.record_answer_failure_stack(auth_error, "api_error")
        logger.info("privacy regression {api_code}", api_code="auth=private-sensitive-sentinel")
    finally:
        current_answer_diagnostic.reset(token)
        assert logger.shutdown(timeout_millis=1000)
    lines = [
        line.split("answer diagnostic ", 1)[1]
        for line in output.getvalue().splitlines()
        if "answer diagnostic " in line
    ]
    records = [AnswerDiagnosticRecord.model_validate_json(line) for line in lines]
    assert [record.event for record in records] == [
        "answer_bound",
        "commit_started",
        "commit_acknowledged",
        "api_error",
        "failure_frame",
        "failure_frame",
        "failure_frame",
    ]
    assert all(record.answer_trace == trace.answer_trace for record in records)
    assert all(record.request_id == trace.request_id for record in records)
    assert all(len(line) > 128 for line in lines)
    assert records[3].api_code == "participant_authentication_required"
    assert records[-3].source_module == "quiz_app.audience.projection"
    assert records[-3].source_function == "_answer_status"
    assert {record.source_function for record in records[-2:]} == {
        "authenticate_in_session",
        "_verify_in_session",
    }
    assert all(record.source_module == "quiz_app.audience.auth_service" for record in records[-2:])
    assert records[-1].failure_boundary == "api_error"
    assert "private-sensitive-sentinel" not in output.getvalue()
    for raw_value in (str(SESSION_ID), str(CLIENT_ANSWER_ID), SIGNING_FIXTURE.get_secret_value()):
        assert raw_value not in output.getvalue()
    for private_value in (BODY_FIXTURE, "private-args", "private-cause", "private-locals"):
        assert private_value not in output.getvalue()


def app_frame_error() -> SQLAlchemyError:
    """実appのprojection frameを通り、test側の秘密locals/causeも持つ例外を作る。"""

    class FailedSession:
        def execute(self, _statement: object) -> None:
            private_locals = {"participant_id": SESSION_ID, "body": BODY_FIXTURE}
            raise SQLAlchemyError("private-args", "private-locals", private_locals) from ValueError(
                "private-cause", SIGNING_FIXTURE.get_secret_value()
            )

    with pytest.raises(SQLAlchemyError) as captured:
        AudienceProjectionService._answer_status(
            cast(Session, FailedSession()), SESSION_ID, CLIENT_ANSWER_ID
        )
    return captured.value


def auth_frame_error() -> ParticipantSessionInvalidError:
    """実認証serviceを通し、DB行が無い結果だけをmockして認証frameを得る。"""

    class EmptyRow:
        def one_or_none(self) -> None:
            return None

    class UnauthenticatedSession:
        def execute(self, _statement: object) -> EmptyRow:
            return EmptyRow()

    service = ParticipantAuthService(
        cast(Engine, object()), Settings(_env_file=None, session_secret=SIGNING_FIXTURE)
    )
    with pytest.raises(ParticipantSessionInvalidError) as captured:
        service.authenticate_in_session(cast(Session, UnauthenticatedSession()), BODY_FIXTURE)
    return captured.value


def test_failure_stack_keeps_only_app_frame_metadata_and_no_private_values(
    diagnostic_records: list[AnswerDiagnosticRecord],
    answer_diagnostic: AnswerDiagnostic,
) -> None:
    bind_answer_diagnostic(SESSION_ID, CLIENT_ANSWER_ID)
    error = app_frame_error()
    diagnostics_module.record_answer_failure_stack(error, "transaction_failed")
    frames = [record for record in diagnostic_records if record.event == "failure_frame"]
    assert len(frames) == 1
    assert frames[0].source_module == "quiz_app.audience.projection"
    assert frames[0].source_function == "_answer_status"
    assert frames[0].source_line is not None and frames[0].source_line > 0
    assert frames[0].frame_index == 0
    assert frames[0].failure_boundary == "transaction_failed"
    assert frames[0].request_id == answer_diagnostic.request_id
    assert frames[0].answer_trace == answer_diagnostic.answer_trace
    serialized = "\n".join(record.model_dump_json() for record in diagnostic_records)
    for private_value in (
        str(SESSION_ID),
        str(CLIENT_ANSWER_ID),
        BODY_FIXTURE,
        SIGNING_FIXTURE.get_secret_value(),
        "private-args",
        "private-locals",
        "private-cause",
        "_statement",
        "FailedSession",
        "/Users/",
        "session.scalar",
        "raise SQLAlchemyError",
    ):
        assert private_value not in serialized


def test_failure_stack_caps_records_at_32_app_frames(
    diagnostic_records: list[AnswerDiagnosticRecord],
    answer_diagnostic: AnswerDiagnostic,
) -> None:
    """実app frameを人工的に反復したtracebackで、記録件数の上限だけを検証する。"""
    del answer_diagnostic
    error = app_frame_error()
    application_frame = error.__traceback__
    while application_frame is not None:
        if application_frame.tb_frame.f_code.co_filename.endswith(
            "/quiz_app/audience/projection.py"
        ):
            break
        application_frame = application_frame.tb_next
    assert application_frame is not None
    repeated: TracebackType | None = None
    for _ in range(40):
        repeated = TracebackType(
            repeated,
            application_frame.tb_frame,
            application_frame.tb_lasti,
            application_frame.tb_lineno,
        )
    diagnostics_module.record_answer_failure_stack(error.with_traceback(repeated), "api_error")
    frames = [record for record in diagnostic_records if record.event == "failure_frame"]
    assert len(frames) == 32
    assert [record.frame_index for record in frames] == list(range(32))
    assert {record.source_module for record in frames} == {"quiz_app.audience.projection"}


def test_missing_traceback_or_outside_app_frames_do_not_create_stack_records(
    diagnostic_records: list[AnswerDiagnosticRecord],
    answer_diagnostic: AnswerDiagnostic,
) -> None:
    del answer_diagnostic
    diagnostics_module.record_answer_failure_stack(SQLAlchemyError(BODY_FIXTURE), "api_error")
    try:
        raise SQLAlchemyError(BODY_FIXTURE)
    except SQLAlchemyError as error:
        diagnostics_module.record_answer_failure_stack(error, "request_finished")
    assert diagnostic_records == []


@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [
        (("message", "reason"), "authentication_required", "authentication_required"),
        (
            ("attributes", "record", "api_code"),
            "participant_authentication_required",
            "participant_authentication_required",
        ),
        (("attributes", "api_code"), "participant_authentication_required private-token", None),
        (("attributes", "reason"), "authentication_required private-token", None),
        (("attributes", "password"), "authentication_required", None),
        (("attributes", "body", "api_code"), "participant_authentication_required", None),
        (("attributes", "record", "api_code"), {"auth": "private-token"}, None),
        (
            ("message", "source_module"),
            "quiz_app.audience.auth_service",
            "quiz_app.audience.auth_service",
        ),
        (
            ("attributes", "record", "source_function"),
            "authenticate_in_session",
            "authenticate_in_session",
        ),
        (("attributes", "source_module"), "quiz_app.audience.auth_service private-token", None),
        (("message", "source_function"), "authenticate_in_session private-token", None),
        (("attributes", "body", "source_module"), "quiz_app.audience.auth_service", None),
        (("attributes", "source_function"), "authentication-private-token", None),
        (("attributes", "source_module"), "quiz_app.audience.session_private_token", None),
        (("attributes", "source_module"), "quiz_app.audience.auth_service_token", None),
        (("attributes", "source_function"), "get_answer_status_in_session_private_token", None),
        (("attributes", "exception", "source_function"), "_verify_in_session", None),
        (("attributes", "source_function"), {"session": "private-token"}, None),
        (("attributes", "reason"), "authenticate", None),
        *[
            ((*prefix, field), value, value)
            for prefix in (("message",), ("attributes",), ("attributes", "record"))
            for field, value in (
                ("source_module", "quiz_app.audience.auth_service"),
                ("source_module", "quiz_app.audience.auth_cache"),
                ("source_function", "authenticate"),
                ("source_function", "authenticate_in_session"),
                ("source_function", "get_participant_auth_service"),
                ("source_function", "_hash_token"),
                ("source_function", "_verify_in_session"),
                ("source_function", "get_answer_status_in_session"),
                ("source_function", "get_answer_replay_in_session"),
            )
        ],
    ],
)
def test_sdk_scrubbing_exception_keeps_only_exact_public_codes(
    path: tuple[str, ...],
    value: object,
    expected: str | None,
) -> None:
    pattern_match = re.search("auth", "authentication_required")
    assert pattern_match is not None
    match = diagnostics_module.logfire.ScrubMatch(path, value, pattern_match)
    assert preserve_diagnostic_codes(match) == expected


@pytest.mark.parametrize("field", ["participant_id", "question_text", "answer_text", "exception"])
def test_record_schema_rejects_raw_identifiers_payload_and_exception(field: str) -> None:
    record = {
        "request_id": "1" * 32,
        "clock_domain": "2" * 32,
        "monotonic_ns": 1,
        "operation": "answer_post",
        "event": "transaction_failed",
        field: BODY_FIXTURE,
    }
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AnswerDiagnosticRecord.model_validate(record)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event", BODY_FIXTURE),
        ("reason", BODY_FIXTURE),
        ("api_code", BODY_FIXTURE),
        ("status", BODY_FIXTURE),
        ("window_position", BODY_FIXTURE),
        ("transaction_phase", BODY_FIXTURE),
        ("http_status", "202"),
        ("http_status", True),
        ("http_status", 99),
        ("http_status", 600),
        ("is_replay", "true"),
        ("monotonic_ns", "123"),
        ("failure_boundary", BODY_FIXTURE),
        ("source_module", "/Users/private/quiz_app.py"),
        ("source_module", "third_party.sqlalchemy"),
        ("source_module", "quiz_app.private-value"),
        ("source_function", "private-body()"),
        ("source_line", 0),
        ("source_line", "123"),
        ("source_line", True),
        ("frame_index", -1),
        ("frame_index", 32),
        ("frame_index", "0"),
    ],
)
def test_record_schema_rejects_unknown_vocabulary_and_implicit_coercion(
    field: str,
    value: object,
) -> None:
    record: dict[str, object] = {
        "request_id": "1" * 32,
        "clock_domain": "2" * 32,
        "monotonic_ns": 1,
        "operation": "answer_post",
        "event": "request_started",
    }
    record[field] = value
    with pytest.raises(ValidationError):
        AnswerDiagnosticRecord.model_validate(record)


def test_failure_frame_schema_accepts_only_public_source_metadata() -> None:
    record = AnswerDiagnosticRecord.model_validate(
        {
            "request_id": "1" * 32,
            "clock_domain": "2" * 32,
            "monotonic_ns": 1,
            "operation": "answer_post",
            "event": "failure_frame",
            "failure_boundary": "transaction_failed",
            "source_module": "quiz_app.audience.projection",
            "source_function": "_answer_status",
            "source_line": 1,
            "frame_index": 31,
        }
    )
    assert AnswerDiagnosticRecord.model_validate_json(record.model_dump_json()) == record


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (VersionConflictError(BODY_FIXTURE), "version_conflict"),
        (AnswerWindowClosedError(BODY_FIXTURE), "answer_window_closed"),
        (AnswerContentConflictError(BODY_FIXTURE), "answer_content_conflict"),
        (ResourceNotFoundError(BODY_FIXTURE), "resource_not_found"),
        (DomainInvariantError(BODY_FIXTURE), "domain_invariant"),
        (InvalidStateTransitionError(BODY_FIXTURE), "invalid_transition"),
        (PublicationTooEarlyError(BODY_FIXTURE), "publication_too_early"),
        (SQLAlchemyError(BODY_FIXTURE), "database_error"),
        (ParticipantSessionInvalidError(BODY_FIXTURE), "authentication_required"),
        (asyncio.CancelledError(BODY_FIXTURE), "cancelled"),
        (ValueError(BODY_FIXTURE), "unclassified"),
        (None, "unclassified"),
    ],
)
def test_failure_reason_is_fixed_vocabulary(failure: BaseException | None, reason: str) -> None:
    assert diagnostic_reason(failure) == reason


@pytest.mark.parametrize(
    "failure,reason",
    [
        (ClientSelectionTimeError("private"), "client_selection_time_invalid"),
        (ClientReceiptDifferenceError("private"), "client_receipt_difference_exceeded"),
    ],
)
def test_client_time_failures_have_distinct_safe_reasons(
    failure: BaseException, reason: str
) -> None:
    assert diagnostic_reason(failure) == reason


@pytest.mark.parametrize(
    ("code", "expected"), [("state_conflict", "state_conflict"), (BODY_FIXTURE, "other")]
)
def test_api_error_records_status_and_allowlisted_code_only(
    diagnostic_records: list[AnswerDiagnosticRecord],
    answer_diagnostic: AnswerDiagnostic,
    code: str,
    expected: str,
) -> None:
    bind_answer_diagnostic(SESSION_ID, CLIENT_ANSWER_ID)
    record_answer_api_error(409, code)
    record = diagnostic_records[-1]
    assert record.event == "api_error"
    assert record.api_code == expected
    assert record.http_status == 409
    assert record.answer_trace == answer_diagnostic.answer_trace
    assert BODY_FIXTURE not in record.model_dump_json()


def test_no_request_context_does_not_emit_or_create_a_trace(
    diagnostic_records: list[AnswerDiagnosticRecord],
) -> None:
    assert current_answer_diagnostic.get() is None
    bind_answer_diagnostic(SESSION_ID, CLIENT_ANSWER_ID)
    record_answer_api_error(409, "state_conflict")
    diagnostics_module.record_answer_failure_stack(app_frame_error(), "api_error")
    assert diagnostic_records == []


@pytest.mark.parametrize("is_stored", [False, True])
def test_projection_query_events_surround_select_and_record_only_status(
    diagnostic_records: list[AnswerDiagnosticRecord],
    answer_diagnostic: AnswerDiagnostic,
    is_stored: bool,
) -> None:
    """SELECTの戻り値だけを置換し、実projectionの計測位置と情報公開範囲を確認する。"""
    bind_answer_diagnostic(SESSION_ID, CLIENT_ANSWER_ID)
    answer = Answer(
        id=UUID(int=11),
        participant_id=UUID(int=12),
        round_id=UUID(int=13),
        option_id=UUID(int=14),
        client_answer_id=CLIENT_ANSWER_ID,
        received_at=datetime(2026, 10, 1, tzinfo=UTC),
    )

    policy = ScoringPolicy(
        version="fixture",
        base_points=400,
        initial_speed_bonus=100,
        speed_bucket_ms=100,
        speed_decrement_per_bucket=1,
        standard_duration_ms=10000,
    )

    class QuerySession:
        def execute(self, _statement: object) -> "QuerySession":
            assert diagnostic_records[-1].event == "status_query_started"
            return self

        def one_or_none(self) -> tuple[Answer, ScoringPolicy] | None:
            return (answer, policy) if is_stored else None

    result = AudienceProjectionService._answer_status(
        cast(Session, QuerySession()), UUID(int=12), CLIENT_ANSWER_ID
    )
    assert result.status == ("AcceptedLocked" if is_stored else "NotStored")
    assert [record.event for record in diagnostic_records] == [
        "answer_bound",
        "status_query_started",
        "status_query_finished",
    ]
    assert diagnostic_records[-1].status == result.status
    assert diagnostic_records[-1].answer_trace == answer_diagnostic.answer_trace
    serialized = "\n".join(record.model_dump_json() for record in diagnostic_records)
    for identifier in (answer.id, answer.participant_id, answer.round_id, answer.option_id):
        assert str(identifier) not in serialized


def http_scope(path: str = ANSWER_PATH, method: str = "POST") -> Scope:
    return cast(
        Scope,
        {
            "type": "http",
            "method": method,
            "path": path,
            "route": SimpleNamespace(path="/api/v1/participant/rounds/{round_id}/answers"),
            "headers": [(b"x-quiz-request-id", BODY_FIXTURE.encode())],
        },
    )


async def disconnected_receive() -> Message:
    return {"type": "http.disconnect"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "reason", "http_status"),
    [
        (BODY_FIXTURE, None, None),
        ("transaction_failed", BODY_FIXTURE, None),
        ("response_started", None, "202"),
        ("response_started", None, True),
        ("response_started", None, 99),
        ("response_started", None, 600),
    ],
)
async def test_invalid_diagnostic_drops_raw_values_but_preserves_request_and_metrics_context(
    monkeypatch: pytest.MonkeyPatch,
    diagnostic_records: list[AnswerDiagnosticRecord],
    event: str,
    reason: str | None,
    http_status: object,
) -> None:
    warnings: list[str] = []
    sent: list[Message] = []
    metrics = QuizMetrics()
    monkeypatch.setattr(diagnostics_module.logfire, "warning", warnings.append)

    async def app(_: Scope, __: Receive, send: Send) -> None:
        diagnostic = current_answer_diagnostic.get()
        assert diagnostic is not None
        # 型の境界違反を意図的に注入し、Pydantic以降のfail-safe経路を確認する。
        diagnostic.emit(
            cast(DiagnosticEvent, event),
            reason=cast(DiagnosticReason | None, reason),
            http_status=cast(int | None, http_status),
        )
        await send({"type": "http.response.start", "status": 202, "headers": []})
        await send({"type": "http.response.body", "body": b"accepted"})

    async def send(message: Message) -> None:
        sent.append(message)

    await PrometheusMiddleware(app, metrics)(http_scope(), disconnected_receive, send)
    assert sent[-1]["body"] == b"accepted"
    assert current_answer_diagnostic.get() is None
    assert current_request_timing.get() is None
    assert warnings == ["answer diagnostic record invalid"]
    assert [record.event for record in diagnostic_records] == [
        "request_started",
        "response_started",
        "request_finished",
    ]
    assert diagnostic_records[-1].http_status == 202
    assert BODY_FIXTURE not in "\n".join(record.model_dump_json() for record in diagnostic_records)
    rendered = metrics.render().decode()
    assert 'status_class="2xx"' in rendered
    assert 'stage="api_total"' in rendered
    assert BODY_FIXTURE not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "method", "operation"),
    [
        (ANSWER_PATH, "POST", "answer_post"),
        (f"/api/v1/participant/answers/{CLIENT_ANSWER_ID}/status", "GET", "answer_status"),
    ],
)
async def test_middleware_emits_safe_server_headers_and_restores_outer_context(
    diagnostic_records: list[AnswerDiagnosticRecord],
    answer_diagnostic: AnswerDiagnostic,
    path: str,
    method: str,
    operation: str,
) -> None:
    sent: list[Message] = []

    async def app(_: Scope, __: Receive, send: Send) -> None:
        assert current_answer_diagnostic.get() is not answer_diagnostic
        bind_answer_diagnostic(SESSION_ID, CLIENT_ANSWER_ID)
        await send({"type": "http.response.start", "status": 409, "headers": []})
        await send({"type": "http.response.body", "body": BODY_FIXTURE.encode()})

    async def send(message: Message) -> None:
        sent.append(message)

    await PrometheusMiddleware(app, QuizMetrics())(
        http_scope(path, method), disconnected_receive, send
    )
    assert current_answer_diagnostic.get() is answer_diagnostic
    headers = dict(sent[0]["headers"])
    assert len(headers[b"x-quiz-request-id"]) == 32
    assert len(headers[b"x-quiz-answer-trace"]) == 64
    assert headers[b"x-quiz-request-id"].decode() == diagnostic_records[0].request_id
    assert headers[b"x-quiz-answer-trace"].decode() == diagnostic_records[1].answer_trace
    assert [record.event for record in diagnostic_records] == [
        "request_started",
        "answer_bound",
        "response_started",
        "request_finished",
    ]
    assert diagnostic_records[-1].http_status == 409
    assert all(record.operation == operation for record in diagnostic_records)
    assert BODY_FIXTURE not in "\n".join(record.model_dump_json() for record in diagnostic_records)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [asyncio.CancelledError(), RuntimeError(BODY_FIXTURE)])
async def test_before_headers_failure_records_finish_and_clears_context(
    diagnostic_records: list[AnswerDiagnosticRecord],
    failure: BaseException,
) -> None:
    async def app(_: Scope, __: Receive, ___: Send) -> None:
        raise failure

    async def send(_: Message) -> None:
        pytest.fail("response_headers_were_not_expected")

    with pytest.raises(type(failure)):
        await PrometheusMiddleware(app, QuizMetrics())(http_scope(), disconnected_receive, send)
    assert current_answer_diagnostic.get() is None
    assert [record.event for record in diagnostic_records] == [
        "request_started",
        "failure_frame",
        "request_finished",
    ]
    assert diagnostic_records[1].source_module == "quiz_app.observability.middleware"
    assert diagnostic_records[1].failure_boundary == "request_finished"
    assert diagnostic_records[-1].reason == diagnostic_reason(failure)
    assert diagnostic_records[-1].http_status is None
    assert BODY_FIXTURE not in diagnostic_records[-1].model_dump_json()


@pytest.mark.asyncio
async def test_handled_outer_exception_is_not_misattributed_to_a_successful_request(
    diagnostic_records: list[AnswerDiagnosticRecord],
) -> None:
    async def app(_: Scope, __: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def send(_: Message) -> None:
        return

    try:
        raise RuntimeError(BODY_FIXTURE)
    except RuntimeError:
        await PrometheusMiddleware(app, QuizMetrics())(http_scope(), disconnected_receive, send)

    assert current_answer_diagnostic.get() is None
    assert diagnostic_records[-1].event == "request_finished"
    assert diagnostic_records[-1].reason is None
    assert diagnostic_records[-1].http_status == 200


@pytest.mark.asyncio
async def test_response_start_observation_does_not_prove_delivery_after_client_disconnect(
    diagnostic_records: list[AnswerDiagnosticRecord],
) -> None:
    """ASGI send境界だけを模擬し、TLS clientの受信やcommitの成否は推定しない。"""

    async def app(_: Scope, __: Receive, send: Send) -> None:
        bind_answer_diagnostic(SESSION_ID, CLIENT_ANSWER_ID)
        await send({"type": "http.response.start", "status": 202, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            raise BrokenPipeError(BODY_FIXTURE)

    with pytest.raises(BrokenPipeError):
        await PrometheusMiddleware(app, QuizMetrics())(http_scope(), disconnected_receive, send)
    assert current_answer_diagnostic.get() is None
    assert [record.event for record in diagnostic_records if record.event != "failure_frame"] == [
        "request_started",
        "answer_bound",
        "response_started",
        "request_finished",
    ]
    failure_frames = [record for record in diagnostic_records if record.event == "failure_frame"]
    assert len(failure_frames) == 2
    assert all(
        record.source_module == "quiz_app.observability.middleware" for record in failure_frames
    )
    assert all(record.failure_boundary == "request_finished" for record in failure_frames)
    assert diagnostic_records[-1].event == "request_finished"
    assert diagnostic_records[-1].reason == "unclassified"
    assert diagnostic_records[-1].http_status == 202
    assert all(record.event != "commit_acknowledged" for record in diagnostic_records)


@pytest.mark.asyncio
async def test_unrelated_route_does_not_emit_answer_diagnostics(
    diagnostic_records: list[AnswerDiagnosticRecord],
) -> None:
    async def app(_: Scope, __: Receive, send: Send) -> None:
        assert current_answer_diagnostic.get() is None
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def send(message: Message) -> None:
        assert b"x-quiz-request-id" not in dict(message.get("headers", []))

    await PrometheusMiddleware(app, QuizMetrics())(
        http_scope("/api/v1/participant/session", "GET"), disconnected_receive, send
    )
    assert diagnostic_records == []
