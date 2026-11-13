import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from quiz_app.observability.metrics import QuizMetrics
from quiz_app.observability.runtime import ObservedQueuePool, monitor_runtime


def test_pool_acquisition_observes_success_and_timeout_without_identity() -> None:
    metrics = QuizMetrics()
    engine = create_engine(
        "sqlite://", poolclass=ObservedQueuePool, pool_size=1, max_overflow=0, pool_timeout=0.01
    )
    metrics.register_database_pool(engine.pool)
    with engine.connect():
        with pytest.raises(PoolTimeoutError):
            engine.connect()
    samples = metrics.render().decode()
    assert 'quiz_db_connection_acquisition_seconds_count{result="success"} 1.0' in samples
    assert 'quiz_db_connection_acquisition_seconds_count{result="error"} 1.0' in samples
    assert 'quiz_db_pool_connections{state="checked_out"} 0.0' in samples
    assert "sqlite" not in samples
    engine.dispose()


@pytest.mark.asyncio
async def test_runtime_monitor_observes_delay_and_stops_cleanly() -> None:
    metrics = QuizMetrics()
    stop = asyncio.Event()
    task = asyncio.create_task(monitor_runtime(metrics, stop))
    await asyncio.sleep(1.05)
    stop.set()
    await asyncio.wait_for(task, timeout=0.2)
    samples = metrics.render().decode()
    assert "quiz_event_loop_delay_seconds_count 1.0" in samples
    assert 'quiz_thread_pool_tokens{state="borrowed"} 0.0' in samples
    assert 'quiz_thread_pool_tokens{state="waiting"} 0.0' in samples


def test_subscription_metrics_use_bounded_labels_and_release_gauge() -> None:
    metrics = QuizMetrics()
    metrics.change_redis_subscriptions("participant", 1)
    metrics.record_redis_delivery_coalesced("participant")
    metrics.record_tls_context_initialization(0.02)
    metrics.record_redis_subscription_cleanup(True)
    metrics.change_redis_subscriptions("participant", -1)
    samples = metrics.render().decode()
    assert 'quiz_redis_subscriptions{audience="participant"} 0.0' in samples
    assert 'quiz_redis_delivery_coalesced_total{audience="participant"} 1.0' in samples
    assert 'quiz_redis_subscription_cleanup_total{result="success"} 1.0' in samples
    assert "quiz_redis_tls_context_initialization_seconds_count 1.0" in samples
