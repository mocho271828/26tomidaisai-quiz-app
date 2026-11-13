import asyncio
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI, Request
from sqlalchemy import Engine, create_engine, event, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel

import quiz_app.health as health_module
import quiz_app.main as main_module
from quiz_app.automatic_results import (
    AUTOMATIC_AGGREGATE_DOMAIN_FAILURE,
    AUTOMATIC_AGGREGATE_OPERATION,
    AUTOMATIC_AGGREGATE_REASON,
    AUTOMATIC_AGGREGATE_RETRY_DELAYS_SECONDS,
    AutomaticResultAggregator,
    automatic_aggregate_candidate_statement,
)
from quiz_app.contracts import ReadinessHealthResponse
from quiz_app.domain.errors import DomainInvariantError, InvalidStateTransitionError
from quiz_app.domain.quiz_service import QuizDomainService
from quiz_app.domain.types import AggregatedRound
from quiz_app.models import (
    Answer,
    Event,
    IdempotencyRecord,
    OperationAudit,
    Participant,
    Question,
    QuestionOption,
    QuizRound,
    RankSnapshot,
    RoundOptionStat,
    ScoreLedger,
    ScoringPolicy,
)
from quiz_app.models.enums import (
    AdminRole,
    EventState,
    RankingVisibility,
    RoundResultPublication,
    RoundState,
)
from quiz_app.operator.auth_service import OperatorPrincipal
from quiz_app.operator.control_contracts import OperatorMutationRequest
from quiz_app.operator.mutations import OperatorMutationService
from quiz_app.quiz_configuration import (
    configured_scoring_policy,
    get_quiz_settings,
    initial_event_configuration,
)
from quiz_app.realtime.contracts import RealtimeAudience

EVENT_ID = UUID("76000000-0000-4000-8000-000000000001")
ROUND_ID = UUID("76000000-0000-4000-8000-000000000002")
QUESTION_ID = UUID("76000000-0000-4000-8000-000000000003")
OPTION_A_ID = UUID("76000000-0000-4000-8000-000000000004")
OPTION_B_ID = UUID("76000000-0000-4000-8000-000000000005")
NOW = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
DATABASE_NOW = NOW.replace(tzinfo=None)


class RecordingPublisher:
    def __init__(self, engine: Engine, *, notify_result: bool = True) -> None:
        self._engine = engine
        self._notify_result = notify_result
        self.notifications: list[tuple[UUID, tuple[RealtimeAudience, ...], RoundState]] = []

    def notify(
        self,
        event_id: UUID,
        audiences: Sequence[RealtimeAudience],
        *,
        cause: str,
    ) -> bool:
        assert cause == "automatic_result"
        with Session(self._engine) as session:
            round_ = session.get(QuizRound, ROUND_ID)
            assert round_ is not None
            self.notifications.append((event_id, tuple(audiences), round_.state))
        return self._notify_result


class FailingAfterWriteDomainService(QuizDomainService):
    def aggregate_round_results_in_transaction(
        self,
        session: Session,
        round_id: UUID,
        expected_version: int,
    ) -> AggregatedRound:
        session.add(
            OperationAudit(
                event_id=EVENT_ID,
                round_id=round_id,
                operation="must_rollback",
                target_type="quiz_round",
                target_id=str(round_id),
                reason="savepoint fixture",
                outcome="failed",
                occurred_at=NOW,
            )
        )
        session.flush()
        raise DomainInvariantError(f"invalid_fixture_version_{expected_version}")


class StaleCandidateDomainService(QuizDomainService):
    def aggregate_round_results_in_transaction(
        self,
        session: Session,
        round_id: UUID,
        expected_version: int,
    ) -> AggregatedRound:
        del session, round_id, expected_version
        raise InvalidStateTransitionError("event_is_not_in_progress")


@dataclass(frozen=True, slots=True)
class AutomaticResultFixture:
    engine: Engine
    publisher: RecordingPublisher
    worker: AutomaticResultAggregator


@pytest.fixture
def automatic_result_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[AutomaticResultFixture]:
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def register_clock(database_connection: object, _: object) -> None:
        assert isinstance(database_connection, sqlite3.Connection)
        database_connection.create_function("pg_advisory_xact_lock", 1, lambda _key: 0)
        database_connection.create_function(
            "clock_timestamp",
            0,
            lambda: NOW.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f"),
        )

    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        # SQLiteはPostgreSQL専用partial条件を無視するため、fixture内だけindexを外す。
        connection.exec_driver_sql("DROP INDEX uq_question_options_one_correct")
    with Session(engine) as session, session.begin():
        settings = get_quiz_settings()
        session.add(configured_scoring_policy(settings))
        session.add(initial_event_configuration(EVENT_ID, settings))
        session.add(
            Event(
                id=EVENT_ID,
                slug="automatic-results",
                title="自動集計test",
                state=EventState.IN_PROGRESS,
                ranking_visibility=RankingVisibility.HIDDEN_ALL,
                scoring_policy_version=settings.scoring.version,
                display_name_policy_version="name-v1",
                version=3,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            Question(
                id=QUESTION_ID,
                event_id=EVENT_ID,
                version=1,
                prompt="問題",
                display_order=1,
                created_at=NOW,
            )
        )
        session.add_all(
            [
                QuestionOption(
                    id=OPTION_A_ID,
                    question_id=QUESTION_ID,
                    label="A",
                    content="正解",
                    display_order=0,
                    is_correct=True,
                ),
                QuestionOption(
                    id=OPTION_B_ID,
                    question_id=QUESTION_ID,
                    label="B",
                    content="不正解",
                    display_order=1,
                    is_correct=False,
                ),
            ]
        )
        session.add(
            QuizRound(
                id=ROUND_ID,
                event_id=EVENT_ID,
                question_id=QUESTION_ID,
                version=4,
                state=RoundState.SCHEDULED,
                result_publication=RoundResultPublication.WITHHELD,
                open_at=NOW - timedelta(milliseconds=14_000),
                standard_close_at=NOW - timedelta(milliseconds=4_000),
                created_at=NOW,
            )
        )

    domain_service = QuizDomainService(engine)
    monkeypatch.setattr(domain_service, "_database_time", lambda _session: DATABASE_NOW)
    publisher = RecordingPublisher(engine)
    worker = AutomaticResultAggregator(engine, domain_service, publisher)
    monkeypatch.setattr(worker, "_database_time", lambda _session: DATABASE_NOW)
    yield AutomaticResultFixture(engine, publisher, worker)
    engine.dispose()


def test_candidate_query_uses_postgresql_skip_locked_and_database_clock() -> None:
    sql = str(
        automatic_aggregate_candidate_statement().compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "clock_timestamp()" in sql
    assert (
        "JOIN scoring_policies ON scoring_policies.version = events.scoring_policy_version" in sql
    )
    assert "quiz_rounds.standard_close_at + scoring_policies.arrival_grace_ms *" in sql
    assert "make_interval(secs=>0.001)" in sql
    assert "scoring_policies.version = 'scoring-2026-10-15-v2'" in sql
    assert "scoring_policies.timing_basis = 'client_monotonic_v1'" in sql
    assert "FOR UPDATE OF quiz_rounds SKIP LOCKED" in sql
    assert "events.state = 'InProgress'" in sql
    assert "quiz_rounds.result_publication = 'Withheld'" in sql
    assert "quiz_rounds.state IN ('Scheduled', 'Open', " in sql
    assert "'AwaitingValidation')" in sql
    assert "ORDER BY quiz_rounds.standard_close_at, quiz_rounds.id" in sql
    assert "LIMIT 1" in sql


def test_process_next_aggregates_exactly_once_then_notifies_after_commit(
    automatic_result_fixture: AutomaticResultFixture,
) -> None:
    fixture = automatic_result_fixture

    first = fixture.worker.process_next()
    second = fixture.worker.process_next()

    assert first is not None
    assert first.status == "aggregated"
    assert second is None
    assert fixture.publisher.notifications == [
        (
            EVENT_ID,
            ("operator",),
            RoundState.READY_TO_REVEAL,
        )
    ]
    with Session(fixture.engine) as session:
        round_ = session.get(QuizRound, ROUND_ID)
        assert round_ is not None
        assert round_.state is RoundState.READY_TO_REVEAL
        assert round_.result_publication is RoundResultPublication.WITHHELD
        assert session.scalar(select(func.count()).select_from(RankSnapshot)) == 1
        assert session.scalar(select(func.count()).select_from(RoundOptionStat)) == 2
        audits = session.scalars(select(OperationAudit)).all()
    assert len(audits) == 1
    assert audits[0].operation == AUTOMATIC_AGGREGATE_OPERATION
    assert audits[0].reason == AUTOMATIC_AGGREGATE_REASON
    assert audits[0].outcome == "succeeded"
    assert audits[0].account_id is None
    assert audits[0].idempotency_key is None


def test_invalidation_failure_does_not_roll_back_aggregated_results(
    automatic_result_fixture: AutomaticResultFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = automatic_result_fixture
    unavailable_publisher = RecordingPublisher(fixture.engine, notify_result=False)
    domain_service = QuizDomainService(fixture.engine)
    worker = AutomaticResultAggregator(
        fixture.engine,
        domain_service,
        unavailable_publisher,
    )
    monkeypatch.setattr(domain_service, "_database_time", lambda _session: DATABASE_NOW)
    monkeypatch.setattr(worker, "_database_time", lambda _session: DATABASE_NOW)

    outcome = worker.process_next()

    assert outcome is not None
    assert outcome.status == "aggregated"
    assert unavailable_publisher.notifications[-1][2] is RoundState.READY_TO_REVEAL
    with Session(fixture.engine) as session:
        round_ = session.get(QuizRound, ROUND_ID)
        assert round_ is not None
        assert round_.state is RoundState.READY_TO_REVEAL
        assert round_.result_publication is RoundResultPublication.WITHHELD
        assert session.scalar(select(func.count()).select_from(RankSnapshot)) == 1


def test_permanent_failure_rolls_back_partial_writes_and_marks_void_candidate(
    automatic_result_fixture: AutomaticResultFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = automatic_result_fixture
    failing_domain = FailingAfterWriteDomainService(fixture.engine)
    monkeypatch.setattr(failing_domain, "_database_time", lambda _session: DATABASE_NOW)
    worker = AutomaticResultAggregator(fixture.engine, failing_domain, fixture.publisher)
    monkeypatch.setattr(worker, "_database_time", lambda _session: DATABASE_NOW)

    outcome = worker.process_next()

    assert outcome is not None
    assert outcome.status == "void_candidate"
    with Session(fixture.engine) as session:
        round_ = session.get(QuizRound, ROUND_ID)
        assert round_ is not None
        audits = session.scalars(select(OperationAudit)).all()
    assert round_.state is RoundState.VOID_CANDIDATE
    assert round_.result_publication is RoundResultPublication.WITHHELD
    assert round_.void_reason == (f"{AUTOMATIC_AGGREGATE_DOMAIN_FAILURE}:invalid_fixture_version_4")
    assert len(audits) == 1
    assert audits[0].operation == AUTOMATIC_AGGREGATE_OPERATION
    assert audits[0].outcome == "failed"
    assert audits[0].reason == (
        f"{AUTOMATIC_AGGREGATE_REASON}:{AUTOMATIC_AGGREGATE_DOMAIN_FAILURE}:invalid_fixture_version_4"
    )
    assert fixture.publisher.notifications[-1][2] is RoundState.VOID_CANDIDATE


def test_stale_candidate_conflict_does_not_mark_round_void(
    automatic_result_fixture: AutomaticResultFixture,
) -> None:
    fixture = automatic_result_fixture
    worker = AutomaticResultAggregator(
        fixture.engine,
        StaleCandidateDomainService(fixture.engine),
        fixture.publisher,
    )

    with pytest.raises(InvalidStateTransitionError, match="event_is_not_in_progress"):
        worker.process_next()

    with Session(fixture.engine) as session:
        round_ = session.get(QuizRound, ROUND_ID)
        assert round_ is not None
        audit_count = session.scalar(select(func.count()).select_from(OperationAudit))
    assert round_.state is RoundState.SCHEDULED
    assert round_.result_publication is RoundResultPublication.WITHHELD
    assert round_.void_reason is None
    assert audit_count == 0
    assert fixture.publisher.notifications == []


@pytest.mark.parametrize("mismatch", ["version", "basis"])
def test_worker_skips_unsupported_policy_without_mutating_or_voiding_round(
    automatic_result_fixture: AutomaticResultFixture, mismatch: str
) -> None:
    fixture = automatic_result_fixture
    with Session(fixture.engine) as session, session.begin():
        policy = session.get(ScoringPolicy, get_quiz_settings().scoring.version)
        event_ = session.get(Event, EVENT_ID)
        assert policy is not None and event_ is not None
        if mismatch == "version":
            old_policy = policy.model_copy(update={"version": "previous-scoring-v1"})
            session.add(old_policy)
            event_.scoring_policy_version = old_policy.version
        else:
            policy.timing_basis = "server_received_v1"
            policy.arrival_grace_ms = 0
            policy.clock_tolerance_ms = 0
            policy.client_ahead_tolerance_ms = 0
    assert fixture.worker.process_next() is None
    with Session(fixture.engine) as session:
        round_ = session.get(QuizRound, ROUND_ID)
        assert round_ is not None
        assert round_.state is RoundState.SCHEDULED
        assert round_.version == 4
        assert round_.result_publication is RoundResultPublication.WITHHELD
        assert round_.void_reason is None
        assert session.scalar(select(func.count()).select_from(OperationAudit)) == 0
        assert session.scalar(select(func.count()).select_from(RankSnapshot)) == 0
    assert fixture.publisher.notifications == []


def test_retry_after_void_candidate_reaggregates_from_clean_savepoint(
    automatic_result_fixture: AutomaticResultFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = automatic_result_fixture
    failing_domain = FailingAfterWriteDomainService(fixture.engine)
    monkeypatch.setattr(failing_domain, "_database_time", lambda _session: DATABASE_NOW)
    failing_worker = AutomaticResultAggregator(
        fixture.engine,
        failing_domain,
        fixture.publisher,
    )
    monkeypatch.setattr(failing_worker, "_database_time", lambda _session: DATABASE_NOW)
    failed = failing_worker.process_next()
    assert failed is not None

    recovery_service = QuizDomainService(fixture.engine)
    monkeypatch.setattr(recovery_service, "_database_time", lambda _session: DATABASE_NOW)
    with Session(fixture.engine) as session, session.begin():
        recovery_service.retry_round_results_in_transaction(
            session,
            ROUND_ID,
            expected_version=failed.round_version,
        )

    published = fixture.worker.process_next()

    assert published is not None
    assert published.status == "aggregated"
    with Session(fixture.engine) as session:
        round_ = session.get(QuizRound, ROUND_ID)
        assert round_ is not None
        assert round_.state is RoundState.READY_TO_REVEAL
        assert round_.result_publication is RoundResultPublication.WITHHELD
        assert session.scalar(select(func.count()).select_from(RankSnapshot)) == 1
        assert session.scalar(select(func.count()).select_from(RoundOptionStat)) == 2


@pytest.mark.asyncio
async def test_database_retry_backoff_is_bounded(
    automatic_result_fixture: AutomaticResultFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = automatic_result_fixture.worker
    stop_requested = asyncio.Event()
    delays: list[float] = []

    def fail_database_operation() -> None:
        raise OperationalError("statement", {}, sqlite3.OperationalError("temporary"))

    async def record_wait(_: asyncio.Event, delay_seconds: float) -> None:
        delays.append(delay_seconds)
        if len(delays) == 6:
            stop_requested.set()

    monkeypatch.setattr(worker, "process_next", fail_database_operation)
    monkeypatch.setattr(worker, "_wait", record_wait)

    await worker.run(stop_requested)

    assert delays == [
        *AUTOMATIC_AGGREGATE_RETRY_DELAYS_SECONDS,
        AUTOMATIC_AGGREGATE_RETRY_DELAYS_SECONDS[-1],
        AUTOMATIC_AGGREGATE_RETRY_DELAYS_SECONDS[-1],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_async_close", [False, True])
async def test_lifespan_starts_and_stops_automatic_result_worker(
    monkeypatch: pytest.MonkeyPatch,
    fail_async_close: bool,
) -> None:
    started = asyncio.Event()
    stopped = asyncio.Event()
    registered_pools: list[object] = []
    resources: list[str] = []

    class StubFactory:
        def __init__(self, name: str = "factory") -> None:
            self.name = name

        async def start(self) -> None:
            resources.append(f"{self.name}_started")

        async def close(self) -> None:
            resources.append(f"{self.name}_closed")

    @lru_cache
    def redis_client() -> SimpleNamespace:
        return SimpleNamespace(close=lambda: resources.append("redis_closed"))

    async def close_async_redis() -> None:
        resources.append("async_redis_closed")
        if fail_async_close:
            raise OSError("simulated_close_failure")

    @lru_cache
    def async_redis_client() -> SimpleNamespace:
        return SimpleNamespace(aclose=close_async_redis)

    async def dispose_async_engine() -> None:
        resources.append("async_engine_closed")

    @lru_cache
    def async_engine() -> SimpleNamespace:
        return SimpleNamespace(
            sync_engine=SimpleNamespace(pool=object()), dispose=dispose_async_engine
        )

    class StubWorker:
        async def run(self, stop_requested: asyncio.Event) -> None:
            started.set()
            await stop_requested.wait()
            stopped.set()

    fake_engine = SimpleNamespace(
        pool=object(), dispose=lambda: resources.append("sync_engine_closed")
    )
    fake_application = SimpleNamespace(
        state=SimpleNamespace(
            quiz_metrics=SimpleNamespace(
                register_database_pool=lambda pool, **_kwargs: registered_pools.append(pool)
            ),
            realtime_subscription_factory=StubFactory(),
            realtime_state_factory=StubFactory("state_factory"),
        )
    )
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            logfire_send_to_logfire=False, redis_operation_timeout_seconds=0.01
        ),
    )
    monkeypatch.setattr(main_module.logfire, "configure", lambda **_kwargs: None)
    monkeypatch.setattr(main_module, "get_engine", lambda: fake_engine)
    monkeypatch.setattr(
        main_module,
        "enforce_production_runtime_isolation",
        lambda _settings, _engine: None,
    )
    monkeypatch.setattr(main_module, "get_sync_redis_client", redis_client)
    monkeypatch.setattr(main_module, "get_async_redis_client", async_redis_client)
    monkeypatch.setattr(main_module, "get_async_engine", async_engine)
    monkeypatch.setattr(
        main_module,
        "AutomaticResultProcessRunner",
        lambda *_args: StubWorker(),
    )

    expectation = (
        pytest.raises(OSError, match="simulated_close_failure")
        if fail_async_close
        else nullcontext()
    )
    with expectation:
        async with main_module.lifespan(cast(FastAPI, fake_application)):
            await asyncio.wait_for(started.wait(), timeout=1)
            assert fake_application.state.automatic_result_worker_alive is True

    await asyncio.sleep(0)
    assert stopped.is_set()
    assert fake_application.state.automatic_result_worker_alive is False
    assert fake_application.state.automatic_result_worker_error is None
    assert len(registered_pools) == 2
    assert registered_pools[0] is fake_engine.pool
    assert resources == [
        "factory_started",
        "state_factory_started",
        "state_factory_closed",
        "factory_closed",
        "async_redis_closed",
        "async_engine_closed",
        "redis_closed",
        "sync_engine_closed",
    ]
    assert redis_client.cache_info().currsize == 0
    assert async_redis_client.cache_info().currsize == 0
    assert async_engine.cache_info().currsize == 0
    assert fake_application.state.answer_notifications._task is None


@pytest.mark.asyncio
async def test_supervisor_recovers_an_unexpected_worker_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restarted = asyncio.Event()
    stop_requested = asyncio.Event()
    logged_states: list[tuple[bool, str, float, BaseException | bool]] = []

    class CrashOnceWorker:
        def __init__(self) -> None:
            self.run_count = 0

        async def run(self, stop: asyncio.Event) -> None:
            self.run_count += 1
            if self.run_count == 1:
                raise RuntimeError("unexpected worker failure")
            restarted.set()
            await stop.wait()

    application = SimpleNamespace(
        state=SimpleNamespace(
            automatic_result_worker_alive=False,
            automatic_result_worker_error=None,
        )
    )
    worker = CrashOnceWorker()
    monkeypatch.setattr(
        main_module,
        "AUTOMATIC_RESULT_WORKER_RESTART_DELAYS_SECONDS",
        (0.001,),
    )

    def record_error(
        _message: str,
        *,
        error_type: str,
        restart_delay_seconds: float,
        _exc_info: BaseException | bool,
    ) -> None:
        logged_states.append(
            (
                application.state.automatic_result_worker_alive,
                error_type,
                restart_delay_seconds,
                _exc_info,
            )
        )

    monkeypatch.setattr(main_module.logfire, "error", record_error)
    supervisor = asyncio.create_task(
        main_module._supervise_automatic_result_worker(
            cast(FastAPI, application),
            cast(AutomaticResultAggregator, worker),
            stop_requested,
        )
    )

    await asyncio.wait_for(restarted.wait(), timeout=1)

    assert worker.run_count == 2
    assert application.state.automatic_result_worker_alive is True
    assert application.state.automatic_result_worker_error is None
    assert len(logged_states) == 1
    assert logged_states[0][:3] == (False, "RuntimeError", 0.001)
    assert isinstance(logged_states[0][3], RuntimeError)

    stop_requested.set()
    await asyncio.wait_for(supervisor, timeout=1)
    assert application.state.automatic_result_worker_alive is False


@pytest.mark.asyncio
async def test_supervisor_does_not_restart_an_unreaped_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed = asyncio.Event()
    stop_requested = asyncio.Event()
    run_count = 0

    class UnreapedWorker:
        async def run(self, _stop: asyncio.Event) -> None:
            nonlocal run_count
            run_count += 1
            raise main_module.ResultWorkerCleanupError("result_worker_cleanup_timeout")

    def record_failure(_message: str, *, _exc_info: BaseException) -> None:
        assert isinstance(_exc_info, main_module.ResultWorkerCleanupError)
        failed.set()

    application = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr(main_module.logfire, "error", record_failure)
    supervisor = asyncio.create_task(
        main_module._supervise_automatic_result_worker(
            cast(FastAPI, application), UnreapedWorker(), stop_requested
        )
    )
    await asyncio.wait_for(failed.wait(), timeout=1)
    await asyncio.sleep(0)
    assert run_count == 1
    assert application.state.automatic_result_worker_alive is False
    assert application.state.automatic_result_worker_error == "ResultWorkerCleanupError"
    assert not supervisor.done()
    stop_requested.set()
    await asyncio.wait_for(supervisor, timeout=1)
    assert run_count == 1


def test_readiness_fails_closed_after_worker_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = ReadinessHealthResponse(
        status="ready",
        database="ok",
        migration="current",
        redis="ok",
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(automatic_result_worker_alive=False),
        )
    )
    monkeypatch.setattr(
        health_module,
        "get_redis_health_probe",
        lambda _request: SimpleNamespace(status=lambda: "ok"),
    )
    monkeypatch.setattr(health_module, "get_engine", lambda: object())
    monkeypatch.setattr(
        health_module,
        "inspect_readiness",
        lambda _engine, _redis_status: ready,
    )

    readiness = health_module.check_readiness(cast(Request, request))

    assert readiness.status == "not_ready"
    assert readiness.database == "ok"
    assert readiness.migration == "current"
    assert readiness.redis == "ok"


@pytest.mark.parametrize(
    "state", [RoundState.SCHEDULED, RoundState.AWAITING_VALIDATION, RoundState.VOID_CANDIDATE]
)
def test_manual_publish_rejects_results_before_successful_aggregation(
    automatic_result_fixture: AutomaticResultFixture, state: RoundState
) -> None:
    fixture = automatic_result_fixture
    domain = QuizDomainService(fixture.engine)
    with Session(fixture.engine) as session, session.begin():
        round_ = session.get(QuizRound, ROUND_ID)
        assert round_ is not None
        round_.state = state
    with Session(fixture.engine) as session, session.begin():
        with pytest.raises(InvalidStateTransitionError, match="not_ready_to_reveal"):
            domain.publish_round_results_in_transaction(session, ROUND_ID, 4)
    with Session(fixture.engine) as session:
        round_ = session.get(QuizRound, ROUND_ID)
        assert round_ is not None
        assert round_.result_publication is RoundResultPublication.WITHHELD
        assert round_.revealed_at is None
        assert session.scalar(select(func.count()).select_from(RankSnapshot)) == 0


def test_manual_publish_is_audited_idempotent_and_does_not_recompute_results(
    automatic_result_fixture: AutomaticResultFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = automatic_result_fixture
    participant_id = UUID(int=901)
    with Session(fixture.engine) as session, session.begin():
        session.add(Participant(id=participant_id, event_id=EVENT_ID, public_label="AAA"))
        session.add(
            Answer(
                participant_id=participant_id,
                round_id=ROUND_ID,
                option_id=OPTION_A_ID,
                client_answer_id=UUID(int=902),
                client_elapsed_ms=2500,
                received_at=NOW - timedelta(milliseconds=9500),
            )
        )
    aggregated = fixture.worker.process_next()
    assert aggregated is not None
    with Session(fixture.engine) as session:
        before_ledger = session.scalar(select(ScoreLedger))
        before_snapshot = session.scalar(select(RankSnapshot))
        assert before_ledger is not None and before_snapshot is not None
        before_ledger_values = before_ledger.model_dump()
        before_snapshot_id = before_snapshot.id
        round_ = session.get(QuizRound, ROUND_ID)
        assert round_ is not None and round_.revealed_at is None

    domain = QuizDomainService(fixture.engine)
    mutations = OperatorMutationService(fixture.engine, domain)
    monkeypatch.setattr(domain, "_database_time", lambda _session: DATABASE_NOW)
    monkeypatch.setattr(mutations, "_database_time", lambda _session: DATABASE_NOW)

    def reject_recomputation(*_args: object, **_kwargs: object) -> None:
        pytest.fail("manual publication must only expose saved results")

    monkeypatch.setattr(domain, "aggregate_round_results_in_transaction", reject_recomputation)
    principal = OperatorPrincipal(
        account_id=UUID(int=903),
        session_id=UUID(int=904),
        username="operator",
        role=AdminRole.OPERATOR,
        csrf_hash="fixture",
    )
    request = OperatorMutationRequest(
        expected_version=aggregated.round_version,
        idempotency_key=UUID(int=905),
    )
    first = mutations.publish_round_results(principal, ROUND_ID, request)
    replay = mutations.publish_round_results(principal, ROUND_ID, request)
    assert first.operation == "publish_round_results"
    assert first.round_state is RoundState.REVEAL
    assert first.valid_answer_count == 1
    assert not first.is_replay and replay.is_replay
    assert first.round_version == replay.round_version
    with Session(fixture.engine) as session:
        round_ = session.get(QuizRound, ROUND_ID)
        assert round_ is not None
        assert round_.result_publication is RoundResultPublication.PUBLISHED
        assert round_.revealed_at == DATABASE_NOW
        ledger = session.scalar(select(ScoreLedger))
        snapshot = session.scalar(select(RankSnapshot))
        assert ledger is not None and ledger.model_dump() == before_ledger_values
        assert snapshot is not None and snapshot.id == before_snapshot_id
        assert session.scalar(select(func.count()).select_from(ScoreLedger)) == 1
        assert session.scalar(select(func.count()).select_from(RankSnapshot)) == 1
        assert session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 1
        audits = session.scalars(
            select(OperationAudit).where(OperationAudit.operation == "publish_round_results")
        ).all()
        assert len(audits) == 1
        assert audits[0].account_id == principal.account_id
        assert audits[0].before_state == "ReadyToReveal"
        assert audits[0].after_state == "Reveal"
        assert audits[0].reason == ""
    assert fixture.worker.process_next() is None
