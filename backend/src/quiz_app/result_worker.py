"""DB上の未公開Roundを待ち行列として処理する専用process。"""

import signal
from threading import Event
from time import monotonic
from types import FrameType

import logfire
from sqlalchemy.exc import SQLAlchemyError

from quiz_app.automatic_results import (
    AUTOMATIC_AGGREGATE_IDLE_SECONDS,
    AUTOMATIC_AGGREGATE_RETRY_DELAYS_SECONDS,
    AutomaticResultAggregator,
    result_failure_stack,
)
from quiz_app.config import get_settings
from quiz_app.db import create_sync_engine
from quiz_app.domain.errors import DomainError
from quiz_app.domain.quiz_service import QuizDomainService
from quiz_app.realtime.redis_adapter import RealtimePublisher, get_sync_redis_client


def run_result_worker(stop_requested: Event, publisher: AutomaticResultAggregator) -> None:
    """一時障害は有界backoffで再試行し、未公開DB状態を失わない。"""

    retry_index = 0
    while not stop_requested.is_set():
        started = monotonic()
        try:
            outcome = publisher.process_next()
        except (SQLAlchemyError, DomainError) as error:
            delay = AUTOMATIC_AGGREGATE_RETRY_DELAYS_SECONDS[
                min(retry_index, len(AUTOMATIC_AGGREGATE_RETRY_DELAYS_SECONDS) - 1)
            ]
            retry_index += 1
            logfire.error(
                "result worker operation failed",
                error_type=type(error).__name__,
                stack=result_failure_stack(error),
                retry_delay_seconds=delay,
            )
            stop_requested.wait(delay)
            continue
        retry_index = 0
        if outcome is None:
            stop_requested.wait(AUTOMATIC_AGGREGATE_IDLE_SECONDS)
        else:
            logfire.info(
                "result worker completed operation",
                outcome=outcome.status,
                duration_seconds=monotonic() - started,
            )


def main() -> None:
    """接続を親から継承せず、専用poolを生成して終了時に閉じる。"""

    settings = get_settings()
    logfire.configure(send_to_logfire=settings.logfire_send_to_logfire)
    stop_requested = Event()

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        stop_requested.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    engine = create_sync_engine(pool_size=settings.database_result_worker_pool_size)
    try:
        redis_client = get_sync_redis_client()
        try:
            logfire.info("result worker started")
            run_result_worker(
                stop_requested,
                AutomaticResultAggregator(
                    engine, QuizDomainService(engine), RealtimePublisher(redis_client)
                ),
            )
        finally:
            redis_client.close()
    finally:
        engine.dispose()
    logfire.info("result worker stopped")


if __name__ == "__main__":
    main()
