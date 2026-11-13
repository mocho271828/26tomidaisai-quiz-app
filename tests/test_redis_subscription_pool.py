"""開催・audience単位の購読共有とTLS初期化の回帰。"""

import asyncio
from collections.abc import Sequence
from typing import cast
from uuid import UUID

import pytest
from redis.asyncio import Redis as AsyncRedis
from redis.asyncio.client import PubSub
from redis.exceptions import ConnectionError as RedisConnectionError

from quiz_app.config import Settings
from quiz_app.realtime.contracts import InvalidationCause, StateInvalidatedMessage
from quiz_app.realtime.redis_adapter import (
    RealtimeUnavailableError,
    RedisRealtimeSubscriptionFactory,
    realtime_channel,
)

EVENT_ID = UUID("41000000-0000-4000-8000-000000000001")


class AcknowledgingPubSub:
    def __init__(self, messages: Sequence[object]) -> None:
        self._messages = list(messages)
        self.subscribed_channels: list[str] = []
        self.get_message_calls: list[tuple[bool, float | None]] = []
        self.is_closed = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed_channels.append(channel)

    async def get_message(
        self,
        *,
        ignore_subscribe_messages: bool,
        timeout: float | None,  # noqa: ASYNC109 - Redis APIのkeyword契約を模倣する。
    ) -> object:
        self.get_message_calls.append((ignore_subscribe_messages, timeout))
        if not self._messages:
            await asyncio.Event().wait()
        return self._messages.pop(0)

    async def aclose(self) -> None:
        self.is_closed = True


class FactoryRedisClient:
    def __init__(self, pubsub: AcknowledgingPubSub) -> None:
        self._pubsub = pubsub
        self.is_closed = False

    def pubsub(self) -> PubSub:
        return cast(PubSub, self._pubsub)

    async def aclose(self) -> None:
        self.is_closed = True


def install_async_redis_factory(
    monkeypatch: pytest.MonkeyPatch,
    pubsub: AcknowledgingPubSub,
) -> tuple[Settings, FactoryRedisClient]:
    settings = Settings(_env_file=None)
    client = FactoryRedisClient(pubsub)

    def from_pool(_: object) -> AsyncRedis:
        return cast(AsyncRedis, client)

    monkeypatch.setattr(AsyncRedis, "from_pool", staticmethod(from_pool))
    monkeypatch.setattr("quiz_app.realtime.redis_adapter.get_settings", lambda: settings)
    return settings, client


@pytest.mark.asyncio
async def test_one_hundred_local_subscriptions_share_one_redis_connection_and_release_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = realtime_channel("participant", EVENT_ID)
    pubsub = AcknowledgingPubSub(
        [{"type": "subscribe", "pattern": None, "channel": channel, "data": 1}]
    )
    _, client = install_async_redis_factory(monkeypatch, pubsub)
    factory = RedisRealtimeSubscriptionFactory()
    subscriptions = await asyncio.gather(
        *(factory.subscribe("participant", EVENT_ID) for _ in range(100))
    )
    assert pubsub.subscribed_channels == [channel]
    message = StateInvalidatedMessage(
        type="state.invalidated", audience="participant", event_id=EVENT_ID
    )
    for subscription in subscriptions:
        subscription.deliver(message)
        subscription.deliver(message)
    assert await asyncio.gather(*(item.next_message() for item in subscriptions)) == [message] * 100
    await asyncio.gather(*(item.close() for item in subscriptions[:-1]))
    assert not pubsub.is_closed
    await subscriptions[-1].close()
    assert pubsub.is_closed and client.is_closed
    await subscriptions[-1].close()
    await factory.close()


@pytest.mark.asyncio
async def test_factory_failure_wakes_every_listener_and_shutdown_reclaims_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = realtime_channel("participant", EVENT_ID)
    pubsub = AcknowledgingPubSub(
        [{"type": "subscribe", "pattern": None, "channel": channel, "data": 1}]
    )
    _, client = install_async_redis_factory(monkeypatch, pubsub)
    factory = RedisRealtimeSubscriptionFactory()
    before = asyncio.all_tasks()
    subscriptions = [await factory.subscribe("participant", EVENT_ID) for _ in range(10)]
    await factory.close()
    for subscription in subscriptions:
        with pytest.raises(RealtimeUnavailableError):
            await subscription.next_message()
        await subscription.close()
    assert pubsub.is_closed and client.is_closed
    assert asyncio.all_tasks() <= before
    with pytest.raises(RealtimeUnavailableError, match="redis_factory_closed"):
        await factory.subscribe("participant", EVENT_ID)


@pytest.mark.asyncio
async def test_cancelled_subscribe_reclaims_partially_open_redis_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pubsub = AcknowledgingPubSub([])
    _, client = install_async_redis_factory(monkeypatch, pubsub)
    factory = RedisRealtimeSubscriptionFactory()
    task = asyncio.create_task(factory.subscribe("participant", EVENT_ID))
    await asyncio.sleep(0)
    assert pubsub.subscribed_channels
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pubsub.is_closed and client.is_closed
    await factory.close()


@pytest.mark.asyncio
async def test_lazy_tls_initialization_does_not_replace_lock_or_duplicate_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ssl

    from redis.asyncio.connection import ConnectionPool

    from quiz_app.realtime.redis_tls import SharedAsyncSSLConnection

    channel = realtime_channel("participant", EVENT_ID)
    pubsub = AcknowledgingPubSub(
        [{"type": "subscribe", "pattern": None, "channel": channel, "data": 1}]
    )
    settings, client = install_async_redis_factory(monkeypatch, pubsub)
    tls_settings = settings.model_copy(update={"redis_url": "rediss://redis:6379/0"})
    monkeypatch.setattr("quiz_app.realtime.redis_adapter.get_settings", lambda: tls_settings)
    context = ssl.create_default_context()
    generated: list[ssl.SSLContext] = []
    pools: list[ConnectionPool] = []

    def generate_context() -> ssl.SSLContext:
        generated.append(context)
        return context

    def from_pool(pool: ConnectionPool) -> AsyncRedis:
        pools.append(pool)
        return cast(AsyncRedis, client)

    monkeypatch.setattr(
        "quiz_app.realtime.redis_adapter.ssl.create_default_context", generate_context
    )
    monkeypatch.setattr(AsyncRedis, "from_pool", staticmethod(from_pool))
    factory = RedisRealtimeSubscriptionFactory()
    subscriptions = await asyncio.gather(
        *(factory.subscribe("participant", EVENT_ID) for _ in range(100))
    )
    assert len(generated) == 1
    assert len(pools) == 1
    assert pubsub.subscribed_channels == [channel]
    assert pools[0].connection_class is SharedAsyncSSLConnection
    connection = pools[0].make_connection()
    assert isinstance(connection, SharedAsyncSSLConnection)
    assert connection.ssl_context.get() is context
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    await asyncio.gather(*(subscription.close() for subscription in subscriptions))
    assert client.is_closed and pubsub.is_closed
    await factory.close()


@pytest.mark.asyncio
async def test_shared_subscription_delivers_redis_messages_and_failure_to_all_listeners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ControlledPubSub(AcknowledgingPubSub):
        def __init__(self) -> None:
            channel = realtime_channel("participant", EVENT_ID)
            super().__init__(
                [{"type": "subscribe", "pattern": None, "channel": channel, "data": 1}]
            )
            self.deliveries: asyncio.Queue[StateInvalidatedMessage | None] = asyncio.Queue()

        async def get_message(
            self,
            *,
            ignore_subscribe_messages: bool,
            timeout: float | None,  # noqa: ASYNC109 - Redis APIのkeyword契約を模倣する。
        ) -> object:
            if not ignore_subscribe_messages:
                return await super().get_message(ignore_subscribe_messages=False, timeout=timeout)
            message = await self.deliveries.get()
            if message is None:
                raise RedisConnectionError("simulated_disconnect")
            return {"data": message.model_dump_json()}

    pubsub = ControlledPubSub()
    _, client = install_async_redis_factory(monkeypatch, pubsub)
    factory = RedisRealtimeSubscriptionFactory()
    first, slow = await asyncio.gather(
        factory.subscribe("participant", EVENT_ID), factory.subscribe("participant", EVENT_ID)
    )
    message = StateInvalidatedMessage(
        type="state.invalidated", audience="participant", event_id=EVENT_ID
    )
    # slowが読まなくてもfirstへの通知を止めず、100件を最大1件へ縮約する。
    for _ in range(100):
        pubsub.deliveries.put_nowait(message)
        assert await asyncio.wait_for(first.next_message(), 1) == message
    assert await asyncio.wait_for(slow.next_message(), 1) == message
    pubsub.deliveries.put_nowait(None)
    for subscription in (first, slow):
        with pytest.raises(RealtimeUnavailableError):
            await asyncio.wait_for(subscription.next_message(), 1)
    await asyncio.gather(first.close(), slow.close())
    assert client.is_closed and pubsub.is_closed
    await factory.close()


@pytest.mark.asyncio
async def test_cancelled_local_close_still_releases_final_redis_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = realtime_channel("participant", EVENT_ID)
    pubsub = AcknowledgingPubSub(
        [{"type": "subscribe", "pattern": None, "channel": channel, "data": 1}]
    )
    _, client = install_async_redis_factory(monkeypatch, pubsub)
    factory = RedisRealtimeSubscriptionFactory()
    subscription = await factory.subscribe("participant", EVENT_ID)
    await factory._lock.acquire()
    closing = asyncio.create_task(subscription.close())
    await asyncio.sleep(0)
    closing.cancel()
    factory._lock.release()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert client.is_closed and pubsub.is_closed
    await factory.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("urgent_cause", ["operator_mutation", "automatic_result"])
@pytest.mark.parametrize("normal_cause", ["participant_answer", "participant_join"])
async def test_normal_notifications_cannot_downgrade_pending_progression(
    monkeypatch: pytest.MonkeyPatch,
    urgent_cause: InvalidationCause,
    normal_cause: InvalidationCause,
) -> None:
    channel = realtime_channel("operator", EVENT_ID)
    pubsub = AcknowledgingPubSub(
        [{"type": "subscribe", "pattern": None, "channel": channel, "data": 1}]
    )
    install_async_redis_factory(monkeypatch, pubsub)
    factory = RedisRealtimeSubscriptionFactory()
    subscription = await factory.subscribe("operator", EVENT_ID)
    normal = StateInvalidatedMessage(
        type="state.invalidated",
        audience="operator",
        event_id=EVENT_ID,
        cause=normal_cause,
    )
    urgent = normal.model_copy(update={"cause": urgent_cause})
    try:
        # 通常通知を進行通知で置換し、その後の通常通知でも優先度を維持する。
        subscription.deliver(normal)
        subscription.deliver(urgent)
        for _ in range(20):
            subscription.deliver(normal)
        assert subscription._pending.qsize() == 1
        assert await subscription.next_message() == urgent
    finally:
        await subscription.close()
        await factory.close()
