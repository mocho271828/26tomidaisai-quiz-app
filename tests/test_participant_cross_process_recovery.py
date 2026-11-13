"""独立した2組のAPI状態で復旧経路を確認する。OS process・実DBの負荷試験ではない。"""

import asyncio
from time import monotonic
from typing import cast
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Engine
from sqlmodel import Session
from test_audience_async_service import (
    EVENT_ID,
    RECEIVED_AT,
    SESSION_VALUE_FIXTURE,
    ProjectionFixture,
    ServiceFixture,
    principal,
)
from test_participant_auth_cache import (
    NOW,
    authenticate,
    service_and_session,
)
from test_participant_auth_cache import (
    SESSION_VALUE_FIXTURE as AUTH_SESSION_VALUE,
)
from test_participant_initial_session import application
from test_participant_recovery_admission import ColdProjection

import quiz_app.audience.async_service as service_module
from quiz_app.audience.auth_service import ParticipantPrincipal
from quiz_app.audience.errors import ParticipantSessionInvalidError
from quiz_app.audience.projection import AudienceProjectionService
from quiz_app.models.core import Event
from quiz_app.realtime.state_cache import CommonStateRefreshRequired, ProjectionSnapshot


@pytest.mark.asyncio
async def test_two_api_recovery_queues_are_independent_and_missing_cookie_bypasses_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = ServiceFixture(monkeypatch), ServiceFixture(monkeypatch)
    monkeypatch.setattr(service_module, "resolve_single_event_id", lambda _: EVENT_ID)
    occupied, release = asyncio.Event(), asyncio.Event()
    first.authentication.io_gate = release
    original = first.authentication.authenticate_in_session

    def authenticate_first(session: Session, token: str) -> ParticipantPrincipal:
        if len(first.authentication.sessions) == 1:
            occupied.set()
        return original(session, token)

    monkeypatch.setattr(first.authentication, "authenticate_in_session", authenticate_first)
    blocked = [
        asyncio.create_task(first.service.get_participant_state(SESSION_VALUE_FIXTURE))
        for _ in range(2)
    ]
    try:
        await asyncio.wait_for(occupied.wait(), 1)
        async with (
            AsyncClient(
                transport=ASGITransport(app=application(first.service)), base_url="https://test"
            ) as first_client,
            AsyncClient(
                transport=ASGITransport(app=application(second.service)), base_url="https://test"
            ) as second_client,
        ):
            responses = await asyncio.wait_for(
                asyncio.gather(
                    *(
                        client.get("/api/v1/participant/session")
                        for client in (first_client, second_client)
                        for _ in range(50)
                    )
                ),
                1,
            )
            assert all(response.status_code == 401 for response in responses)
            assert all(response.headers["cache-control"] == "no-store" for response in responses)
        assert len(first.factory.sessions) == 2
        assert second.factory.sessions == []
        recovered = await asyncio.wait_for(
            second.service.get_participant_state(SESSION_VALUE_FIXTURE), 1
        )
        verified = await asyncio.wait_for(
            second.service.authenticate_realtime(SESSION_VALUE_FIXTURE), 1
        )
        assert recovered.participant.public_label == verified.principal.public_label == "ABC"
        assert all(not task.done() for task in blocked)
        assert len(second.factory.sessions) == 2
    finally:
        release.set()
        await asyncio.gather(*blocked, return_exceptions=True)


@pytest.mark.asyncio
async def test_common_build_is_shared_per_api_and_ws_on_other_api_can_authenticate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixtures = (ServiceFixture(monkeypatch), ServiceFixture(monkeypatch))
    monkeypatch.setattr(service_module, "resolve_single_event_id", lambda _: EVENT_ID)
    cold = tuple(ColdProjection(fixture, monkeypatch) for fixture in fixtures)
    pending = [
        asyncio.create_task(fixture.service.get_participant_state(SESSION_VALUE_FIXTURE))
        for fixture in fixtures
        for _ in range(2)
    ]
    try:
        await asyncio.wait_for(asyncio.gather(*(item.waiting.wait() for item in cold)), 1)
        assert all(
            await asyncio.gather(*(asyncio.to_thread(item.started.wait, 1) for item in cold))
        )
        assert [item.builds for item in cold] == [1, 1]
        assert all(
            fixture.factory.events == ["open:0", "close:0", "open:1", "close:1"]
            for fixture in fixtures
        )
        # HTTPが別APIの共有構築を待っていても、WS認証はそのAPIのDB枠を使える。
        verified = await asyncio.wait_for(
            fixtures[1].service.authenticate_realtime(SESSION_VALUE_FIXTURE), 1
        )
        assert verified.principal == principal()
        cold[0].release.set()
        first_states = await asyncio.wait_for(asyncio.gather(*pending[:2]), 1)
        assert all(state.participant.public_label == "ABC" for state in first_states)
        assert all(not task.done() for task in pending[2:])
        cold[1].release.set()
        second_states = await asyncio.wait_for(asyncio.gather(*pending[2:]), 1)
        assert all(state.participant == first_states[0].participant for state in second_states)
        assert [item.builds for item in cold] == [1, 1]
    finally:
        for item in cold:
            item.release.set()
        await asyncio.gather(*(item.close() for item in cold))
        await asyncio.gather(*pending, return_exceptions=True)


def test_http_on_one_api_and_ws_on_other_use_same_database_identity_without_cache_dependency() -> (
    None
):
    first, shared_database, first_clock = service_and_session()
    second, _, second_clock = service_and_session()
    identity = authenticate(first, shared_database)
    assert first._hash_token(AUTH_SESSION_VALUE) == second._hash_token(AUTH_SESSION_VALUE)
    verified = second.authenticate_realtime_in_session(
        cast(Session, shared_database), AUTH_SESSION_VALUE
    )
    assert verified.principal == identity
    assert second._authentication_cache._entries == {}
    assert shared_database.calls == 2

    second_clock.value = first_clock.value = 30
    assert authenticate(second, shared_database) == identity
    shared_database.credential.revoked_at = NOW
    for service in (first, second):
        with pytest.raises(ParticipantSessionInvalidError):
            service.authenticate_realtime_in_session(
                cast(Session, shared_database), AUTH_SESSION_VALUE
            )
    # HTTPの最大60秒契約は照会元ごとに始まり、他APIのcache hitでは延長されない。
    second_clock.value = first_clock.value = 60
    with pytest.raises(ParticipantSessionInvalidError):
        authenticate(first, shared_database)
    assert authenticate(second, shared_database) == identity
    second_clock.value = first_clock.value = 90
    with pytest.raises(ParticipantSessionInvalidError):
        authenticate(second, shared_database)


class SharedEventSession:
    """Event共有lockで得た版だけを模擬し、実DBへの接続は作らない。"""

    def __init__(self) -> None:
        self.event = Event(id=EVENT_ID, title="Fixture", version=1)

    def connection(self) -> None:
        pass

    def get(self, model: type[Event], event_id: UUID, *, with_for_update: object) -> Event:
        assert model is Event and event_id == EVENT_ID
        assert with_for_update == {"read": True}
        return self.event


def test_other_api_http_recovery_rejects_old_common_cache_without_redis_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = (
        AudienceProjectionService(cast(Engine, object())),
        AudienceProjectionService(cast(Engine, object())),
    )
    shared_database = SharedEventSession()
    common = ProjectionFixture().get_common_state(EVENT_ID)
    now = monotonic()
    for service in (first, second):
        service._common_cache.get_snapshot(
            EVENT_ID,
            lambda: ProjectionSnapshot(common, now, now + 5),
            lambda value: value.event.event_version,
        )
        monkeypatch.setattr(service, "_participant_answer_for_round", lambda *_: None)
        monkeypatch.setattr(service, "_database_time", lambda _: RECEIVED_AT)
        assert (
            service.get_participant_state_in_session(
                cast(Session, shared_database), principal()
            ).event.event_version
            == 1
        )

    shared_database.event.version = 2
    first.invalidate_common_state(EVENT_ID)
    # mutationを受けたAPIだけが無効化済みでも、他方のHTTPはDB版との不一致を拒む。
    with pytest.raises(CommonStateRefreshRequired):
        second.get_participant_state_in_session(cast(Session, shared_database), principal())
    assert second._common_cache.peek(EVENT_ID, 1) is None
