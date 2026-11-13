import asyncio
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from threading import Event as ThreadEvent
from time import monotonic
from types import TracebackType
from typing import Concatenate, ParamSpec, Self, TypeVar, cast
from uuid import UUID

import pytest
from anyio.to_thread import current_default_thread_limiter
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.util.concurrency import await_only
from sqlmodel import Session
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.concurrency import run_in_threadpool

import quiz_app.audience.async_service as service_module
from quiz_app.audience.async_service import AudienceAsyncService
from quiz_app.audience.auth_service import (
    ParticipantAuthService,
    ParticipantPrincipal,
    VerifiedParticipantAuthentication,
)
from quiz_app.audience.contracts import (
    AcceptedAnswerStatusResponse,
    AnswerStatusResponse,
    AudienceEventResponse,
    ParticipantAnswerResponse,
    ParticipantCommonStateResponse,
    ParticipantIdentityResponse,
    ParticipantStateResponse,
    PersonalResultDelivery,
    SubmitAnswerRequest,
    UnsavedAnswerStatusResponse,
)
from quiz_app.audience.errors import AudienceServiceUnavailableError, ParticipantSessionInvalidError
from quiz_app.audience.projection import AudienceProjectionService
from quiz_app.domain.errors import (
    AnswerContentConflictError,
    AnswerWindowClosedError,
    ResourceNotFoundError,
    VersionConflictError,
)
from quiz_app.domain.quiz_service import QuizDomainService
from quiz_app.domain.types import AcceptedAnswer, SubmitAnswerCommand
from quiz_app.models.enums import EventState
from quiz_app.observability.answer_diagnostics import (
    AnswerDiagnostic,
    AnswerDiagnosticRecord,
    current_answer_diagnostic,
    record_answer_event,
)
from quiz_app.observability.request_timing import (
    RequestTiming,
    current_request_timing,
    mark_handler_dispatch,
    observe_handler,
)
from quiz_app.quiz_configuration import ResultVisibility
from quiz_app.realtime.state_cache import CommonStateRefreshRequired
from quiz_app.single_event import SingleEventConfigurationError

PARAMETERS = ParamSpec("PARAMETERS")
RESULT = TypeVar("RESULT")
EVENT_ID = UUID(int=1)
PARTICIPANT_ID = UUID(int=2)
ROUND_ID = UUID(int=3)
OPTION_ID = UUID(int=4)
CLIENT_ANSWER_ID = UUID(int=5)
RECEIVED_AT = datetime(2026, 9, 30, tzinfo=UTC)
SESSION_VALUE_FIXTURE = "session-fixture"


def principal(event_id: UUID = EVENT_ID) -> ParticipantPrincipal:
    return ParticipantPrincipal(
        participant_id=PARTICIPANT_ID,
        session_id=UUID(int=6),
        event_id=event_id,
        public_label="ABC",
        is_ranking_eligible=True,
    )


def accepted(command: SubmitAnswerCommand, *, replay: bool = False) -> AcceptedAnswer:
    return AcceptedAnswer(
        timing_basis="server_received_v1",
        client_elapsed_ms=None,
        answer_id=UUID(int=7),
        round_id=command.round_id,
        option_id=command.option_id,
        client_answer_id=command.client_answer_id,
        received_at=RECEIVED_AT,
        is_replay=replay,
    )


class TraceTransaction:
    def __init__(self, session: "TraceAsyncSession") -> None:
        self._session = session

    async def __aenter__(self) -> Self:
        self._session.events.append(f"begin:{self._session.number}")
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        if exception_type is not None:
            self._session.events.append(f"rollback:{self._session.number}")
            return
        if self._session.commit_gate is not None:
            await self._session.commit_gate.wait()
        self._session.events.append(f"commit:{self._session.number}")
        if self._session.commit_error is not None:
            raise self._session.commit_error


class TraceAsyncSession:
    def __init__(self, number: int, events: list[str]) -> None:
        self.number = number
        self.events = events
        self._async_session = AsyncSession()
        self.sync_session = cast(Session, self._async_session.sync_session)
        self.commit_error: BaseException | None = None
        self.commit_gate: asyncio.Event | None = None

    async def __aenter__(self) -> Self:
        self.events.append(f"open:{self.number}")
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._async_session.close()
        self.events.append(f"close:{self.number}")

    def begin(self) -> TraceTransaction:
        return TraceTransaction(self)

    async def run_sync(
        self,
        function: Callable[Concatenate[Session, PARAMETERS], RESULT],
        *args: PARAMETERS.args,
        **kwargs: PARAMETERS.kwargs,
    ) -> RESULT:
        return await self._async_session.run_sync(function, *args, **kwargs)


class TraceSessionFactory:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.sessions: list[TraceAsyncSession] = []
        self.commit_error: BaseException | None = None
        self.commit_gate: asyncio.Event | None = None

    def __call__(self) -> TraceAsyncSession:
        session = TraceAsyncSession(len(self.sessions), self.events)
        if session.number == 0:
            session.commit_error = self.commit_error
            session.commit_gate = self.commit_gate
        self.sessions.append(session)
        return session


class AuthenticationFixture:
    def __init__(self) -> None:
        self.sessions: list[Session] = []
        self.event_id = EVENT_ID
        self.io_gate: asyncio.Event | None = None
        self.realtime_sessions: list[Session] = []

    def authenticate_realtime_in_session(
        self, session: Session, token: str
    ) -> VerifiedParticipantAuthentication:
        self.realtime_sessions.append(session)
        if token != SESSION_VALUE_FIXTURE:
            raise ParticipantSessionInvalidError("participant_session_invalid")
        return VerifiedParticipantAuthentication(
            principal=principal(self.event_id),
            database_time=RECEIVED_AT,
            expires_at=RECEIVED_AT + timedelta(hours=1),
            verified_from=monotonic(),
        )

    def authenticate_in_session(self, session: Session, token: str) -> ParticipantPrincipal:
        self.sessions.append(session)
        if self.io_gate is not None:
            await_only(self.io_gate.wait())
        if token != SESSION_VALUE_FIXTURE:
            raise ParticipantSessionInvalidError("participant_session_invalid")
        return principal(self.event_id)


class ProjectionFixture:
    def __init__(self) -> None:
        self.sessions: list[Session] = []
        self.io_delay_seconds = 0.0

    def get_answer_status_in_session(
        self, session: Session, identity: ParticipantPrincipal, client_answer_id: UUID
    ) -> UnsavedAnswerStatusResponse:
        self.sessions.append(session)
        if self.io_delay_seconds:
            await_only(asyncio.sleep(self.io_delay_seconds))
        assert identity.participant_id == PARTICIPANT_ID
        return UnsavedAnswerStatusResponse(status="NotStored", client_answer_id=client_answer_id)

    def get_server_time_in_session(self, session: Session) -> datetime:
        self.sessions.append(session)
        return RECEIVED_AT

    @staticmethod
    def _event() -> AudienceEventResponse:
        return AudienceEventResponse(
            timing_basis="server_received_v1",
            arrival_grace_ms=0,
            clock_tolerance_ms=0,
            client_ahead_tolerance_ms=0,
            event_id=EVENT_ID,
            event_title="Fixture",
            event_state=EventState.IN_PROGRESS,
            event_version=1,
            start_delay_seconds=3,
        )

    def get_participant_state_in_session(
        self, session: Session, identity: ParticipantPrincipal
    ) -> ParticipantStateResponse:
        self.sessions.append(session)
        return ParticipantStateResponse(
            award_ceremony=None,
            result_visibility=ResultVisibility(
                distribution=False, personal_ranking=False, overall_ranking=False
            ),
            latest_ranking_snapshot_version=None,
            personal_result_delivery=PersonalResultDelivery(status="none", revision=None),
            projected_at=RECEIVED_AT,
            event=self._event(),
            participant=ParticipantIdentityResponse(
                public_label=identity.public_label, is_ranking_eligible=True
            ),
            current_round=None,
            latest_result=None,
        )

    def get_common_state(self, event_id: UUID) -> ParticipantCommonStateResponse:
        assert event_id == EVENT_ID
        return ParticipantCommonStateResponse(
            award_ceremony=None,
            projected_at=RECEIVED_AT,
            event=self._event(),
            current_round=None,
            latest_result=None,
            result_visibility=ResultVisibility(
                distribution=False, personal_ranking=False, overall_ranking=False
            ),
            latest_ranking_snapshot_version=None,
        )

    async def prepare_common_state(self, event_id: UUID) -> ParticipantCommonStateResponse:
        return await asyncio.to_thread(self.get_common_state, event_id)


class DomainFixture:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.command: SubmitAnswerCommand | None = None
        self.expected_event_id: UUID | None = None
        self.replay: AcceptedAnswer | None = None
        self.failure: BaseException | None = None

    def submit_answer_in_transaction(
        self,
        _session: Session,
        command: SubmitAnswerCommand,
        *,
        expected_event_id: UUID | None = None,
    ) -> AcceptedAnswer:
        self._events.append("domain")
        self.command = command
        self.expected_event_id = expected_event_id
        if self.failure is not None:
            raise self.failure
        return accepted(command)

    def get_answer_replay_in_session(
        self, _session: Session, _command: SubmitAnswerCommand
    ) -> AcceptedAnswer | None:
        self._events.append("replay")
        return self.replay


class ServiceFixture:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.factory = TraceSessionFactory()
        self.authentication = AuthenticationFixture()
        self.projection = ProjectionFixture()
        self.domain = DomainFixture(self.factory.events)
        self.event_sessions: list[Session] = []

        def resolve_event(session: Session) -> UUID:
            self.event_sessions.append(session)
            return EVENT_ID

        monkeypatch.setattr(service_module, "resolve_single_event_id", resolve_event)
        self.service = AudienceAsyncService(
            cast(async_sessionmaker[AsyncSession], self.factory),
            cast(ParticipantAuthService, self.authentication),
            cast(AudienceProjectionService, self.projection),
            cast(QuizDomainService, self.domain),
        )

    async def submit(self) -> service_module.CommittedParticipantAnswer:
        return await self.service.submit_answer(
            "session-fixture",
            ROUND_ID,
            SubmitAnswerRequest(
                client_elapsed_ms=1000,
                round_version=1,
                option_id=OPTION_ID,
                client_answer_id=CLIENT_ANSWER_ID,
            ),
        )


@pytest.fixture
def answer_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[AnswerDiagnostic, list[AnswerDiagnosticRecord]]]:
    records: list[AnswerDiagnosticRecord] = []

    def capture(_message: str, *, record: str, **_fields: str) -> None:
        records.append(AnswerDiagnosticRecord.model_validate_json(record))

    monkeypatch.setattr("quiz_app.observability.answer_diagnostics.logfire.info", capture)
    diagnostic = AnswerDiagnostic("answer_post")
    token = current_answer_diagnostic.set(diagnostic)
    try:
        yield diagnostic, records
    finally:
        current_answer_diagnostic.reset(token)


@pytest.mark.asyncio
async def test_diagnostics_order_mock_commit_before_ack_and_status_visibility(
    monkeypatch: pytest.MonkeyPatch,
    answer_diagnostics: tuple[AnswerDiagnostic, list[AnswerDiagnosticRecord]],
) -> None:
    """mockのcommit gateによる順序確認であり、実DB isolationやTLS配送の実証ではない。"""
    fixture = ServiceFixture(monkeypatch)
    diagnostic, records = answer_diagnostics
    fixture.factory.commit_gate = asyncio.Event()

    def projected_status(
        _session: Session, _identity: ParticipantPrincipal, client_answer_id: UUID
    ) -> AnswerStatusResponse:
        record_answer_event("status_query_started")
        if "commit:0" not in fixture.factory.events:
            result: AnswerStatusResponse = UnsavedAnswerStatusResponse(
                status="NotStored", client_answer_id=client_answer_id
            )
        else:
            result = AcceptedAnswerStatusResponse(
                status="AcceptedLocked",
                answer=ParticipantAnswerResponse(
                    timing_basis="server_received_v1",
                    client_elapsed_ms=None,
                    answer_id=UUID(int=7),
                    round_id=ROUND_ID,
                    option_id=OPTION_ID,
                    client_answer_id=client_answer_id,
                    received_at=RECEIVED_AT,
                ),
            )
        record_answer_event("status_query_finished", status=result.status)
        return result

    monkeypatch.setattr(fixture.projection, "get_answer_status_in_session", projected_status)

    async def status_request() -> AnswerStatusResponse:
        status_diagnostic = AnswerDiagnostic("answer_status")
        token = current_answer_diagnostic.set(status_diagnostic)
        try:
            return await fixture.service.get_answer_status(SESSION_VALUE_FIXTURE, CLIENT_ANSWER_ID)
        finally:
            current_answer_diagnostic.reset(token)

    pending = asyncio.create_task(fixture.submit())
    try:
        await asyncio.sleep(0)
        assert "commit_started" in [record.event for record in records]
        assert "commit_acknowledged" not in [record.event for record in records]
        assert not pending.done()
        assert (await status_request()).status == "NotStored"
        fixture.factory.commit_gate.set()
        assert (await pending).answer.is_replay is False
        assert (await status_request()).status == "AcceptedLocked"
    finally:
        fixture.factory.commit_gate.set()
        if not pending.done():
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending

    post_records = [record for record in records if record.request_id == diagnostic.request_id]
    assert [record.event for record in post_records] == [
        "answer_bound",
        "transaction_started",
        "commit_started",
        "commit_acknowledged",
    ]
    status_records = [record for record in records if record.event == "status_query_finished"]
    assert [record.status for record in status_records] == ["NotStored", "AcceptedLocked"]
    assert all(record.answer_trace == diagnostic.answer_trace for record in records)
    ack_index = next(i for i, record in enumerate(records) if record.event == "commit_acknowledged")
    query_indices = [
        i for i, record in enumerate(records) if record.event == "status_query_finished"
    ]
    assert query_indices[0] < ack_index < query_indices[1]
    assert [record.monotonic_ns for record in records] == sorted(
        record.monotonic_ns for record in records
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (VersionConflictError("private-error-body"), "version_conflict"),
        (AnswerWindowClosedError("private-error-body"), "answer_window_closed"),
        (AnswerContentConflictError("private-error-body"), "answer_content_conflict"),
        (asyncio.CancelledError("private-error-body"), "cancelled"),
    ],
)
async def test_domain_failure_diagnostic_does_not_claim_commit(
    monkeypatch: pytest.MonkeyPatch,
    answer_diagnostics: tuple[AnswerDiagnostic, list[AnswerDiagnosticRecord]],
    failure: BaseException,
    reason: str,
) -> None:
    fixture = ServiceFixture(monkeypatch)
    fixture.domain.failure = failure
    _, records = answer_diagnostics
    with pytest.raises(type(failure)):
        await fixture.submit()
    assert [record.event for record in records] == [
        "answer_bound",
        "transaction_started",
        "transaction_failed",
        "failure_frame",
    ]
    assert records[-2].reason == reason
    assert records[-2].transaction_phase == "body"
    assert records[-1].failure_boundary == "transaction_failed"
    assert records[-1].source_function == "_commit_answer"
    assert records[-1].source_module == "quiz_app.audience.async_service"
    assert "private-error-body" not in records[-1].model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [IntegrityError(None, None, ValueError("private-sql")), asyncio.CancelledError()]
)
async def test_commit_failure_and_cancellation_never_emit_acknowledged(
    monkeypatch: pytest.MonkeyPatch,
    answer_diagnostics: tuple[AnswerDiagnostic, list[AnswerDiagnosticRecord]],
    failure: BaseException,
) -> None:
    fixture = ServiceFixture(monkeypatch)
    fixture.factory.commit_error = failure
    _, records = answer_diagnostics
    with pytest.raises(type(failure)):
        await fixture.submit()
    assert not any(record.event == "commit_acknowledged" for record in records)
    failed = next(record for record in records if record.event == "transaction_failed")
    assert failed.transaction_phase == "commit"
    assert failed.reason == (
        "database_error" if isinstance(failure, IntegrityError) else "cancelled"
    )
    assert "private-sql" not in failed.model_dump_json()


@pytest.mark.asyncio
async def test_integrity_recheck_logs_replay_without_fabricated_commit_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
    answer_diagnostics: tuple[AnswerDiagnostic, list[AnswerDiagnosticRecord]],
) -> None:
    fixture = ServiceFixture(monkeypatch)
    fixture.factory.commit_error = IntegrityError(None, None, ValueError("private-sql"))
    fixture.domain.replay = accepted(
        SubmitAnswerCommand(
            client_elapsed_ms=1000,
            participant_id=PARTICIPANT_ID,
            round_id=ROUND_ID,
            round_version=1,
            option_id=OPTION_ID,
            client_answer_id=CLIENT_ANSWER_ID,
        ),
        replay=True,
    )
    _, records = answer_diagnostics
    assert (await fixture.submit()).answer.is_replay is True
    assert [record.event for record in records] == [
        "answer_bound",
        "transaction_started",
        "commit_started",
        "transaction_failed",
        "failure_frame",
        "integrity_recheck_started",
        "integrity_recheck_finished",
    ]
    assert records[-1].status == "AcceptedLocked"
    assert records[-1].is_replay is True


@pytest.mark.asyncio
async def test_failed_authentication_never_binds_answer_trace(
    monkeypatch: pytest.MonkeyPatch,
    answer_diagnostics: tuple[AnswerDiagnostic, list[AnswerDiagnosticRecord]],
) -> None:
    fixture = ServiceFixture(monkeypatch)
    diagnostic, records = answer_diagnostics
    with pytest.raises(ParticipantSessionInvalidError):
        await fixture.service.get_answer_status("invalid", CLIENT_ANSWER_ID)
    assert diagnostic.answer_trace is None
    assert records == []


@pytest.mark.asyncio
async def test_status_metadata_and_projection_share_one_short_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ServiceFixture(monkeypatch)
    status = await fixture.service.get_answer_status("session-fixture", CLIENT_ANSWER_ID)
    assert status.status == "NotStored"
    assert len(fixture.factory.sessions) == 1
    assert fixture.event_sessions == fixture.authentication.sessions == fixture.projection.sessions
    assert fixture.factory.events == ["open:0", "close:0"]


@pytest.mark.asyncio
async def test_time_uses_database_clock_after_same_session_event_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ServiceFixture(monkeypatch)
    assert (await fixture.service.get_server_time()).server_time == RECEIVED_AT
    assert fixture.event_sessions == fixture.projection.sessions
    assert len(fixture.factory.sessions) == 1


@pytest.mark.asyncio
async def test_simultaneous_clock_requests_share_only_inflight_database_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ServiceFixture(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = fixture.projection.get_server_time_in_session

    def delayed_clock(session: Session) -> datetime:
        entered.set()
        await_only(release.wait())
        return original(session)

    monkeypatch.setattr(fixture.projection, "get_server_time_in_session", delayed_clock)
    requests = [asyncio.create_task(fixture.service.get_server_time()) for _ in range(100)]
    await asyncio.wait_for(entered.wait(), 1)
    requests[0].cancel()
    assert len(fixture.factory.sessions) == 1
    release.set()
    results = await asyncio.gather(*requests, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert all(
        isinstance(result, service_module.TimeResponse) and result.server_time == RECEIVED_AT
        for result in results[1:]
    )
    assert (await fixture.service.get_server_time()).server_time == RECEIVED_AT
    assert len(fixture.factory.sessions) == 2
    await fixture.service.close()


@pytest.mark.asyncio
async def test_late_clock_join_reports_age_and_timeout_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ServiceFixture(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    now = [10.0]
    monkeypatch.setattr(service_module, "monotonic", lambda: now[0])

    def delayed_clock(_: Session) -> datetime:
        entered.set()
        await_only(release.wait())
        return RECEIVED_AT

    monkeypatch.setattr(fixture.projection, "get_server_time_in_session", delayed_clock)
    first = asyncio.create_task(fixture.service.get_server_time())
    await asyncio.wait_for(entered.wait(), 1)
    now[0] += 2.0
    late = asyncio.create_task(fixture.service.get_server_time())
    await asyncio.sleep(0)
    release.set()
    initial, joined = await asyncio.gather(first, late)
    assert initial.shared_sample_age_ms == 0
    assert joined.shared_sample_age_ms == 2000
    assert initial.server_time == joined.server_time
    assert len(fixture.factory.sessions) == 1
    release.clear()
    monkeypatch.setattr(service_module, "COMMON_STATE_LOAD_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(AudienceServiceUnavailableError, match="database_clock_deferred"):
        await fixture.service.get_server_time()
    await fixture.service.close()


@pytest.mark.asyncio
async def test_realtime_authentication_uses_fresh_same_session_event_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ServiceFixture(monkeypatch)
    for _ in range(2):
        verified = await fixture.service.authenticate_realtime(SESSION_VALUE_FIXTURE)
        assert verified.principal == principal()
        assert verified.expires_at == RECEIVED_AT + timedelta(hours=1)
    assert fixture.authentication.sessions == []
    assert fixture.authentication.realtime_sessions == fixture.event_sessions
    assert len(fixture.factory.sessions) == 2
    assert fixture.factory.events == ["open:0", "close:0", "open:1", "close:1"]


@pytest.mark.asyncio
async def test_realtime_authentication_rejects_missing_invalid_and_cross_event_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ServiceFixture(monkeypatch)
    with pytest.raises(ParticipantSessionInvalidError, match="participant_session_missing"):
        await fixture.service.authenticate_realtime(None)
    assert fixture.factory.sessions == []
    with pytest.raises(ParticipantSessionInvalidError, match="participant_session_invalid"):
        await fixture.service.authenticate_realtime("invalid-fixture")
    fixture.authentication.event_id = UUID(int=99)
    with pytest.raises(ParticipantSessionInvalidError, match="participant_session_event_mismatch"):
        await fixture.service.authenticate_realtime(SESSION_VALUE_FIXTURE)
    assert fixture.factory.events == ["open:0", "close:0", "open:1", "close:1"]


@pytest.mark.asyncio
async def test_realtime_authentication_requires_single_event_before_fresh_session_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ServiceFixture(monkeypatch)

    def unavailable(_session: Session) -> UUID:
        raise SingleEventConfigurationError("single_event_configuration_required")

    monkeypatch.setattr(service_module, "resolve_single_event_id", unavailable)
    with pytest.raises(SingleEventConfigurationError):
        await fixture.service.authenticate_realtime(SESSION_VALUE_FIXTURE)
    assert fixture.authentication.realtime_sessions == []
    assert fixture.factory.events == ["open:0", "close:0"]


@pytest.mark.asyncio
async def test_ws_auth_and_http_recovery_share_two_slots_without_blocking_operator_or_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """driver待機の模擬検証であり、実DBや600接続の容量証明ではない。"""
    fixture = ServiceFixture(monkeypatch)
    first_entered, two_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    authenticate = fixture.authentication.authenticate_realtime_in_session
    project = fixture.projection.get_participant_state_in_session
    active = 0
    maximum = 0

    def delayed_read() -> None:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        first_entered.set()
        if active == 2:
            two_entered.set()
        try:
            await_only(release.wait())
        finally:
            active -= 1

    def delayed_auth(session: Session, token: str) -> VerifiedParticipantAuthentication:
        delayed_read()
        return authenticate(session, token)

    def delayed_state(session: Session, identity: ParticipantPrincipal) -> ParticipantStateResponse:
        delayed_read()
        return project(session, identity)

    monkeypatch.setattr(fixture.authentication, "authenticate_realtime_in_session", delayed_auth)
    monkeypatch.setattr(fixture.projection, "get_participant_state_in_session", delayed_state)
    limiter = current_default_thread_limiter()
    previous_tokens = limiter.borrowed_tokens
    requests = [asyncio.create_task(fixture.service.authenticate_realtime(SESSION_VALUE_FIXTURE))]
    try:
        await asyncio.wait_for(first_entered.wait(), 1)
        requests.append(
            asyncio.create_task(fixture.service.get_participant_state(SESSION_VALUE_FIXTURE))
        )
        await asyncio.wait_for(two_entered.wait(), 1)
        requests.extend(
            asyncio.create_task(fixture.service.authenticate_realtime(SESSION_VALUE_FIXTURE))
            for _ in range(40)
        )
        requests.extend(
            asyncio.create_task(fixture.service.get_participant_state(SESSION_VALUE_FIXTURE))
            for _ in range(20)
        )
        await asyncio.sleep(0)
        assert len(fixture.factory.sessions) == 2
        assert limiter.borrowed_tokens == previous_tokens
        assert await asyncio.wait_for(run_in_threadpool(lambda: "operator-finished"), 0.5) == (
            "operator-finished"
        )
        committed = await asyncio.wait_for(fixture.submit(), 0.5)
        assert committed.answer.answer_id == UUID(int=7)
        assert maximum == 2
        release.set()
        await asyncio.gather(*requests)
        assert maximum == 2
    finally:
        release.set()
        await asyncio.gather(*requests, return_exceptions=True)


@pytest.mark.asyncio
async def test_realtime_authentication_timeout_closes_session_and_releases_recovery_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ServiceFixture(monkeypatch)
    authenticate = fixture.authentication.authenticate_realtime_in_session

    def blocked(_session: Session, _token: str) -> VerifiedParticipantAuthentication:
        await_only(asyncio.Future())
        raise AssertionError("unreachable")

    monkeypatch.setattr(fixture.authentication, "authenticate_realtime_in_session", blocked)
    monkeypatch.setattr(service_module, "COMMON_STATE_LOAD_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(AudienceServiceUnavailableError, match="participant_recovery_deferred"):
        await fixture.service.authenticate_realtime(SESSION_VALUE_FIXTURE)
    assert fixture.factory.events == ["open:0", "close:0"]
    monkeypatch.setattr(fixture.authentication, "authenticate_realtime_in_session", authenticate)
    assert (
        await fixture.service.authenticate_realtime(SESSION_VALUE_FIXTURE)
    ).principal == principal()


@pytest.mark.asyncio
async def test_recovery_admission_preserves_answer_commit_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ServiceFixture(monkeypatch)
    two_entered, release = asyncio.Event(), asyncio.Event()
    original = fixture.projection.get_participant_state_in_session
    active = 0
    maximum = 0

    def delayed_state(session: Session, identity: ParticipantPrincipal) -> ParticipantStateResponse:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == 2:
            two_entered.set()
        try:
            await_only(release.wait())
            return original(session, identity)
        finally:
            active -= 1

    monkeypatch.setattr(fixture.projection, "get_participant_state_in_session", delayed_state)
    requests = [
        asyncio.create_task(fixture.service.get_participant_state(SESSION_VALUE_FIXTURE))
        for _ in range(20)
    ]
    try:
        await asyncio.wait_for(two_entered.wait(), 1)
        assert len(fixture.factory.sessions) == 2
        committed = await asyncio.wait_for(fixture.submit(), 0.5)
        assert committed.answer.answer_id == UUID(int=7)
        assert maximum == 2
        release.set()
        await asyncio.gather(*requests)
        assert maximum == 2
    finally:
        release.set()
        await asyncio.gather(*requests, return_exceptions=True)


@pytest.mark.asyncio
async def test_state_metadata_and_projection_use_one_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ServiceFixture(monkeypatch)
    await fixture.service.get_participant_state(SESSION_VALUE_FIXTURE)
    assert fixture.authentication.sessions == fixture.event_sessions
    assert fixture.event_sessions == fixture.projection.sessions
    assert len(fixture.factory.sessions) == 1
    assert fixture.factory.events[-1] == "close:0"


@pytest.mark.asyncio
async def test_state_cache_miss_releases_event_lock_before_shared_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ServiceFixture(monkeypatch)
    project = fixture.projection.get_participant_state_in_session
    load_common = fixture.projection.get_common_state
    attempts = 0

    def miss_once(session: Session, identity: ParticipantPrincipal) -> ParticipantStateResponse:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise CommonStateRefreshRequired(EVENT_ID)
        return project(session, identity)

    def build_after_close(event_id: UUID) -> ParticipantCommonStateResponse:
        assert fixture.factory.events == ["open:0", "close:0"]
        return load_common(event_id)

    monkeypatch.setattr(fixture.projection, "get_participant_state_in_session", miss_once)
    monkeypatch.setattr(fixture.projection, "get_common_state", build_after_close)
    state = await fixture.service.get_participant_state(SESSION_VALUE_FIXTURE)
    assert state.event.event_id == EVENT_ID
    assert fixture.factory.events == ["open:0", "close:0", "open:1", "close:1"]
    assert len(fixture.authentication.sessions) == 2


@pytest.mark.asyncio
async def test_state_cache_invalidation_retry_is_bounded_and_closes_each_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ServiceFixture(monkeypatch)
    load_common = fixture.projection.get_common_state
    builds = 0

    def miss(_session: Session, _identity: ParticipantPrincipal) -> ParticipantStateResponse:
        raise CommonStateRefreshRequired(EVENT_ID)

    def build(event_id: UUID) -> ParticipantCommonStateResponse:
        nonlocal builds
        builds += 1
        assert fixture.factory.events[-1].startswith("close:")
        return load_common(event_id)

    monkeypatch.setattr(fixture.projection, "get_participant_state_in_session", miss)
    monkeypatch.setattr(fixture.projection, "get_common_state", build)
    with pytest.raises(AudienceServiceUnavailableError, match="common_state_retry_exhausted"):
        await fixture.service.get_participant_state(SESSION_VALUE_FIXTURE)
    assert builds == 2
    assert len(fixture.factory.sessions) == 3
    assert fixture.factory.events[-1] == "close:2"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_state_cache_preparation_timeout_and_cancel_release_request_resources(
    monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    fixture = ServiceFixture(monkeypatch)
    load_common = fixture.projection.get_common_state
    started = ThreadEvent()
    release = ThreadEvent()
    finished = ThreadEvent()

    def miss(_session: Session, _identity: ParticipantPrincipal) -> ParticipantStateResponse:
        raise CommonStateRefreshRequired(EVENT_ID)

    def build(event_id: UUID) -> ParticipantCommonStateResponse:
        started.set()
        try:
            assert release.wait(timeout=1)
            return load_common(event_id)
        finally:
            finished.set()

    monkeypatch.setattr(fixture.projection, "get_participant_state_in_session", miss)
    monkeypatch.setattr(fixture.projection, "get_common_state", build)
    monkeypatch.setattr(
        service_module, "COMMON_STATE_LOAD_TIMEOUT_SECONDS", 0.025 if not cancel else 1
    )
    task = asyncio.create_task(fixture.service.get_participant_state(SESSION_VALUE_FIXTURE))
    try:
        assert await asyncio.to_thread(started.wait, 0.5)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(
                AudienceServiceUnavailableError, match="participant_recovery_deferred"
            ):
                await asyncio.wait_for(task, timeout=0.5)
        assert fixture.factory.events == ["open:0", "close:0"]
        assert len(fixture.factory.sessions) == 1
    finally:
        # to_threadの取消後も継続するbuilderが、完了して回収されることを確認する。
        release.set()
        assert await asyncio.to_thread(finished.wait, 0.5)


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, "invalid"])
async def test_invalid_authentication_closes_metadata_session_without_projection(
    monkeypatch: pytest.MonkeyPatch, token: str | None
) -> None:
    fixture = ServiceFixture(monkeypatch)
    with pytest.raises(ParticipantSessionInvalidError):
        await fixture.service.get_answer_status(token, CLIENT_ANSWER_ID)
    assert fixture.factory.events == ["open:0", "close:0"]
    assert fixture.projection.sessions == []


@pytest.mark.asyncio
async def test_cross_event_principal_is_rejected_before_write_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ServiceFixture(monkeypatch)
    fixture.authentication.event_id = UUID(int=99)
    with pytest.raises(ParticipantSessionInvalidError, match="participant_session_event_mismatch"):
        await fixture.submit()
    assert len(fixture.factory.sessions) == 1
    assert fixture.domain.command is None


@pytest.mark.asyncio
async def test_missing_event_still_fails_closed_before_time_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ServiceFixture(monkeypatch)

    def unavailable(_session: Session) -> UUID:
        raise SingleEventConfigurationError("single_event_configuration_required")

    monkeypatch.setattr(service_module, "resolve_single_event_id", unavailable)
    with pytest.raises(SingleEventConfigurationError):
        await fixture.service.get_server_time()
    assert fixture.projection.sessions == []
    assert fixture.factory.events == ["open:0", "close:0"]


@pytest.mark.asyncio
async def test_post_metadata_and_save_share_one_transaction_and_committed_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ServiceFixture(monkeypatch)
    result = await fixture.submit()
    assert fixture.factory.events == [
        "open:0",
        "begin:0",
        "domain",
        "commit:0",
        "close:0",
    ]
    assert result.event_id == EVENT_ID
    assert result.answer.received_at == RECEIVED_AT
    assert fixture.domain.command is not None
    assert fixture.domain.command.participant_id == PARTICIPANT_ID
    assert fixture.domain.expected_event_id == EVENT_ID
    assert fixture.event_sessions == fixture.authentication.sessions
    assert fixture.event_sessions == [fixture.factory.sessions[0].sync_session]


@pytest.mark.asyncio
async def test_post_result_is_unavailable_until_commit_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ServiceFixture(monkeypatch)
    fixture.factory.commit_gate = asyncio.Event()
    pending = asyncio.create_task(fixture.submit())
    await asyncio.sleep(0)
    assert "domain" in fixture.factory.events
    assert not pending.done()
    fixture.factory.commit_gate.set()
    assert (await pending).answer.is_replay is False


@pytest.mark.asyncio
async def test_integrity_failure_rechecks_replay_in_a_new_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ServiceFixture(monkeypatch)
    fixture.factory.commit_error = IntegrityError(None, None, ValueError("fixture_constraint"))
    command = SubmitAnswerCommand(
        client_elapsed_ms=1000,
        participant_id=PARTICIPANT_ID,
        round_id=ROUND_ID,
        round_version=1,
        option_id=OPTION_ID,
        client_answer_id=CLIENT_ANSWER_ID,
    )
    fixture.domain.replay = accepted(command, replay=True)
    result = await fixture.submit()
    assert result.answer.is_replay is True
    assert len(fixture.factory.sessions) == 2
    assert fixture.factory.events[-3:] == ["open:1", "replay", "close:1"]
    assert fixture.factory.events.index("close:0") < fixture.factory.events.index("open:1")


@pytest.mark.asyncio
async def test_integrity_failure_without_saved_answer_is_not_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ServiceFixture(monkeypatch)
    fixture.factory.commit_error = IntegrityError(None, None, ValueError("fixture_constraint"))
    with pytest.raises(IntegrityError):
        await fixture.submit()
    assert len(fixture.factory.sessions) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [ResourceNotFoundError("round_not_found"), asyncio.CancelledError()]
)
async def test_domain_failure_rolls_back_and_closes_without_integrity_retry(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    fixture = ServiceFixture(monkeypatch)
    fixture.domain.failure = failure
    with pytest.raises(type(failure)):
        await fixture.submit()
    assert fixture.factory.events[-2:] == ["rollback:0", "close:0"]
    assert len(fixture.factory.sessions) == 1
    assert "replay" not in fixture.factory.events


@pytest.mark.asyncio
async def test_driver_await_yields_loop_and_metadata_stays_in_total_latency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ServiceFixture(monkeypatch)
    fixture.authentication.io_gate = asyncio.Event()
    fixture.projection.io_delay_seconds = 0.015
    timing = RequestTiming()
    context_token = current_request_timing.set(timing)

    async def request() -> None:
        await mark_handler_dispatch()
        with observe_handler(is_async=True):
            await fixture.service.get_answer_status(SESSION_VALUE_FIXTURE, CLIENT_ANSWER_ID)
        timing.finish_response()

    pending = asyncio.create_task(request())
    try:
        await asyncio.sleep(0)
        # 実run_syncのgreenlet内のdriver待ちを模擬し、別taskが進むことを確認する。
        assert fixture.authentication.sessions
        assert not pending.done()
        await asyncio.sleep(0.02)
        fixture.authentication.io_gate.set()
        await asyncio.wait_for(pending, timeout=1)
    finally:
        fixture.authentication.io_gate.set()
        if not pending.done():
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        current_request_timing.reset(context_token)

    assert timing.durations["participant_metadata"] >= 0.02
    assert timing.durations["status_projection"] >= 0.015
    assert timing.durations["handler"] >= (
        timing.durations["participant_metadata"] + timing.durations["status_projection"]
    )
    assert timing.durations["api_total"] >= timing.durations["handler"]
    assert "thread_wait" not in timing.durations
    assert "dispatch_wait" in timing.durations
    assert timing.database_work == {"sessions": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["state", "status", "time", "post", "post_retry"])
async def test_cold_request_session_work_is_bounded_and_stops_at_response(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    fixture = ServiceFixture(monkeypatch)
    timing = RequestTiming()
    context_token = current_request_timing.set(timing)
    try:
        if operation == "state":
            await fixture.service.get_participant_state(SESSION_VALUE_FIXTURE)
        elif operation == "status":
            await fixture.service.get_answer_status(SESSION_VALUE_FIXTURE, CLIENT_ANSWER_ID)
        elif operation == "time":
            await fixture.service.get_server_time()
        else:
            if operation == "post_retry":
                fixture.factory.commit_error = IntegrityError(
                    None, None, ValueError("fixture_constraint")
                )
                command = SubmitAnswerCommand(
                    client_elapsed_ms=1000,
                    participant_id=PARTICIPANT_ID,
                    round_id=ROUND_ID,
                    round_version=1,
                    option_id=OPTION_ID,
                    client_answer_id=CLIENT_ANSWER_ID,
                )
                fixture.domain.replay = accepted(command, replay=True)
            await fixture.submit()
        expected_sessions = 2 if operation == "post_retry" else 1
        assert timing.database_work == {"sessions": expected_sessions}
        assert len(fixture.factory.sessions) == expected_sessions
        # fixtureはcacheを持たず、cold認証も同じ1回の開催照合Sessionに収まる。
        assert len(fixture.event_sessions) == 1
        assert len(fixture.authentication.sessions) == (0 if operation == "time" else 1)
        timing.finish_response()
        frozen_counts = timing.database_work.copy()
        timing.count_database_work("sessions")
        assert timing.database_work == frozen_counts
    finally:
        current_request_timing.reset(context_token)
