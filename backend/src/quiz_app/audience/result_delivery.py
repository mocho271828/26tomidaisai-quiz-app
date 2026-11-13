"""進行処理を待たせない、最新確定結果一組だけの共有構築。"""

import asyncio
from collections.abc import Callable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from threading import RLock
from time import monotonic
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from quiz_app.audience.auth_service import ParticipantPrincipal
from quiz_app.audience.contracts import (
    ParticipantCommonStateResponse,
    PersonalResultDelivery,
    PersonalResultResponse,
    PersonalResultRevision,
)
from quiz_app.audience.result_batch import ResultBatch, ResultBatchKey, load_result_batch
from quiz_app.domain.errors import DomainInvariantError
from quiz_app.observability.metrics import QuizMetrics, RealtimeBuildOutcome
from quiz_app.observability.realtime_failures import log_realtime_failure


def result_revision(common: ParticipantCommonStateResponse) -> PersonalResultRevision | None:
    published = common.latest_result
    ranking = common.latest_ranking_snapshot_version
    if published is None and ranking is None:
        return None
    return PersonalResultRevision(
        event_id=common.event.event_id,
        event_version=common.event.event_version,
        result_round_id=published.round_id if published is not None else None,
        result_round_version=published.version if published is not None else None,
        ranking_snapshot_version=ranking,
        result_visibility=common.result_visibility,
    )


class PersonalResultService:
    """builder一件と最新要求一件に制限し、callerは完了を待たない。"""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Callable[[], float] = monotonic,
        loader: Callable[[Engine, ResultBatchKey], ResultBatch] = load_result_batch,
    ) -> None:
        self._engine, self._clock, self._loader = engine, clock, loader
        self._lock = RLock()
        self._executor: ThreadPoolExecutor | None = None
        self._loading: Future[ResultBatch] | None = None
        self._batch: ResultBatch | None = None
        self._revision: PersonalResultRevision | None = None
        self._event_id: UUID | None = None
        self._event_version = 0
        self._deferred: set[str] = set()
        self._invalidated = False
        self._closed = False
        self._started = False
        self._retry_at = 0.0
        self._failures = 0
        self._generation = 0
        self._metrics: QuizMetrics | None = None
        self._build_started_at = 0.0
        self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Future[int]]] = set()

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def start(self, metrics: QuizMetrics | None = None) -> None:
        """アプリlifespanだけがbackground DB処理を開始する。"""

        with self._lock:
            if self._closed:
                raise RuntimeError("personal_result_service_closed")
            self._started = True
            self._metrics = metrics
            self._start_if_needed()

    @staticmethod
    def _complete(waiter: asyncio.Future[int], generation: int) -> None:
        if not waiter.done():
            waiter.set_result(generation)

    def _changed(self) -> None:
        self._generation += 1
        for loop, waiter in tuple(self._waiters):
            if not loop.is_closed():
                loop.call_soon_threadsafe(self._complete, waiter, self._generation)

    async def wait_for_change(self, generation: int) -> int:
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[int] = loop.create_future()
        entry = (loop, waiter)
        with self._lock:
            if self._generation != generation or self._closed:
                return self._generation
            self._waiters.add(entry)
        try:
            return await waiter
        finally:
            with self._lock:
                self._waiters.discard(entry)

    def invalidate(self, event_id: UUID) -> None:
        with self._lock:
            if self._event_id not in {None, event_id}:
                return
            self._invalidated = True
            self._changed()

    def set_deferred(self, event_id: UUID, reason: str, deferred: bool) -> None:
        with self._lock:
            if self._event_id not in {None, event_id}:
                return
            was_deferred = bool(self._deferred)
            if deferred:
                self._deferred.add(reason)
            else:
                self._deferred.discard(reason)
            if was_deferred != bool(self._deferred):
                self._changed()
                if self._deferred and self._metrics is not None:
                    self._metrics.record_realtime_build(
                        audience="participant", kind="personal_result", outcome="deferred"
                    )
            self._start_if_needed()

    def _start_if_needed(self) -> None:
        if (
            not self._started
            or self._closed
            or self._invalidated
            or self._deferred
            or self._revision is None
            or self._loading is not None
            or self._clock() < self._retry_at
        ):
            return
        key = ResultBatchKey.from_revision(self._revision)
        if self._batch is not None and self._batch.key == key:
            return
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="personal-result")
        self._build_started_at = self._clock()
        self._loading = self._executor.submit(self._loader, self._engine, key)
        self._loading.add_done_callback(self._finished)

    def _finished(self, future: Future[ResultBatch]) -> None:
        batch = None
        failed = False
        outcome: RealtimeBuildOutcome = "error"
        try:
            batch = future.result()
        except CancelledError:
            outcome = "discarded"
        except (SQLAlchemyError, DomainInvariantError, TimeoutError, ValidationError) as error:
            failed = True
            outcome = "timeout" if isinstance(error, TimeoutError) else "error"
            log_realtime_failure(error, "shared_state", audience="participant")
        finally:
            with self._lock:
                self._loading = None
                if failed:
                    self._failures += 1
                    self._retry_at = self._clock() + min(
                        30.0, 5.0 * 2 ** min(3, self._failures - 1)
                    )
                elif batch is not None:
                    self._failures = 0
                    self._retry_at = 0.0
                    # 完了中に次の結果へ進んだ場合、旧batchを最新として公開しない。
                    if self._revision is not None and batch.key == ResultBatchKey.from_revision(
                        self._revision
                    ):
                        self._batch = batch
                        outcome = "success"
                    else:
                        outcome = "discarded"
                if self._metrics is not None:
                    self._metrics.record_realtime_build(
                        audience="participant",
                        kind="personal_result",
                        outcome=outcome,
                        duration_seconds=max(0.0, self._clock() - self._build_started_at),
                    )
                self._changed()
                if batch is not None:
                    self._start_if_needed()

    def observe(self, common: ParticipantCommonStateResponse) -> None:
        """鮮度確認済みの共通snapshotを受け入れる入口。"""

        with self._lock:
            if self._closed or common.event.event_version < self._event_version:
                return
            if self._event_id not in {None, common.event.event_id}:
                raise DomainInvariantError("personal_result_single_event_mismatch")
            self._event_id = common.event.event_id
            self._event_version = common.event.event_version
            revision = result_revision(common)
            changed = revision != self._revision or self._invalidated
            self._revision = revision
            self._invalidated = False
            if revision is None:
                self._batch = None
            if changed:
                self._changed()
            self._start_if_needed()

    def delivery(self, common: ParticipantCommonStateResponse) -> PersonalResultDelivery:
        revision = result_revision(common)
        with self._lock:
            if (
                self._closed
                or self._invalidated
                or common.event.event_id != self._event_id
                or common.event.event_version != self._event_version
                or revision != self._revision
            ):
                return PersonalResultDelivery(status="unavailable", revision=revision)
            if revision is None:
                return PersonalResultDelivery(status="none", revision=None)
            self._start_if_needed()
            ready = (
                not self._deferred
                and self._batch is not None
                and self._batch.key == ResultBatchKey.from_revision(revision)
            )
            return PersonalResultDelivery(status="ready" if ready else "pending", revision=revision)

    def result_for(
        self, principal: ParticipantPrincipal, common: ParticipantCommonStateResponse
    ) -> PersonalResultResponse:
        with self._lock:
            delivery = self.delivery(common)
            if principal.event_id != common.event.event_id:
                raise DomainInvariantError("personal_result_principal_mismatch")
            payload = None
            if (
                delivery.status == "ready"
                and self._batch is not None
                and delivery.revision is not None
            ):
                payload = self._batch.for_participant(principal, delivery.revision)
            return PersonalResultResponse(delivery=delivery, result=payload)

    async def close(self) -> None:
        with self._lock:
            self._closed = True
            self._batch = None
            self._changed()
            executor = self._executor
        if executor is not None:
            # 待機timeoutだけでは実行中threadは止まらないため、DB期限内の終了を回収する。
            await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)
