import asyncio
import json
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Event
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlmodel import Session

import quiz_app.automatic_results as automatic_module
import quiz_app.result_worker as worker_module
from quiz_app.automatic_results import (
    AUTOMATIC_AGGREGATE_DOMAIN_FAILURE,
    AUTOMATIC_AGGREGATE_IDLE_SECONDS,
    AUTOMATIC_AGGREGATE_INTEGRITY_FAILURE,
    AUTOMATIC_AGGREGATE_REASON,
    AUTOMATIC_AGGREGATE_RETRY_DELAYS_SECONDS,
    AutomaticAggregateOutcome,
    AutomaticResultAggregator,
    ResultFailureFrame,
)
from quiz_app.domain.errors import DomainError, DomainInvariantError
from quiz_app.domain.quiz_service import QuizDomainService
from quiz_app.domain.types import AggregatedRound, RecoveredRoundResults
from quiz_app.models import OperationAudit, QuizRound
from quiz_app.models.enums import RoundState

EVENT_ID = UUID(int=401)
ROUND_ID = UUID(int=402)
NOW = datetime(2026, 9, 30, tzinfo=UTC)
PRIVATE_VALUE = "private-cookie-answer-value-sentinel"
PRIVATE_UUID = "95000000-0000-4000-8000-000000000099"
PRIVATE_SQL = "SELECT private_statement_sentinel"


@dataclass(frozen=True)
class RecordedFailure:
    message: str
    error_type: str
    stack: list[ResultFailureFrame]
    retry_delay_seconds: float | None


@pytest.fixture
def recorded_failures(monkeypatch: pytest.MonkeyPatch) -> list[RecordedFailure]:
    records: list[RecordedFailure] = []

    def record(
        message: str,
        *,
        error_type: str,
        stack: list[ResultFailureFrame],
        retry_delay_seconds: float | None = None,
    ) -> None:
        records.append(RecordedFailure(message, error_type, stack, retry_delay_seconds))

    def reject_exception_log(*_args: object, **_kwargs: object) -> None:
        pytest.fail("raw exception logging is forbidden for result failures")

    monkeypatch.setattr(automatic_module.logfire, "error", record)
    monkeypatch.setattr(automatic_module.logfire, "exception", reject_exception_log)
    monkeypatch.setattr(automatic_module.logfire, "info", lambda *_args, **_kwargs: None)
    return records


def private_failure(
    *, domain: bool = False, integrity: bool = False
) -> SQLAlchemyError | DomainError:
    if domain:
        return DomainInvariantError(PRIVATE_VALUE)
    exception_type = IntegrityError if integrity else OperationalError
    return exception_type(
        PRIVATE_SQL,
        {"participant_id": PRIVATE_UUID, "cookie": PRIVATE_VALUE},
        ValueError(PRIVATE_VALUE),
    )


def raise_private_failure(error: SQLAlchemyError | DomainError) -> None:
    private_local = PRIVATE_VALUE
    assert private_local
    try:
        raise ValueError(PRIVATE_VALUE)
    except ValueError as cause:
        raise error from cause


def assert_safe_failure(record: RecordedFailure, error: SQLAlchemyError | DomainError) -> None:
    assert record.error_type == type(error).__name__
    assert record.stack
    frames = [frame.model_dump() for frame in record.stack]
    assert all(set(frame) == {"module", "line", "function"} for frame in frames)
    assert all(isinstance(frame.module, str) and frame.line > 0 for frame in record.stack)
    assert any(frame.function == "raise_private_failure" for frame in record.stack)
    serialized = json.dumps({"message": record.message, "stack": frames})
    for private in (PRIVATE_VALUE, PRIVATE_UUID, PRIVATE_SQL, str(EVENT_ID), str(ROUND_ID)):
        assert private not in serialized
    assert "private_local" not in serialized
    assert "raise error from cause" not in serialized


class RecordingStop:
    def __init__(self, max_waits: int) -> None:
        self.delays: list[float] = []
        self.max_waits = max_waits

    def is_set(self) -> bool:
        return len(self.delays) >= self.max_waits

    def wait(self, delay: float) -> bool:
        self.delays.append(delay)
        return self.is_set()


class SequencePublisher:
    def __init__(
        self, outcomes: Sequence[AutomaticAggregateOutcome | SQLAlchemyError | DomainError | None]
    ) -> None:
        self._outcomes = iter(outcomes)

    def process_next(self) -> AutomaticAggregateOutcome | None:
        outcome = next(self._outcomes)
        if isinstance(outcome, SQLAlchemyError | DomainError):
            raise_private_failure(outcome)
        return cast(AutomaticAggregateOutcome | None, outcome)


@pytest.mark.parametrize("domain", [False, True])
def test_worker_logs_only_type_and_source_locations(
    recorded_failures: list[RecordedFailure], domain: bool
) -> None:
    error = private_failure(domain=domain)
    stop = RecordingStop(1)
    worker_module.run_result_worker(
        cast(Event, stop), cast(AutomaticResultAggregator, SequencePublisher([error]))
    )
    assert stop.delays == [AUTOMATIC_AGGREGATE_RETRY_DELAYS_SECONDS[0]]
    assert len(recorded_failures) == 1
    assert_safe_failure(recorded_failures[0], error)


def test_worker_backoff_is_bounded_and_resets_after_success(
    recorded_failures: list[RecordedFailure],
) -> None:
    error = private_failure()
    stop = RecordingStop(8)
    publisher = SequencePublisher(
        [
            *([error] * 6),
            AutomaticAggregateOutcome("aggregated", EVENT_ID, ROUND_ID, 1),
            error,
            None,
        ]
    )
    worker_module.run_result_worker(cast(Event, stop), cast(AutomaticResultAggregator, publisher))
    assert stop.delays == [
        *AUTOMATIC_AGGREGATE_RETRY_DELAYS_SECONDS,
        *([AUTOMATIC_AGGREGATE_RETRY_DELAYS_SECONDS[-1]] * 2),
        AUTOMATIC_AGGREGATE_RETRY_DELAYS_SECONDS[0],
        AUTOMATIC_AGGREGATE_IDLE_SECONDS,
    ]
    assert len(recorded_failures) == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", [False, True])
async def test_existing_async_publisher_logs_only_safe_failure(
    monkeypatch: pytest.MonkeyPatch, recorded_failures: list[RecordedFailure], domain: bool
) -> None:
    error = private_failure(domain=domain)
    publisher = AutomaticResultAggregator(
        cast(Engine, object()),
        cast(QuizDomainService, object()),
        cast(automatic_module.ResultInvalidationPublisher, object()),
    )
    stop = asyncio.Event()
    waits: list[float] = []

    def fail() -> None:
        raise_private_failure(error)

    async def wait(stop_requested: asyncio.Event, delay: float) -> None:
        waits.append(delay)
        stop_requested.set()

    monkeypatch.setattr(publisher, "process_next", fail)
    monkeypatch.setattr(publisher, "_wait", wait)
    await publisher.run(stop)
    assert waits == [AUTOMATIC_AGGREGATE_RETRY_DELAYS_SECONDS[-1 if domain else 0]]
    assert_safe_failure(recorded_failures[0], error)


class CandidateSession:
    def __init__(self) -> None:
        self.audits: list[OperationAudit] = []

    def __enter__(self) -> "CandidateSession":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def begin(self) -> nullcontext[None]:
        return nullcontext()

    def begin_nested(self) -> nullcontext[None]:
        return nullcontext()

    def scalar(self, _statement: object) -> QuizRound:
        return QuizRound(id=ROUND_ID, event_id=EVENT_ID, question_id=UUID(int=403), version=1)

    def add(self, audit: OperationAudit) -> None:
        self.audits.append(audit)


@pytest.mark.parametrize("domain", [False, True])
def test_permanent_failure_keeps_audit_reason_without_logging_identity(
    monkeypatch: pytest.MonkeyPatch, recorded_failures: list[RecordedFailure], domain: bool
) -> None:
    error = private_failure(domain=domain, integrity=True)
    session = CandidateSession()
    service = QuizDomainService(cast(Engine, object()))
    reasons: list[str] = []

    def fail(_session: Session, _round: UUID, _version: int) -> AggregatedRound:
        raise_private_failure(error)

    def mark(
        _session: Session, _round: UUID, _version: int, failure_reason: str
    ) -> RecoveredRoundResults:
        reasons.append(failure_reason)
        return RecoveredRoundResults(EVENT_ID, 2, ROUND_ID, 2, RoundState.VOID_CANDIDATE)

    monkeypatch.setattr(service, "aggregate_round_results_in_transaction", fail)
    monkeypatch.setattr(service, "mark_round_results_void_candidate_in_transaction", mark)
    publisher = AutomaticResultAggregator(
        cast(Engine, object()),
        service,
        cast(
            automatic_module.ResultInvalidationPublisher,
            SimpleNamespace(notify=lambda *_args, cause: cause == "automatic_result"),
        ),
        session_factory=lambda: cast(Session, session),
    )
    monkeypatch.setattr(publisher, "_database_time", lambda _session: NOW)
    outcome = publisher.process_next()
    expected = (
        AUTOMATIC_AGGREGATE_DOMAIN_FAILURE if domain else AUTOMATIC_AGGREGATE_INTEGRITY_FAILURE
    )
    assert outcome is not None and outcome.status == "void_candidate"
    assert reasons == [expected]
    assert session.audits[0].reason == f"{AUTOMATIC_AGGREGATE_REASON}:{expected}"
    assert_safe_failure(recorded_failures[0], error)


@pytest.mark.parametrize("close_fails", [False, True])
def test_worker_main_disposes_database_even_when_redis_close_fails(
    monkeypatch: pytest.MonkeyPatch, close_fails: bool
) -> None:
    resources: list[str] = []

    def close() -> None:
        resources.append("redis_closed")
        if close_fails:
            raise OSError("simulated_redis_close_failure")

    engine = SimpleNamespace(dispose=lambda: resources.append("database_disposed"))
    monkeypatch.setattr(
        worker_module,
        "get_settings",
        lambda: SimpleNamespace(logfire_send_to_logfire=False, database_result_worker_pool_size=2),
    )
    monkeypatch.setattr(worker_module.logfire, "configure", lambda **_kwargs: None)
    monkeypatch.setattr(worker_module.logfire, "info", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker_module.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(worker_module, "create_sync_engine", lambda **_kwargs: engine)
    monkeypatch.setattr(
        worker_module, "get_sync_redis_client", lambda: SimpleNamespace(close=close)
    )
    monkeypatch.setattr(worker_module, "run_result_worker", lambda *_args: None)
    expectation = (
        pytest.raises(OSError, match="simulated_redis_close_failure")
        if close_fails
        else nullcontext()
    )
    with expectation:
        worker_module.main()
    assert resources == ["redis_closed", "database_disposed"]


def test_worker_main_disposes_database_when_redis_initialization_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources: list[str] = []

    def unavailable_redis() -> None:
        raise OSError("simulated_redis_initialization_failure")

    monkeypatch.setattr(
        worker_module,
        "get_settings",
        lambda: SimpleNamespace(logfire_send_to_logfire=False, database_result_worker_pool_size=2),
    )
    monkeypatch.setattr(worker_module.logfire, "configure", lambda **_kwargs: None)
    monkeypatch.setattr(worker_module.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(
        worker_module,
        "create_sync_engine",
        lambda **_kwargs: SimpleNamespace(dispose=lambda: resources.append("database_disposed")),
    )
    monkeypatch.setattr(worker_module, "get_sync_redis_client", unavailable_redis)

    with pytest.raises(OSError, match="simulated_redis_initialization_failure"):
        worker_module.main()
    assert resources == ["database_disposed"]
