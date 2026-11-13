from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError
from sqlmodel import Session

import quiz_app.audience.auth_service as auth_service_module
from quiz_app.audience.auth_cache import ParticipantAuthenticationCache
from quiz_app.audience.auth_service import ParticipantAuthService, ParticipantPrincipal
from quiz_app.audience.errors import AudienceServiceUnavailableError, ParticipantSessionInvalidError
from quiz_app.config import Settings
from quiz_app.models.identity import Participant, ParticipantSession
from quiz_app.observability.request_timing import RequestTiming, current_request_timing

NOW = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
EVENT_ID = UUID("70000000-0000-4000-8000-000000000001")
PARTICIPANT_ID = UUID("70000000-0000-4000-8000-000000000002")
SESSION_ID = UUID("70000000-0000-4000-8000-000000000003")
SESSION_VALUE_FIXTURE = "participant-cache-token-fixture"
SESSION_DIGEST_FIXTURE = "fixture-hash"


@dataclass
class ControlledMonotonicClock:
    value: float = 0.0

    def __call__(self) -> float:
        return self.value


class AuthenticationRowResult:
    def __init__(self, row: tuple[ParticipantSession, Participant, object] | None) -> None:
        self._row = row

    def one_or_none(self) -> tuple[ParticipantSession, Participant, object] | None:
        return self._row


class AuthenticationSession:
    def __init__(self, clock: ControlledMonotonicClock) -> None:
        self.clock = clock
        self.calls = 0
        self.statement: object | None = None
        self.before_result: Callable[[], None] = lambda: None
        self.error: OperationalError | None = None
        self.clock_unavailable = False
        self.participant = Participant(
            id=PARTICIPANT_ID,
            event_id=EVENT_ID,
            public_label="ABC",
            is_ranking_eligible=True,
        )
        self.credential = ParticipantSession(
            id=SESSION_ID,
            participant_id=PARTICIPANT_ID,
            token_hash=SESSION_DIGEST_FIXTURE,
            issued_at=NOW,
            expires_at=NOW + timedelta(seconds=600),
        )

    def execute(self, statement: object) -> AuthenticationRowResult:
        self.calls += 1
        self.statement = statement
        self.before_result()
        if self.error is not None:
            raise self.error
        database_time = (
            None if self.clock_unavailable else NOW + timedelta(seconds=self.clock.value)
        )
        return AuthenticationRowResult((self.credential, self.participant, database_time))


def service_and_session() -> tuple[
    ParticipantAuthService, AuthenticationSession, ControlledMonotonicClock
]:
    clock = ControlledMonotonicClock()
    service = ParticipantAuthService(cast(Engine, object()), Settings(_env_file=None))
    service._authentication_cache = ParticipantAuthenticationCache(clock=clock)
    return service, AuthenticationSession(clock), clock


def authenticate(
    service: ParticipantAuthService, session: AuthenticationSession
) -> ParticipantPrincipal:
    return service.authenticate_in_session(cast(Session, session), SESSION_VALUE_FIXTURE)


def test_http_authentication_cache_is_positive_hashed_and_does_not_extend_on_hits() -> None:
    service, session, clock = service_and_session()
    principal = authenticate(service, session)

    clock.value = 59.999
    assert authenticate(service, session) is principal
    assert session.calls == 1
    assert list(service._authentication_cache._entries) == [
        service._hash_token(SESSION_VALUE_FIXTURE)
    ]
    assert SESSION_VALUE_FIXTURE not in repr(service._authentication_cache._entries)

    clock.value = 60.0
    assert authenticate(service, session) == principal
    assert session.calls == 2


def test_only_actual_http_cache_hits_increment_request_work_without_identity() -> None:
    service, session, _clock = service_and_session()
    timing = RequestTiming()
    context_token = current_request_timing.set(timing)
    try:
        authenticate(service, session)
        assert timing.database_work == {}
        authenticate(service, session)
        assert timing.database_work == {"auth_cache_hits": 1}
        timing.finish_response()
        authenticate(service, session)
        assert timing.database_work == {"auth_cache_hits": 1}
    finally:
        current_request_timing.reset(context_token)


def test_distinct_tokens_never_share_participant_principals() -> None:
    service, first_session, clock = service_and_session()
    second_session = AuthenticationSession(clock)
    second_session.participant.id = UUID("70000000-0000-4000-8000-000000000004")
    second_session.credential.id = UUID("70000000-0000-4000-8000-000000000005")
    second_session.participant.public_label = "DEF"
    second_value_fixture = "second-participant-cookie-fixture"
    first = authenticate(service, first_session)
    second = service.authenticate_in_session(cast(Session, second_session), second_value_fixture)

    assert first.participant_id != second.participant_id
    assert authenticate(service, first_session) is first
    assert (
        service.authenticate_in_session(cast(Session, second_session), second_value_fixture)
        is second
    )
    assert first_session.calls == second_session.calls == 1


def test_missing_session_is_not_cached() -> None:
    service, session, _clock = service_and_session()

    class MissingSession:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, _: object) -> AuthenticationRowResult:
            self.calls += 1
            return AuthenticationRowResult(None)

    missing = MissingSession()
    for _ in range(2):
        with pytest.raises(ParticipantSessionInvalidError):
            service.authenticate_in_session(cast(Session, missing), SESSION_VALUE_FIXTURE)
    assert missing.calls == 2
    assert authenticate(service, session).participant_id == PARTICIPANT_ID


def test_revocation_is_reflected_at_the_sixty_second_boundary() -> None:
    service, session, clock = service_and_session()
    principal = authenticate(service, session)
    session.credential.revoked_at = NOW

    clock.value = 59.999
    assert authenticate(service, session) is principal
    clock.value = 60.0
    with pytest.raises(ParticipantSessionInvalidError):
        authenticate(service, session)
    assert session.calls == 2

    session.credential.revoked_at = None
    assert authenticate(service, session) == principal
    assert session.calls == 3


def test_participant_changes_after_sixty_seconds_do_not_mutate_cached_principal() -> None:
    service, session, clock = service_and_session()
    initial = authenticate(service, session)
    session.participant.public_label = "DEF"

    clock.value = 59.999
    assert authenticate(service, session) is initial
    assert initial.public_label == "ABC"

    clock.value = 60.0
    updated = authenticate(service, session)
    assert updated.public_label == "DEF"
    assert session.calls == 2


def test_session_expiry_before_cache_ttl_is_never_extended() -> None:
    service, session, clock = service_and_session()
    session.credential.expires_at = NOW + timedelta(seconds=0.25)
    principal = authenticate(service, session)

    clock.value = 0.249
    assert authenticate(service, session) is principal
    clock.value = 0.25
    with pytest.raises(ParticipantSessionInvalidError):
        authenticate(service, session)
    assert session.calls == 2


@pytest.mark.parametrize("remaining_seconds", [0, -0.01])
def test_expired_authentication_is_not_cached(remaining_seconds: float) -> None:
    service, session, _clock = service_and_session()
    session.credential.expires_at = NOW + timedelta(seconds=remaining_seconds)

    for _ in range(2):
        with pytest.raises(ParticipantSessionInvalidError):
            authenticate(service, session)
    assert session.calls == 2
    assert not service._authentication_cache._entries


def test_slow_miss_consumes_cache_lifetime_before_database_result() -> None:
    service, session, clock = service_and_session()
    session.before_result = lambda: setattr(clock, "value", 0.8)
    principal = authenticate(service, session)
    session.before_result = lambda: None

    clock.value = 59.999
    assert authenticate(service, session) is principal
    clock.value = 60.0
    authenticate(service, session)
    assert session.calls == 2


def test_miss_taking_longer_than_ttl_is_not_cached() -> None:
    service, session, clock = service_and_session()
    session.before_result = lambda: setattr(clock, "value", clock.value + 60.1)

    authenticate(service, session)
    authenticate(service, session)
    assert session.calls == 2
    assert not service._authentication_cache._entries


def test_expiry_conversion_is_conservative_for_delayed_database_time() -> None:
    service, session, clock = service_and_session()
    session.credential.expires_at = NOW + timedelta(seconds=0.9)
    session.before_result = lambda: setattr(clock, "value", 0.6)

    authenticate(service, session)
    assert not service._authentication_cache._entries


def test_database_errors_and_missing_clock_are_not_cached() -> None:
    service, session, _clock = service_and_session()
    session.error = OperationalError("SELECT", None, RuntimeError("database unavailable"))
    for _ in range(2):
        with pytest.raises(OperationalError):
            authenticate(service, session)
    session.error = None
    session.clock_unavailable = True
    with pytest.raises(AudienceServiceUnavailableError):
        authenticate(service, session)
    session.clock_unavailable = False
    authenticate(service, session)
    assert session.calls == 4


def test_cache_does_not_hold_its_lock_during_database_access() -> None:
    service, session, _clock = service_and_session()

    def verify_lock_is_free() -> None:
        assert service._authentication_cache._lock.acquire(blocking=False)
        service._authentication_cache._lock.release()

    session.before_result = verify_lock_is_free
    authenticate(service, session)


def test_websocket_authentication_does_not_use_or_refresh_http_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, session, clock = service_and_session()
    authenticate(service, session)
    session.credential.revoked_at = NOW

    class SessionContext:
        def __enter__(self) -> AuthenticationSession:
            return session

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(auth_service_module, "Session", lambda _: SessionContext())
    with pytest.raises(ParticipantSessionInvalidError):
        service.authenticate(SESSION_VALUE_FIXTURE)
    clock.value = 60.0
    with pytest.raises(ParticipantSessionInvalidError):
        authenticate(service, session)
    assert session.calls == 3


def test_realtime_verification_returns_expiry_and_query_start_without_populating_http_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, session, clock = service_and_session()
    clock.value = 1.0
    session.before_result = lambda: setattr(clock, "value", 2.3)

    class SessionContext:
        def __enter__(self) -> AuthenticationSession:
            return session

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(auth_service_module, "Session", lambda _: SessionContext())
    monkeypatch.setattr(auth_service_module, "monotonic", clock)
    verified = service.authenticate_realtime(SESSION_VALUE_FIXTURE)

    assert verified.verified_from == 1.0
    assert verified.database_time == NOW + timedelta(seconds=2.3)
    assert verified.expires_at == session.credential.expires_at
    assert verified.principal.participant_id == PARTICIPANT_ID
    assert session.calls == 1
    assert not service._authentication_cache._entries


def test_realtime_caller_session_ignores_positive_cache_and_preserves_fresh_expiry() -> None:
    service, session, _clock = service_and_session()
    authenticate(service, session)
    entries_before = service._authentication_cache._entries.copy()
    session.credential.expires_at = NOW + timedelta(seconds=1)
    verified = service.authenticate_realtime_in_session(
        cast(Session, session), SESSION_VALUE_FIXTURE
    )
    assert verified.expires_at == NOW + timedelta(seconds=1)
    assert session.calls == 2
    assert service._authentication_cache._entries == entries_before
    session.credential.revoked_at = NOW
    with pytest.raises(ParticipantSessionInvalidError):
        service.authenticate_realtime_in_session(cast(Session, session), SESSION_VALUE_FIXTURE)
    assert session.calls == 3
    assert service._authentication_cache._entries == entries_before


def test_authentication_clock_and_credential_use_one_query() -> None:
    service, session, _clock = service_and_session()
    authenticate(service, session)
    assert session.calls == 1
    statement = session.statement
    assert statement is not None
    compiled = statement.compile(dialect=postgresql.dialect())
    assert "clock_timestamp()" in str(compiled)
    assert "participant_sessions.revoked_at IS NULL" in str(compiled)
    assert "participant_sessions.expires_at > clock_timestamp()" in str(compiled)
    assert SESSION_VALUE_FIXTURE not in compiled.params.values()


def test_cache_capacity_is_bounded_and_uses_lru_eviction() -> None:
    service, session, clock = service_and_session()
    principal = authenticate(service, session)
    cache = ParticipantAuthenticationCache(max_entries=2, clock=clock)
    hashes = [service._hash_token(f"token-{index}") for index in range(3)]
    for token_hash in hashes[:2]:
        cache.put_verified(token_hash, principal, 0.0, NOW, NOW + timedelta(seconds=60))
    assert cache.get(hashes[0]) is principal
    cache.put_verified(hashes[2], principal, 0.0, NOW, NOW + timedelta(seconds=60))

    assert cache.get(hashes[1]) is None
    assert cache.get(hashes[0]) is principal
    assert cache.get(hashes[2]) is principal
    assert len(cache._entries) == 2


@pytest.mark.parametrize("capacity", [0, -1, 10_001])
def test_cache_capacity_rejects_unbounded_configuration(capacity: int) -> None:
    with pytest.raises(ValueError, match="out of bounds"):
        ParticipantAuthenticationCache(max_entries=capacity)


def test_late_older_miss_does_not_overwrite_newer_principal() -> None:
    service, session, clock = service_and_session()
    principal = authenticate(service, session)
    updated = replace(principal, public_label="DEF")
    cache = ParticipantAuthenticationCache(clock=clock)
    token_hash = service._hash_token(SESSION_VALUE_FIXTURE)
    clock.value = 0.3
    cache.put_verified(token_hash, updated, 0.2, NOW, NOW + timedelta(seconds=60))
    cache.put_verified(token_hash, principal, 0.1, NOW, NOW + timedelta(seconds=60))

    assert cache.get(token_hash) is updated
