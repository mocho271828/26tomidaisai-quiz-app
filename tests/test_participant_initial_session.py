"""初回session GETの待ち列隔離を検証する。実TLS・DB容量の試験ではない。"""

import asyncio
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlmodel import Session
from test_audience_async_service import EVENT_ID, SESSION_VALUE_FIXTURE, ServiceFixture

import quiz_app.audience.async_service as service_module
from quiz_app.audience.async_service import AudienceAsyncService, get_audience_async_service
from quiz_app.audience.error_handlers import install_audience_error_handlers
from quiz_app.audience.errors import ParticipantSessionInvalidError
from quiz_app.audience.router import router
from quiz_app.config import PARTICIPANT_SESSION_COOKIE_NAME
from quiz_app.single_event import SingleEventConfigurationError


def application(service: AudienceAsyncService) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    install_audience_error_handlers(app)

    async def get_service() -> AudienceAsyncService:
        return service

    app.dependency_overrides[get_audience_async_service] = get_service
    return app


@pytest.mark.asyncio
async def test_100_missing_cookie_requests_finish_while_authenticated_recovery_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ServiceFixture(monkeypatch)
    occupied, release = asyncio.Event(), asyncio.Event()
    fixture.authentication.io_gate = release

    def resolve_event(session: Session) -> UUID:
        fixture.event_sessions.append(session)
        if len(fixture.event_sessions) == 2:
            occupied.set()
        return EVENT_ID

    monkeypatch.setattr(service_module, "resolve_single_event_id", resolve_event)
    recovering = [
        asyncio.create_task(fixture.service.get_participant_state(SESSION_VALUE_FIXTURE))
        for _ in range(2)
    ]
    try:
        await asyncio.wait_for(occupied.wait(), 1)
        async with AsyncClient(
            transport=ASGITransport(app=application(fixture.service)), base_url="https://test"
        ) as client:
            # 期限はtestのハング防止だけ。実サービスの応答SLOとは扱わない。
            responses = await asyncio.wait_for(
                asyncio.gather(*(client.get("/api/v1/participant/session") for _ in range(100))),
                1,
            )
        assert all(response.status_code == 401 for response in responses)
        assert all(
            response.json()["code"] == "participant_authentication_required"
            and response.headers["cache-control"] == "no-store"
            and "set-cookie" not in response.headers
            for response in responses
        )
        assert all(not request.done() for request in recovering)
        assert len(fixture.factory.sessions) == 2
        assert len(fixture.event_sessions) == len(fixture.authentication.sessions) == 2
        assert fixture.projection.sessions == []
    finally:
        release.set()
        await asyncio.gather(*recovering)
    assert len(fixture.projection.sessions) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("cookie", [None, ""])
async def test_missing_cookie_rejection_does_not_depend_on_database_configuration(
    monkeypatch: pytest.MonkeyPatch, cookie: str | None
) -> None:
    fixture = ServiceFixture(monkeypatch)

    def unavailable(_session: Session) -> UUID:
        pytest.fail("未認証の初回GETが開催確認へ到達した")

    monkeypatch.setattr(service_module, "resolve_single_event_id", unavailable)
    cookies = {} if cookie is None else {PARTICIPANT_SESSION_COOKIE_NAME: cookie}
    async with AsyncClient(
        transport=ASGITransport(app=application(fixture.service)),
        base_url="https://test",
        cookies=cookies,
    ) as client:
        response = await client.get("/api/v1/participant/session")
    assert response.status_code == 401
    assert response.json()["code"] == "participant_authentication_required"
    assert response.headers["cache-control"] == "no-store"
    assert fixture.factory.sessions == []
    assert fixture.authentication.sessions == fixture.projection.sessions == []


@pytest.mark.asyncio
async def test_existing_cookie_still_checks_single_event_before_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ServiceFixture(monkeypatch)

    def unavailable(_session: Session) -> UUID:
        raise SingleEventConfigurationError("single_event_configuration_required")

    monkeypatch.setattr(service_module, "resolve_single_event_id", unavailable)
    with pytest.raises(SingleEventConfigurationError):
        await fixture.service.get_participant_state(SESSION_VALUE_FIXTURE)
    assert fixture.factory.events == ["open:0", "close:0"]
    assert fixture.authentication.sessions == fixture.projection.sessions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("other_event", [False, True])
async def test_existing_cookie_cannot_bypass_authentication_or_event_boundary(
    monkeypatch: pytest.MonkeyPatch, other_event: bool
) -> None:
    fixture = ServiceFixture(monkeypatch)
    if other_event:
        fixture.authentication.event_id = UUID(int=99)
    token = SESSION_VALUE_FIXTURE if other_event else "invalid-fixture"
    reason = "participant_session_event_mismatch" if other_event else "participant_session_invalid"
    with pytest.raises(ParticipantSessionInvalidError, match=reason):
        await fixture.service.get_participant_state(token)
    assert fixture.factory.events == ["open:0", "close:0"]
    assert len(fixture.event_sessions) == len(fixture.authentication.sessions) == 1
    assert fixture.projection.sessions == []
