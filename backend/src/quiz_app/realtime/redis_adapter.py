import asyncio
import ssl
from collections.abc import Sequence
from functools import lru_cache
from time import perf_counter
from typing import Literal, Protocol
from uuid import UUID

import logfire
from fastapi import Request, WebSocket
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError
from redis import ConnectionPool as SyncConnectionPool
from redis import Redis as SyncRedis
from redis.asyncio import BlockingConnectionPool as AsyncBlockingConnectionPool
from redis.asyncio import ConnectionPool as AsyncConnectionPool
from redis.asyncio import Redis as AsyncRedis
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError

from quiz_app.config import get_settings
from quiz_app.contracts import RedisDependencyStatus
from quiz_app.observability import QuizMetrics
from quiz_app.observability.realtime_failures import log_realtime_failure
from quiz_app.realtime.contracts import InvalidationCause, RealtimeAudience, StateInvalidatedMessage
from quiz_app.realtime.redis_tls import SharedAsyncSSLConnection, SharedSyncSSLConnection

CHANNEL_PREFIX = "quiz:realtime"
ALL_AUDIENCES: tuple[RealtimeAudience, ...] = ("participant", "operator")
OPERATOR_AUDIENCE: tuple[RealtimeAudience, ...] = ("operator",)


class RealtimeUnavailableError(Exception):
    """Redis通知経路を現在利用できない。"""


class RedisSubscribeAcknowledgement(BaseModel):
    """redis-pyが返すsubscribe ACKを厳格に検証する内部契約。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["subscribe"]
    pattern: None
    channel: StrictStr
    data: StrictInt = Field(ge=1)


class RedisPublishClient(Protocol):
    def publish(self, channel: str, message: str) -> int: ...

    def ping(self) -> bool: ...


class AsyncRedisPublishClient(Protocol):
    async def publish(self, channel: str, message: str) -> int: ...


class RealtimeSubscription(Protocol):
    async def next_message(self) -> StateInvalidatedMessage: ...

    async def close(self) -> None: ...


class RealtimeSubscriptionFactory(Protocol):
    async def subscribe(
        self,
        audience: RealtimeAudience,
        event_id: UUID,
    ) -> RealtimeSubscription: ...


def realtime_channel(audience: RealtimeAudience, event_id: UUID) -> str:
    """audienceとeventを混線させないRedis channel名を返す。"""

    return f"{CHANNEL_PREFIX}:{audience}:{event_id}"


def audiences_for_invalidation(cause: InvalidationCause) -> tuple[RealtimeAudience, ...]:
    """行動timingを公開せず必要なprojectionだけを無効化する。"""

    if cause in {"operator_mutation", "automatic_result"}:
        return ALL_AUDIENCES
    return OPERATOR_AUDIENCE


def invalidate_local_common_state(event_id: UUID, audiences: Sequence[RealtimeAudience]) -> None:
    """mutation応答直後のGETにも、同processの旧cacheを返さない。"""

    from quiz_app.audience.projection import get_audience_projection_service
    from quiz_app.operator.projection import get_operator_projection_service

    if "participant" in audiences:
        get_audience_projection_service().invalidate_common_state(event_id)
    if "operator" in audiences:
        get_operator_projection_service().invalidate_common_state(event_id)


class RealtimePublisher:
    """commit済みDB状態のinvalidationをbest effortで通知する。"""

    def __init__(
        self,
        client: RedisPublishClient,
        metrics: QuizMetrics | None = None,
    ) -> None:
        self._client = client
        self._metrics = metrics

    def notify(
        self,
        event_id: UUID,
        audiences: Sequence[RealtimeAudience],
        *,
        cause: InvalidationCause = "operator_mutation",
    ) -> bool:
        invalidate_local_common_state(event_id, audiences)
        is_successful = True
        for audience in audiences:
            message = StateInvalidatedMessage(
                type="state.invalidated",
                audience=audience,
                event_id=event_id,
                cause=cause,
            )
            try:
                self._client.publish(
                    realtime_channel(audience, event_id),
                    message.model_dump_json(),
                )
            except (RedisError, OSError) as error:
                if self._metrics is not None:
                    self._metrics.record_redis_publish(
                        audience=audience,
                        succeeded=False,
                    )
                log_realtime_failure(error, "redis_publish", audience=audience)
                is_successful = False
            else:
                if self._metrics is not None:
                    self._metrics.record_redis_publish(
                        audience=audience,
                        succeeded=True,
                    )
        return is_successful


class AsyncRealtimePublisher:
    """commit後の通知I/Oをevent loop上でawaitし、DB受付成功を維持する。"""

    def __init__(
        self,
        client: AsyncRedisPublishClient,
        metrics: QuizMetrics | None = None,
    ) -> None:
        self._client = client
        self._metrics = metrics

    async def notify(
        self,
        event_id: UUID,
        audiences: Sequence[RealtimeAudience],
        *,
        cause: InvalidationCause = "operator_mutation",
    ) -> bool:
        invalidate_local_common_state(event_id, audiences)
        is_successful = True
        for audience in audiences:
            message = StateInvalidatedMessage(
                type="state.invalidated",
                audience=audience,
                event_id=event_id,
                cause=cause,
            )
            try:
                await self._client.publish(
                    realtime_channel(audience, event_id),
                    message.model_dump_json(),
                )
            except (RedisError, OSError) as error:
                if self._metrics is not None:
                    self._metrics.record_redis_publish(audience=audience, succeeded=False)
                log_realtime_failure(error, "redis_publish", audience=audience)
                is_successful = False
            else:
                if self._metrics is not None:
                    self._metrics.record_redis_publish(audience=audience, succeeded=True)
        return is_successful


class RedisHealthProbe:
    """Redis障害をDB readinessと分離したcoarse statusへ変換する。"""

    def __init__(
        self,
        client: RedisPublishClient,
        metrics: QuizMetrics | None = None,
    ) -> None:
        self._client = client
        self._metrics = metrics

    def status(self) -> RedisDependencyStatus:
        try:
            is_available = self._client.ping()
        except (RedisError, OSError) as error:
            if self._metrics is not None:
                self._metrics.set_redis_dependency_up(False)
            log_realtime_failure(error, "redis_readiness")
            return "degraded"
        is_up = is_available is True
        if self._metrics is not None:
            self._metrics.set_redis_dependency_up(is_up)
        return "ok" if is_up else "degraded"


class RedisRealtimeSubscription:
    def __init__(
        self,
        client: AsyncRedis,
        pubsub: PubSub,
        audience: RealtimeAudience,
        event_id: UUID,
    ) -> None:
        self._client = client
        self._pubsub = pubsub
        self._audience = audience
        self._event_id = event_id

    async def next_message(self) -> StateInvalidatedMessage:
        while True:
            try:
                raw_message: object = await self._pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=None,
                )
            except (RedisError, OSError, RuntimeError) as error:
                raise RealtimeUnavailableError("redis_subscription_lost") from error
            if not isinstance(raw_message, dict):
                continue
            raw_data: object = raw_message.get("data")
            if not isinstance(raw_data, (str, bytes, bytearray)):
                continue
            try:
                message = StateInvalidatedMessage.model_validate_json(raw_data)
            except ValidationError:
                logfire.warning("invalid realtime invalidation discarded")
                continue
            if message.audience != self._audience or message.event_id != self._event_id:
                logfire.warning("cross-channel realtime invalidation discarded")
                continue
            return message

    async def close(self) -> bool:
        return await _close_redis_resources(self._pubsub, self._client)


async def _close_redis_resources(pubsub: PubSub, client: AsyncRedis) -> bool:
    """部分的に確立した購読も両方のresourceを閉じる。"""

    succeeded = True
    try:
        await pubsub.aclose()
    except (RedisError, OSError, RuntimeError) as error:
        succeeded = False
        log_realtime_failure(error, "redis_pubsub_close")
    try:
        await client.aclose()
    except (RedisError, OSError, RuntimeError) as error:
        succeeded = False
        log_realtime_failure(error, "redis_client_close")
    return succeeded


class LocalRealtimeSubscription:
    """最新状態の再取得通知を最大1件だけ保持し、遅い端末を分離する。"""

    def __init__(
        self, factory: "RedisRealtimeSubscriptionFactory", hub: "RedisAudienceHub"
    ) -> None:
        self._factory = factory
        self._hub = hub
        self._pending: asyncio.Queue[StateInvalidatedMessage | None] = asyncio.Queue(maxsize=1)
        self._closed = False

    def deliver(self, message: StateInvalidatedMessage | None) -> None:
        if self._pending.full():
            pending = self._pending.get_nowait()
            # 進行通知を回答数・参加者数の更新で上書きして通常枠へ降格させない。
            if (
                pending is not None
                and message is not None
                and pending.cause not in {"participant_answer", "participant_join"}
                and message.cause in {"participant_answer", "participant_join"}
            ):
                message = pending
            if self._factory.metrics is not None:
                self._factory.metrics.record_redis_delivery_coalesced(self._hub.audience)
        self._pending.put_nowait(message)

    async def next_message(self) -> StateInvalidatedMessage:
        if self._closed:
            raise RealtimeUnavailableError("redis_subscription_closed")
        message = await self._pending.get()
        if message is None:
            raise RealtimeUnavailableError("redis_subscription_lost")
        return message

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        cleanup = asyncio.create_task(self._factory.release(self, self._hub))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # WS取消時も最後のlistenerが所有するRedis FDを回収してから伝播する。
            await cleanup
            raise


class RedisAudienceHub:
    def __init__(
        self,
        source: RedisRealtimeSubscription,
        audience: RealtimeAudience,
        event_id: UUID,
    ) -> None:
        self.source = source
        self.audience = audience
        self.event_id = event_id
        self.listeners: set[LocalRealtimeSubscription] = set()
        self.task: asyncio.Task[None] | None = None
        self.failed = False


class RedisRealtimeSubscriptionFactory:
    """app process・開催・audienceごとにRedis購読を1本だけ維持する。"""

    def __init__(self, metrics: QuizMetrics | None = None) -> None:
        self.metrics = metrics
        self._hubs: dict[tuple[RealtimeAudience, UUID], RedisAudienceHub] = {}
        self._lock = asyncio.Lock()
        self._ssl_context: ssl.SSLContext | None = None
        self._closed = False

    async def start(self) -> None:
        async with self._lock:
            self._closed = False
            await self._initialize_tls_context()

    async def _initialize_tls_context(self) -> None:
        if self._ssl_context is None and get_settings().redis_url.startswith("rediss://"):
            started = perf_counter()
            self._ssl_context = await asyncio.to_thread(ssl.create_default_context)
            if self.metrics is not None:
                self.metrics.record_tls_context_initialization(perf_counter() - started)

    async def subscribe(
        self,
        audience: RealtimeAudience,
        event_id: UUID,
    ) -> LocalRealtimeSubscription:
        async with self._lock:
            if self._closed:
                raise RealtimeUnavailableError("redis_factory_closed")
            key = (audience, event_id)
            hub = self._hubs.get(key)
            if hub is not None and hub.failed:
                await self._stop_hub(hub)
                del self._hubs[key]
                hub = None
            if hub is None:
                source = await self._subscribe_redis(audience, event_id)
                hub = RedisAudienceHub(source, audience, event_id)
                self._hubs[key] = hub
                if self.metrics is not None:
                    self.metrics.change_redis_subscriptions(audience, 1)
                hub.task = asyncio.create_task(self._fan_out(hub), name="redis-audience-listener")
            subscription = LocalRealtimeSubscription(self, hub)
            hub.listeners.add(subscription)
            return subscription

    async def _subscribe_redis(
        self, audience: RealtimeAudience, event_id: UUID
    ) -> RedisRealtimeSubscription:
        settings = get_settings()
        pool = AsyncConnectionPool.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=1,
            socket_connect_timeout=settings.redis_connect_timeout_seconds,
        )
        if settings.redis_url.startswith("rediss://"):
            await self._initialize_tls_context()
            pool.connection_class = SharedAsyncSSLConnection
            pool.connection_kwargs["shared_ssl_context"] = self._ssl_context
        client = AsyncRedis.from_pool(pool)
        pubsub = client.pubsub()
        channel = realtime_channel(audience, event_id)
        try:
            # subscribe書込みとACK読取りを一つの期限へ束ねる。
            async with asyncio.timeout(settings.redis_operation_timeout_seconds):
                await pubsub.subscribe(channel)
                raw_acknowledgement: object = await pubsub.get_message(
                    ignore_subscribe_messages=False,
                    timeout=settings.redis_operation_timeout_seconds,
                )
            acknowledgement = RedisSubscribeAcknowledgement.model_validate(raw_acknowledgement)
            if acknowledgement.channel != channel:
                raise RealtimeUnavailableError("redis_subscribe_ack_channel_mismatch")
        except (asyncio.CancelledError, RealtimeUnavailableError):
            await _close_redis_resources(pubsub, client)
            raise
        except (RedisError, OSError, RuntimeError, ValidationError, TimeoutError) as error:
            await _close_redis_resources(pubsub, client)
            raise RealtimeUnavailableError("redis_subscription_unavailable") from error
        return RedisRealtimeSubscription(client, pubsub, audience, event_id)

    async def _fan_out(self, hub: RedisAudienceHub) -> None:
        try:
            while True:
                message = await hub.source.next_message()
                for listener in tuple(hub.listeners):
                    listener.deliver(message)
        except RealtimeUnavailableError:
            hub.failed = True
            for listener in tuple(hub.listeners):
                listener.deliver(None)

    async def release(self, subscription: LocalRealtimeSubscription, hub: RedisAudienceHub) -> None:
        async with self._lock:
            hub.listeners.discard(subscription)
            key = (hub.audience, hub.event_id)
            if not hub.listeners and self._hubs.get(key) is hub:
                del self._hubs[key]
                await self._stop_hub(hub)

    async def _stop_hub(self, hub: RedisAudienceHub) -> None:
        if hub.task is not None:
            hub.task.cancel()
            await asyncio.gather(hub.task, return_exceptions=True)
        succeeded = await hub.source.close()
        if self.metrics is not None:
            self.metrics.change_redis_subscriptions(hub.audience, -1)
            self.metrics.record_redis_subscription_cleanup(succeeded)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            for hub in self._hubs.values():
                for listener in tuple(hub.listeners):
                    listener.deliver(None)
                await self._stop_hub(hub)
            self._hubs.clear()


@lru_cache
def get_sync_redis_client() -> SyncRedis:
    settings = get_settings()
    pool = SyncConnectionPool.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=settings.redis_connect_timeout_seconds,
        socket_timeout=settings.redis_operation_timeout_seconds,
    )
    if settings.redis_url.startswith("rediss://"):
        pool.connection_class = SharedSyncSSLConnection
        pool.connection_kwargs["shared_ssl_context"] = ssl.create_default_context()
    return SyncRedis.from_pool(pool)


@lru_cache
def get_async_redis_client() -> AsyncRedis:
    """購読接続と分離したpublish poolをprocess内で再利用する。"""

    settings = get_settings()
    pool = AsyncBlockingConnectionPool.from_url(
        settings.redis_url,
        decode_responses=True,
        max_connections=10,
        timeout=settings.redis_operation_timeout_seconds,
        socket_connect_timeout=settings.redis_connect_timeout_seconds,
        socket_timeout=settings.redis_operation_timeout_seconds,
    )
    if settings.redis_url.startswith("rediss://"):
        pool.connection_class = SharedAsyncSSLConnection
        pool.connection_kwargs["shared_ssl_context"] = ssl.create_default_context()
    return AsyncRedis.from_pool(pool)


def _metrics_from_request(request: Request) -> QuizMetrics:
    metrics = request.app.state.quiz_metrics
    if not isinstance(metrics, QuizMetrics):
        raise RuntimeError("application_metrics_not_configured")
    return metrics


def get_realtime_publisher(request: Request) -> RealtimePublisher:
    """Bind Redis publish metrics to the registry owned by the requesting app."""

    dispatcher = getattr(request.app.state, "notification_dispatcher", None)
    if dispatcher is not None:
        from quiz_app.realtime.notification_dispatcher import QueuedRealtimePublisher

        return QueuedRealtimePublisher(dispatcher)
    return RealtimePublisher(get_sync_redis_client(), _metrics_from_request(request))


async def get_async_realtime_publisher(request: Request) -> AsyncRealtimePublisher:
    """async dependencyにより通知準備でthread dispatchを増やさない。"""

    return AsyncRealtimePublisher(get_async_redis_client(), _metrics_from_request(request))


def get_redis_health_probe(request: Request) -> RedisHealthProbe:
    """Bind Redis health metrics to the registry owned by the requesting app."""

    return RedisHealthProbe(get_sync_redis_client(), _metrics_from_request(request))


def get_realtime_subscription_factory(websocket: WebSocket) -> RedisRealtimeSubscriptionFactory:
    return websocket.app.state.realtime_subscription_factory
