from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from time import monotonic
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quiz_app.audience.auth_service import ParticipantPrincipal

PARTICIPANT_AUTH_CACHE_TTL_SECONDS = 60.0
PARTICIPANT_AUTH_CACHE_MAX_ENTRIES = 10_000


@dataclass(frozen=True, slots=True)
class CachedParticipantAuthentication:
    principal: ParticipantPrincipal
    verified_from: float
    valid_until: float


class ParticipantAuthenticationCache:
    """参加者HTTP認証の正の結果だけを、最大60秒・有限件数で保持する。"""

    def __init__(
        self,
        max_entries: int = PARTICIPANT_AUTH_CACHE_MAX_ENTRIES,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if not 1 <= max_entries <= PARTICIPANT_AUTH_CACHE_MAX_ENTRIES:
            raise ValueError("participant authentication cache capacity is out of bounds")
        self._max_entries = max_entries
        self._clock = clock
        self._lock = Lock()
        self._entries: OrderedDict[str, CachedParticipantAuthentication] = OrderedDict()

    def current_time(self) -> float:
        return self._clock()

    def get(self, token_hash: str) -> ParticipantPrincipal | None:
        with self._lock:
            entry = self._entries.get(token_hash)
            if entry is None:
                return None
            if self._clock() >= entry.valid_until:
                del self._entries[token_hash]
                return None
            self._entries.move_to_end(token_hash)
            return entry.principal

    def put_verified(
        self,
        token_hash: str,
        principal: ParticipantPrincipal,
        verified_from: float,
        database_time: datetime,
        expires_at: datetime,
    ) -> None:
        # DB時刻取得前を起点にするので、照会待ちの時間をTTLへ加算しない。
        lifetime = min(
            PARTICIPANT_AUTH_CACHE_TTL_SECONDS,
            (expires_at - database_time).total_seconds(),
        )
        valid_until = verified_from + lifetime
        with self._lock:
            if self._clock() >= valid_until:
                return
            previous = self._entries.get(token_hash)
            if previous is not None and previous.verified_from >= verified_from:
                return
            self._entries[token_hash] = CachedParticipantAuthentication(
                principal=principal,
                verified_from=verified_from,
                valid_until=valid_until,
            )
            self._entries.move_to_end(token_hash)
            if len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
