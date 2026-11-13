"""Cookie有り復旧の構築待ちとDB枠を分離する因果回帰。実TLS負荷ではない。"""

import asyncio
from threading import Event as ThreadEvent
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.util.concurrency import await_only
from sqlmodel import Session
from test_audience_async_service import (
    CLIENT_ANSWER_ID,
    EVENT_ID,
    OPTION_ID,
    RECEIVED_AT,
    ROUND_ID,
    SESSION_VALUE_FIXTURE,
    ServiceFixture,
    principal,
)
from test_participant_initial_session import application

import quiz_app.audience.async_service as service_module
from quiz_app.audience.auth_service import ParticipantPrincipal
from quiz_app.audience.contracts import (
    ParticipantAnswerResponse,
    ParticipantCommonStateResponse,
    ParticipantStateResponse,
    PersonalResultDelivery,
    PersonalResultResponse,
)
from quiz_app.audience.errors import AudienceServiceUnavailableError, ParticipantSessionInvalidError
from quiz_app.config import PARTICIPANT_SESSION_COOKIE_NAME
from quiz_app.observability.metrics import QuizMetrics
from quiz_app.observability.middleware import PrometheusMiddleware
from quiz_app.observability.request_timing import RequestTiming, current_request_timing
from quiz_app.quiz_configuration import ResultVisibility
from quiz_app.realtime.state_cache import CommonStatePreparation, CommonStateRefreshRequired


class ColdProjection:
    """実single-flightを使い、解放前のDB枠と解放後の再照合を観測する。"""

    def __init__(self, fixture: ServiceFixture, monkeypatch: pytest.MonkeyPatch) -> None:
        self.fixture = fixture
        self.ready = False
        self.release = ThreadEvent()
        self.started = ThreadEvent()
        self.waiting = asyncio.Event()
        self.expected_waiters = 2
        self.waiters = 0
        self.builds = 0
        self.event_version = 1
        self.answer: ParticipantAnswerResponse | None = None
        self._project = fixture.projection.get_participant_state_in_session
        self.preparation = CommonStatePreparation(self._build)
        monkeypatch.setattr(fixture.projection, "get_participant_state_in_session", self.state)
        monkeypatch.setattr(fixture.projection, "prepare_common_state", self.prepare)
        monkeypatch.setattr(fixture.service, "_participant_result", self.result)

    def _build(self, event_id: UUID) -> ParticipantCommonStateResponse:
        self.builds += 1
        self.started.set()
        assert self.release.wait(3)
        self.ready = True
        return self.fixture.projection.get_common_state(event_id)

    async def prepare(self, event_id: UUID) -> ParticipantCommonStateResponse:
        self.waiters += 1
        if self.waiters == self.expected_waiters:
            self.waiting.set()
        return await self.preparation.prepare(event_id)

    def state(self, session: Session, identity: ParticipantPrincipal) -> ParticipantStateResponse:
        if not self.ready:
            raise CommonStateRefreshRequired(identity.event_id)
        state = self._project(session, identity)
        return state.model_copy(
            update={
                "event": state.event.model_copy(update={"event_version": self.event_version}),
                "answer": self.answer,
                "result_visibility": ResultVisibility(
                    distribution=False, personal_ranking=False, overall_ranking=False
                ),
            }
        )

    def result(self, session: Session, token: str | None) -> PersonalResultResponse:
        identity = self.fixture.service._participant_context(session, token)
        if not self.ready:
            raise CommonStateRefreshRequired(identity.event_id)
        return PersonalResultResponse(delivery=PersonalResultDelivery(status="none", revision=None))

    async def close(self) -> None:
        self.release.set()
        await self.preparation.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [False, True])
async def test_common_build_wait_releases_slots_for_cookie_state_and_ws_authentication(
    monkeypatch: pytest.MonkeyPatch, result: bool
) -> None:
    fixture = ServiceFixture(monkeypatch)
    cold = ColdProjection(fixture, monkeypatch)
    get = (
        fixture.service.get_participant_result if result else fixture.service.get_participant_state
    )
    pending = [asyncio.create_task(get(SESSION_VALUE_FIXTURE)) for _ in range(2)]
    try:
        await asyncio.wait_for(cold.waiting.wait(), 1)
        assert await asyncio.to_thread(cold.started.wait, 1)
        assert fixture.factory.events == ["open:0", "close:0", "open:1", "close:1"]
        # 別要求が最新snapshotを利用できても、先行するbuilder完了通知が遅れ得る。
        cold.ready = True
        state = await asyncio.wait_for(
            fixture.service.get_participant_state(SESSION_VALUE_FIXTURE), 1
        )
        verified = await asyncio.wait_for(
            fixture.service.authenticate_realtime(SESSION_VALUE_FIXTURE), 1
        )
        assert state.participant.public_label == verified.principal.public_label == "ABC"
        assert all(not task.done() for task in pending)
    finally:
        await cold.close()
        await asyncio.gather(*pending, return_exceptions=True)
    assert cold.builds == 1


@pytest.mark.asyncio
async def test_100_cookie_requests_share_build_without_holding_slots_and_reread_current_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ServiceFixture(monkeypatch)
    cold = ColdProjection(fixture, monkeypatch)
    cold.expected_waiters = 100
    identities = {
        f"fixture-{index}": ParticipantPrincipal(
            participant_id=UUID(int=100 + index),
            session_id=UUID(int=1000 + index),
            event_id=EVENT_ID,
            public_label=f"A{chr(65 + index // 26)}{chr(65 + index % 26)}",
            is_ranking_eligible=True,
        )
        for index in range(100)
    }

    def authenticate(session: Session, token: str) -> ParticipantPrincipal:
        fixture.authentication.sessions.append(session)
        await_only(asyncio.sleep(0))
        return identities[token]

    monkeypatch.setattr(fixture.authentication, "authenticate_in_session", authenticate)
    app = application(fixture.service)
    app.add_middleware(PrometheusMiddleware, metrics=QuizMetrics())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        requests = [
            asyncio.create_task(
                client.get(
                    "/api/v1/participant/session",
                    headers={"Cookie": f"{PARTICIPANT_SESSION_COOKIE_NAME}={token}"},
                )
            )
            for token in identities
        ]
        try:
            await asyncio.wait_for(cold.waiting.wait(), 1)
            assert cold.builds == 1
            opened = [event for event in fixture.factory.events if event.startswith("open:")]
            closed = [event for event in fixture.factory.events if event.startswith("close:")]
            assert len(opened) == len(closed) == 100
            active = maximum = 0
            for event in fixture.factory.events:
                active += 1 if event.startswith("open:") else -1
                maximum = max(maximum, active)
            assert active == 0 and maximum == 2
            assert (
                await asyncio.wait_for(
                    fixture.service.authenticate_realtime(SESSION_VALUE_FIXTURE), 1
                )
            ).principal == principal()
            # 構築前のprojectionを保存せず、待ち中のEvent版・回答fixtureを再取得する。
            cold.event_version = 2
            cold.answer = ParticipantAnswerResponse(
                timing_basis="server_received_v1",
                client_elapsed_ms=None,
                answer_id=UUID(int=9),
                round_id=ROUND_ID,
                option_id=OPTION_ID,
                client_answer_id=CLIENT_ANSWER_ID,
                received_at=RECEIVED_AT,
            )
            cold.release.set()
            responses = await asyncio.wait_for(asyncio.gather(*requests), 1)
            assert all(response.status_code == 200 for response in responses)
            assert all(
                "recovery_wait;dur=" in response.headers["server-timing"]
                and "common_state_wait;dur=" in response.headers["server-timing"]
                for response in responses
            )
            states = [
                ParticipantStateResponse.model_validate(response.json()) for response in responses
            ]
            assert {state.participant.public_label for state in states} == {
                identity.public_label for identity in identities.values()
            }
            assert all(
                state.event.event_version == 2 and state.answer == cold.answer for state in states
            )
            assert len(fixture.authentication.sessions) == 200
        finally:
            await cold.close()
            await asyncio.gather(*requests, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_build_wait_does_not_cancel_shared_build_or_leak_a_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ServiceFixture(monkeypatch)
    cold = ColdProjection(fixture, monkeypatch)
    pending = [
        asyncio.create_task(fixture.service.get_participant_state(SESSION_VALUE_FIXTURE))
        for _ in range(2)
    ]
    try:
        await asyncio.wait_for(cold.waiting.wait(), 1)
        pending[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending[0]
        assert not pending[1].done()
        assert (
            await asyncio.wait_for(fixture.service.authenticate_realtime(SESSION_VALUE_FIXTURE), 1)
        ).principal == principal()
        cold.release.set()
        assert (await asyncio.wait_for(pending[1], 1)).participant.public_label == "ABC"
        assert cold.builds == 1
    finally:
        await cold.close()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_existing_cookie_is_rechecked_after_common_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ServiceFixture(monkeypatch)
    cold = ColdProjection(fixture, monkeypatch)
    cold.expected_waiters = 1
    pending = asyncio.create_task(fixture.service.get_participant_state(SESSION_VALUE_FIXTURE))
    try:
        await asyncio.wait_for(cold.waiting.wait(), 1)
        fixture.authentication.event_id = UUID(int=99)
        cold.release.set()
        with pytest.raises(
            ParticipantSessionInvalidError, match="participant_session_event_mismatch"
        ):
            await pending
        assert fixture.projection.sessions == []
        assert fixture.factory.events == ["open:0", "close:0", "open:1", "close:1"]
    finally:
        await cold.close()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_recovery_deadline_covers_db_build_and_retry_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ServiceFixture(monkeypatch)
    project = fixture.projection.get_participant_state_in_session
    attempts = 0

    def delayed_state(session: Session, identity: ParticipantPrincipal) -> ParticipantStateResponse:
        nonlocal attempts
        attempts += 1
        await_only(asyncio.sleep(0.025))
        if attempts == 1:
            raise CommonStateRefreshRequired(EVENT_ID)
        return project(session, identity)

    async def prepare(event_id: UUID) -> ParticipantCommonStateResponse:
        await asyncio.sleep(0.025)
        return fixture.projection.get_common_state(event_id)

    monkeypatch.setattr(fixture.projection, "get_participant_state_in_session", delayed_state)
    monkeypatch.setattr(fixture.projection, "prepare_common_state", prepare)
    monkeypatch.setattr(service_module, "COMMON_STATE_LOAD_TIMEOUT_SECONDS", 0.06)
    timing = RequestTiming()
    token = current_request_timing.set(timing)
    try:
        with pytest.raises(AudienceServiceUnavailableError, match="participant_recovery_deferred"):
            await asyncio.wait_for(
                fixture.service.get_participant_state(SESSION_VALUE_FIXTURE), 0.5
            )
    finally:
        current_request_timing.reset(token)
    assert "recovery_wait" in timing.durations
    assert "common_state_wait" in timing.durations
    assert sum(event.startswith("open:") for event in fixture.factory.events) == sum(
        event.startswith("close:") for event in fixture.factory.events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_queue_cancellation_records_wait_without_releasing_an_unowned_slot(
    monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    fixture = ServiceFixture(monkeypatch)
    release = asyncio.Event()
    occupied = asyncio.Event()
    fixture.authentication.io_gate = release
    original = fixture.authentication.authenticate_in_session

    def authenticate(session: Session, value: str) -> ParticipantPrincipal:
        if len(fixture.authentication.sessions) == 1:
            occupied.set()
        return original(session, value)

    monkeypatch.setattr(fixture.authentication, "authenticate_in_session", authenticate)
    holders = [
        asyncio.create_task(fixture.service.get_participant_state(SESSION_VALUE_FIXTURE))
        for _ in range(2)
    ]
    queued: asyncio.Task[ParticipantStateResponse] | None = None
    follower: asyncio.Task[ParticipantStateResponse] | None = None
    try:
        await asyncio.wait_for(occupied.wait(), 1)
        monkeypatch.setattr(service_module, "COMMON_STATE_LOAD_TIMEOUT_SECONDS", 0.02)
        timing = RequestTiming()
        token = current_request_timing.set(timing)
        try:
            queued = asyncio.create_task(
                fixture.service.get_participant_state(SESSION_VALUE_FIXTURE)
            )
        finally:
            current_request_timing.reset(token)
        await asyncio.sleep(0)
        if cancel:
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
        else:
            with pytest.raises(
                AudienceServiceUnavailableError, match="participant_recovery_deferred"
            ):
                await queued
        assert timing.durations["recovery_wait"] >= 0
        assert "participant_metadata" not in timing.durations
        monkeypatch.setattr(service_module, "COMMON_STATE_LOAD_TIMEOUT_SECONDS", 5)
        follower = asyncio.create_task(fixture.service.get_participant_state(SESSION_VALUE_FIXTURE))
        await asyncio.sleep(0)
        assert len(fixture.factory.sessions) == 2
        assert not follower.done()
        release.set()
        assert (await follower).participant.public_label == "ABC"
    finally:
        release.set()
        await asyncio.gather(
            *holders,
            *([queued] if queued is not None else []),
            *([follower] if follower is not None else []),
            return_exceptions=True,
        )
