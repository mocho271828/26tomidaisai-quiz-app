import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from time import monotonic
from typing import NoReturn
from uuid import UUID

from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from quiz_app.audience.auth_cache import ParticipantAuthenticationCache
from quiz_app.audience.errors import (
    AudienceServiceUnavailableError,
    EventJoinUnavailableError,
    ParticipantSessionInvalidError,
)
from quiz_app.audience.participant_ids import allocate_participant_id
from quiz_app.config import Settings, get_settings
from quiz_app.db import get_engine
from quiz_app.models.core import Event
from quiz_app.models.enums import EventState, PublicLabelKind
from quiz_app.models.identity import Participant, ParticipantSession
from quiz_app.observability.request_timing import record_database_work

JOINABLE_EVENT_STATES = {EventState.LOBBY_OPEN, EventState.IN_PROGRESS}
PARTICIPANT_PUBLIC_LABEL_CONSTRAINT = "uq_participants_event_public_label"


@dataclass(frozen=True, slots=True)
class ParticipantPrincipal:
    participant_id: UUID
    session_id: UUID
    event_id: UUID
    public_label: str
    is_ranking_eligible: bool


@dataclass(frozen=True, slots=True)
class CreatedParticipantSession:
    principal: ParticipantPrincipal
    session_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class VerifiedParticipantAuthentication:
    principal: ParticipantPrincipal
    database_time: datetime
    expires_at: datetime
    verified_from: float


class ParticipantAuthService:
    """匿名participantとopaque DB sessionの境界を扱う。"""

    def __init__(self, engine: Engine, settings: Settings) -> None:
        self._engine = engine
        self._settings = settings
        self._authentication_cache = ParticipantAuthenticationCache()

    def join(
        self,
        event_id: UUID,
        client_join_token: str,
        existing_session_token: str | None,
    ) -> CreatedParticipantSession:
        """再試行を同じ匿名participantへ収束させてsessionを発行する。"""

        with Session(self._engine) as session, session.begin():
            event = session.scalar(
                select(Event).where(Event.id == event_id).with_for_update(read=True)
            )
            if event is None or event.state not in JOINABLE_EVENT_STATES:
                raise EventJoinUnavailableError("event_is_not_joinable")
            database_time = self._database_time(session)
            if existing_session_token is not None:
                resumed = self._resume_cookie_session(
                    session,
                    event.id,
                    existing_session_token,
                    database_time,
                )
                if resumed is not None:
                    return resumed

            session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": self._join_lock_key(event.id, client_join_token)},
            )
            join_attempt_hash = self._hash_join_attempt(event.id, client_join_token)
            participant = session.scalar(
                select(Participant)
                .where(
                    Participant.event_id == event.id,
                    Participant.join_attempt_hash == join_attempt_hash,
                )
                .with_for_update()
            )
            is_new_participant = participant is None
            if is_new_participant:
                participant = Participant(
                    event_id=event.id,
                    public_label=allocate_participant_id(session, event.id),
                    public_label_kind=PublicLabelKind.RANDOM_ID,
                    join_attempt_hash=join_attempt_hash,
                    is_ranking_eligible=True,
                    joined_at=database_time,
                )
                session.add(participant)
                try:
                    session.flush()
                except IntegrityError as error:
                    self._raise_participant_insert_error(error)

            session_token = self._join_session_token(event.id, client_join_token)
            session_token_hash = self._hash_token(session_token)
            expires_at = database_time + timedelta(
                minutes=self._settings.participant_session_ttl_minutes
            )
            if is_new_participant:
                participant_session = ParticipantSession(
                    participant_id=participant.id,
                    token_hash=session_token_hash,
                    issued_at=database_time,
                    expires_at=expires_at,
                    last_seen_at=database_time,
                )
                session.add(participant_session)
                return self._created_session(participant, participant_session, session_token)

            participant_session = session.scalar(
                select(ParticipantSession)
                .where(
                    ParticipantSession.participant_id == participant.id,
                    ParticipantSession.token_hash == session_token_hash,
                )
                .with_for_update()
            )
            if participant_session is None or not self._is_join_retry_active(
                participant,
                participant_session,
                database_time,
            ):
                # 長期の本人復旧は別credentialの責務とし、再試行secretで失効を解除しない。
                raise EventJoinUnavailableError("participant_join_retry_expired")
            participant_session.last_seen_at = database_time
            return self._created_session(participant, participant_session, session_token)

    def authenticate(self, session_token: str) -> ParticipantPrincipal:
        """WS等ではcacheを使わず、DBの現在のparticipant sessionを復元する。"""

        return self.authenticate_realtime(session_token).principal

    def authenticate_realtime(self, session_token: str) -> VerifiedParticipantAuthentication:
        """WSの再認証期限をDB満了と照会開始の単調時計へ束縛する。"""

        with Session(self._engine) as session:
            return self.authenticate_realtime_in_session(session, session_token)

    def authenticate_realtime_in_session(
        self, session: Session, session_token: str
    ) -> VerifiedParticipantAuthentication:
        """caller所有SessionでDBを再確認し、HTTPのpositive cacheは利用しない。"""

        return self._verify_in_session(session, self._hash_token(session_token))

    def authenticate_in_session(self, session: Session, session_token: str) -> ParticipantPrincipal:
        """HTTP要求のcaller所有Sessionと、承認済み最大60秒cacheを使う。"""

        token_hash = self._hash_token(session_token)
        cached = self._authentication_cache.get(token_hash)
        if cached is not None:
            record_database_work("auth_cache_hits")
            return cached
        verified_from = self._authentication_cache.current_time()
        verified = self._verify_in_session(session, token_hash)
        self._authentication_cache.put_verified(
            token_hash,
            verified.principal,
            verified_from,
            verified.database_time,
            verified.expires_at,
        )
        return verified.principal

    @staticmethod
    def _verify_in_session(session: Session, token_hash: str) -> VerifiedParticipantAuthentication:
        verified_from = monotonic()
        row = session.execute(
            select(ParticipantSession, Participant, func.clock_timestamp())
            .join(Participant, Participant.id == ParticipantSession.participant_id)
            .where(
                ParticipantSession.token_hash == token_hash,
                ParticipantSession.revoked_at.is_(None),
                ParticipantSession.expires_at > func.clock_timestamp(),
            )
            .execution_options(populate_existing=True)
        ).one_or_none()
        if row is None:
            raise ParticipantSessionInvalidError("participant_session_invalid")
        participant_session, participant, database_time = row
        if not isinstance(database_time, datetime):
            raise AudienceServiceUnavailableError("database_clock_unavailable")
        if (
            participant_session.revoked_at is not None
            or participant_session.expires_at <= database_time
        ):
            raise ParticipantSessionInvalidError("participant_session_invalid")
        return VerifiedParticipantAuthentication(
            principal=ParticipantPrincipal(
                participant_id=participant.id,
                session_id=participant_session.id,
                event_id=participant.event_id,
                public_label=participant.public_label,
                is_ranking_eligible=True,
            ),
            database_time=database_time,
            expires_at=participant_session.expires_at,
            verified_from=verified_from,
        )

    def _hash_token(self, token: str) -> str:
        key = self._settings.session_secret.get_secret_value().encode()
        return hmac.new(key, token.encode(), hashlib.sha256).hexdigest()

    def _hash_join_attempt(self, event_id: UUID, client_join_token: str) -> str:
        key = self._settings.session_secret.get_secret_value().encode()
        value = f"participant-join:{event_id}:{client_join_token}".encode()
        return hmac.new(key, value, hashlib.sha256).hexdigest()

    def _join_session_token(self, event_id: UUID, client_join_token: str) -> str:
        key = self._settings.session_secret.get_secret_value().encode()
        value = f"participant-session:{event_id}:{client_join_token}".encode()
        return hmac.new(key, value, hashlib.sha256).hexdigest()

    def _resume_cookie_session(
        self,
        session: Session,
        event_id: UUID,
        session_token: str,
        database_time: datetime,
    ) -> CreatedParticipantSession | None:
        row = session.execute(
            select(ParticipantSession, Participant)
            .join(Participant, Participant.id == ParticipantSession.participant_id)
            .where(
                ParticipantSession.token_hash == self._hash_token(session_token),
                Participant.event_id == event_id,
                ParticipantSession.revoked_at.is_(None),
                ParticipantSession.expires_at > database_time,
            )
            .with_for_update(of=ParticipantSession)
        ).one_or_none()
        if row is None:
            return None
        participant_session, participant = row
        is_resumable = (
            participant.event_id == event_id
            and participant_session.revoked_at is None
            and participant_session.expires_at > database_time
        )
        if not is_resumable:
            return None
        participant_session.last_seen_at = database_time
        return self._created_session(participant, participant_session, session_token)

    @staticmethod
    def _created_session(
        participant: Participant,
        participant_session: ParticipantSession,
        session_token: str,
    ) -> CreatedParticipantSession:
        return CreatedParticipantSession(
            principal=ParticipantPrincipal(
                participant_id=participant.id,
                session_id=participant_session.id,
                event_id=participant.event_id,
                public_label=participant.public_label,
                is_ranking_eligible=True,
            ),
            session_token=session_token,
            expires_at=participant_session.expires_at,
        )

    @staticmethod
    def _join_lock_key(event_id: UUID, client_join_token: str) -> int:
        digest = hashlib.sha256(f"{event_id}:{client_join_token}".encode()).digest()
        return int.from_bytes(digest[:8], byteorder="big", signed=True)

    def _is_join_retry_active(
        self,
        participant: Participant,
        participant_session: ParticipantSession,
        database_time: datetime,
    ) -> bool:
        if participant.joined_at is None:
            return False
        retry_expires_at = participant.joined_at + timedelta(
            minutes=self._settings.participant_join_retry_minutes
        )
        return (
            participant_session.revoked_at is None
            and participant_session.expires_at > database_time
            and retry_expires_at > database_time
        )

    @staticmethod
    def _database_time(session: Session) -> datetime:
        database_time = session.scalar(select(func.clock_timestamp()))
        if not isinstance(database_time, datetime):
            raise AudienceServiceUnavailableError("database_clock_unavailable")
        return database_time

    @staticmethod
    def _constraint_name(error: IntegrityError) -> str | None:
        diagnostic = getattr(error.orig, "diag", None)
        constraint_name = getattr(diagnostic, "constraint_name", None)
        return constraint_name if isinstance(constraint_name, str) else None

    @classmethod
    def _raise_participant_insert_error(cls, error: IntegrityError) -> NoReturn:
        if cls._constraint_name(error) == PARTICIPANT_PUBLIC_LABEL_CONSTRAINT:
            # 固定リストの不整合を再採番で隠さず、既存参加者を維持して停止する。
            raise AudienceServiceUnavailableError("participant_id_pool_conflict") from error
        raise error


@lru_cache
def get_participant_auth_service() -> ParticipantAuthService:
    return ParticipantAuthService(get_engine(), get_settings())
