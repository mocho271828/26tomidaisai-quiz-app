from datetime import UTC, datetime, timedelta
from typing import Self, cast
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

import quiz_app.audience.auth_service as auth_service_module
from quiz_app.audience.auth_service import ParticipantAuthService
from quiz_app.audience.errors import (
    AudienceServiceUnavailableError,
    EventJoinUnavailableError,
)
from quiz_app.config import Settings
from quiz_app.models.core import Event
from quiz_app.models.enums import EventState, PublicLabelKind
from quiz_app.models.identity import Participant, ParticipantSession

EVENT_ID = UUID("70000000-0000-4000-8000-000000000001")
OTHER_EVENT_ID = UUID("70000000-0000-4000-8000-000000000002")
PARTICIPANT_ID = UUID("70000000-0000-4000-8000-000000000003")
SESSION_ID = UUID("70000000-0000-4000-8000-000000000004")
CLIENT_JOIN_VALUE_FIXTURE = "A" * 43
NOW = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
SESSION_VALUE_FIXTURE = "existing-participant-session"
PUBLIC_LABEL = "ABC"


class FakeRowResult:
    def __init__(
        self,
        participant_session: ParticipantSession,
        participant: Participant,
    ) -> None:
        self._row = (participant_session, participant)

    def one_or_none(self) -> tuple[ParticipantSession, Participant]:
        return self._row


class FakeAuthenticationRowResult:
    def __init__(
        self,
        participant_session: ParticipantSession,
        participant: Participant,
    ) -> None:
        self._row = (participant_session, participant, NOW)

    def one_or_none(self) -> tuple[ParticipantSession, Participant, datetime]:
        return self._row


class FakeResumeSession:
    def __init__(
        self,
        participant_session: ParticipantSession,
        participant: Participant,
    ) -> None:
        self._result = FakeRowResult(participant_session, participant)

    def execute(self, _: object) -> FakeRowResult:
        return self._result


class MissingDatabaseClockSession:
    def scalar(self, _: object) -> None:
        return None


class ScriptedJoinSession:
    def __init__(
        self,
        scalar_values: list[object],
        flush_error: IntegrityError | None = None,
    ) -> None:
        self.scalar_values = scalar_values
        self.flush_error = flush_error
        self.added: list[object] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def begin(self) -> "ScriptedJoinSession":
        return self

    def scalar(self, _: object) -> object:
        return self.scalar_values.pop(0)

    def execute(self, _: object, __: object | None = None) -> object:
        return object()

    def add(self, value: object) -> None:
        self.added.append(value)

    def flush(self) -> None:
        if self.flush_error is not None:
            raise self.flush_error


def _auth_service() -> ParticipantAuthService:
    return ParticipantAuthService(cast(Engine, object()), Settings(_env_file=None))


def _participant() -> Participant:
    return Participant(
        id=PARTICIPANT_ID,
        event_id=EVENT_ID,
        public_label=PUBLIC_LABEL,
        is_ranking_eligible=True,
        joined_at=NOW,
    )


def _participant_session(service: ParticipantAuthService, token: str) -> ParticipantSession:
    return ParticipantSession(
        id=SESSION_ID,
        participant_id=PARTICIPANT_ID,
        token_hash=service._hash_token(token),
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
        last_seen_at=NOW - timedelta(seconds=30),
    )


def _event() -> Event:
    return Event(
        id=EVENT_ID,
        slug="demo",
        title="Demo",
        state=EventState.LOBBY_OPEN,
        scoring_policy_version="scoring-test-v1",
        display_name_policy_version="display-name-test-v1",
    )


@pytest.mark.parametrize("restart_api", [False, True])
def test_committed_join_with_lost_response_returns_same_id_without_allocating(
    monkeypatch: pytest.MonkeyPatch,
    restart_api: bool,
) -> None:
    service = _auth_service()
    participant = _participant()
    expected_session_token = service._join_session_token(
        EVENT_ID,
        CLIENT_JOIN_VALUE_FIXTURE,
    )
    participant_session = _participant_session(service, expected_session_token)
    database_session = ScriptedJoinSession([_event(), NOW, participant, participant_session])
    monkeypatch.setattr(auth_service_module, "Session", lambda _: database_session)
    if restart_api:
        # 別API processでも同じ設定とDB保存済みidentityから復元できる。
        service = _auth_service()

    created = service.join(
        EVENT_ID,
        CLIENT_JOIN_VALUE_FIXTURE,
        None,
    )

    assert created.principal.public_label == PUBLIC_LABEL
    assert created.principal.participant_id == PARTICIPANT_ID
    assert created.principal.session_id == SESSION_ID
    assert created.session_token == expected_session_token
    assert participant_session.last_seen_at == NOW
    assert database_session.added == []
    assert database_session.scalar_values == []


@pytest.mark.parametrize("expired", [False, True])
def test_retry_cannot_replace_revoked_or_expired_identity(
    monkeypatch: pytest.MonkeyPatch, expired: bool
) -> None:
    service = _auth_service()
    participant = _participant()
    saved_session = _participant_session(service, SESSION_VALUE_FIXTURE)
    if expired:
        participant.joined_at = NOW - timedelta(minutes=6)
    else:
        saved_session.revoked_at = NOW
    database_session = ScriptedJoinSession([_event(), NOW, participant, saved_session])
    monkeypatch.setattr(auth_service_module, "Session", lambda _: database_session)

    with pytest.raises(EventJoinUnavailableError, match="participant_join_retry_expired"):
        service.join(EVENT_ID, CLIENT_JOIN_VALUE_FIXTURE, None)

    assert database_session.added == []
    assert database_session.scalar_values == []


def test_same_event_cookie_resume_preserves_identity_and_token() -> None:
    service = _auth_service()
    participant_session = _participant_session(service, SESSION_VALUE_FIXTURE)
    session = FakeResumeSession(participant_session, _participant())

    resumed = service._resume_cookie_session(
        cast(Session, session),
        EVENT_ID,
        SESSION_VALUE_FIXTURE,
        NOW,
    )

    assert resumed is not None
    assert resumed.session_token == SESSION_VALUE_FIXTURE
    assert resumed.principal.participant_id == PARTICIPANT_ID
    assert resumed.principal.session_id == SESSION_ID
    assert participant_session.revoked_at is None
    assert participant_session.last_seen_at == NOW


def test_cookie_from_other_event_is_not_revoked_or_resumed() -> None:
    service = _auth_service()
    participant_session = _participant_session(service, SESSION_VALUE_FIXTURE)
    session = FakeResumeSession(participant_session, _participant())

    resumed = service._resume_cookie_session(
        cast(Session, session),
        OTHER_EVENT_ID,
        SESSION_VALUE_FIXTURE,
        NOW,
    )

    assert resumed is None
    assert participant_session.revoked_at is None
    assert participant_session.last_seen_at == NOW - timedelta(seconds=30)


class ConstraintDiagnostic:
    def __init__(self, constraint_name: str) -> None:
        self.constraint_name = constraint_name


class ConstraintViolation(Exception):
    def __init__(self, constraint_name: str) -> None:
        super().__init__(constraint_name)
        self.diag = ConstraintDiagnostic(constraint_name)


def _integrity_error(constraint_name: str) -> IntegrityError:
    return IntegrityError(
        "INSERT INTO participants ...",
        {},
        ConstraintViolation(constraint_name),
    )


def test_pool_collision_stops_without_retry_or_changing_existing_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        auth_service_module, "allocate_participant_id", lambda _session, _event: PUBLIC_LABEL
    )
    service = _auth_service()
    database_session = ScriptedJoinSession(
        [_event(), NOW, None],
        _integrity_error("uq_participants_event_public_label"),
    )
    monkeypatch.setattr(auth_service_module, "Session", lambda _: database_session)

    with pytest.raises(AudienceServiceUnavailableError, match="participant_id_pool_conflict"):
        service.join(
            EVENT_ID,
            CLIENT_JOIN_VALUE_FIXTURE,
            None,
        )

    assert len(database_session.added) == 1
    participant = database_session.added[0]
    assert isinstance(participant, Participant)
    assert participant.public_label == PUBLIC_LABEL
    assert participant.public_label_kind is PublicLabelKind.RANDOM_ID


def test_public_label_unique_violation_is_pool_unavailability() -> None:
    error = _integrity_error("uq_participants_event_public_label")

    with pytest.raises(AudienceServiceUnavailableError, match="participant_id_pool_conflict"):
        ParticipantAuthService._raise_participant_insert_error(error)


def test_unrelated_participant_integrity_error_is_not_translated() -> None:
    error = _integrity_error("uq_participants_event_join_attempt_hash")

    with pytest.raises(IntegrityError) as error_info:
        ParticipantAuthService._raise_participant_insert_error(error)

    assert error_info.value is error


def test_join_retry_material_is_deterministic_and_domain_separated() -> None:
    service = _auth_service()

    join_hash = service._hash_join_attempt(EVENT_ID, CLIENT_JOIN_VALUE_FIXTURE)
    session_token = service._join_session_token(EVENT_ID, CLIENT_JOIN_VALUE_FIXTURE)

    assert join_hash == service._hash_join_attempt(EVENT_ID, CLIENT_JOIN_VALUE_FIXTURE)
    assert session_token == service._join_session_token(EVENT_ID, CLIENT_JOIN_VALUE_FIXTURE)
    assert join_hash != session_token
    assert session_token != service._join_session_token(
        OTHER_EVENT_ID,
        CLIENT_JOIN_VALUE_FIXTURE,
    )


@pytest.mark.parametrize("event_state", [EventState.LOBBY_OPEN, EventState.IN_PROGRESS])
def test_join_is_ranking_eligible_regardless_of_event_progress(
    monkeypatch: pytest.MonkeyPatch, event_state: EventState
) -> None:
    monkeypatch.setattr(
        auth_service_module, "allocate_participant_id", lambda _session, _event: PUBLIC_LABEL
    )
    service = _auth_service()
    event = _event()
    event.state = event_state
    database_session = ScriptedJoinSession([event, NOW, None])
    monkeypatch.setattr(auth_service_module, "Session", lambda _: database_session)

    created = service.join(EVENT_ID, CLIENT_JOIN_VALUE_FIXTURE, None)

    assert created.principal.is_ranking_eligible is True
    participant = database_session.added[0]
    assert isinstance(participant, Participant)
    assert participant.is_ranking_eligible is True


def test_resuming_former_late_joiner_keeps_identity_and_enables_ranking() -> None:
    service = _auth_service()
    participant = _participant()
    participant.is_ranking_eligible = False
    participant_session = _participant_session(service, SESSION_VALUE_FIXTURE)
    session = FakeResumeSession(participant_session, participant)

    resumed = service._resume_cookie_session(
        cast(Session, session), EVENT_ID, SESSION_VALUE_FIXTURE, NOW
    )

    assert resumed is not None
    assert resumed.principal.participant_id == participant.id
    assert resumed.principal.is_ranking_eligible is True
    assert participant.is_ranking_eligible is False


def test_authenticating_former_late_joiner_enables_ranking_without_updating_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _auth_service()
    participant = _participant()
    participant.is_ranking_eligible = False
    participant_session = _participant_session(service, SESSION_VALUE_FIXTURE)
    database_session = ScriptedJoinSession([NOW])
    monkeypatch.setattr(auth_service_module, "Session", lambda _: database_session)
    monkeypatch.setattr(
        database_session,
        "execute",
        lambda _: FakeAuthenticationRowResult(participant_session, participant),
    )

    principal = service.authenticate(SESSION_VALUE_FIXTURE)

    assert principal.participant_id == participant.id
    assert principal.is_ranking_eligible is True
    assert participant.is_ranking_eligible is False


def test_database_clock_failure_is_not_classified_as_invalid_session() -> None:
    with pytest.raises(AudienceServiceUnavailableError):
        ParticipantAuthService._database_time(
            cast(Session, MissingDatabaseClockSession()),
        )


def test_join_retry_never_reactivates_revoked_or_old_identity() -> None:
    service = _auth_service()
    participant = _participant()
    participant_session = _participant_session(service, SESSION_VALUE_FIXTURE)

    assert service._is_join_retry_active(participant, participant_session, NOW) is True

    participant_session.revoked_at = NOW
    assert service._is_join_retry_active(participant, participant_session, NOW) is False

    participant_session.revoked_at = None
    participant.joined_at = NOW - timedelta(minutes=6)
    assert service._is_join_retry_active(participant, participant_session, NOW) is False
