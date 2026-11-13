import re
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from types import FunctionType
from typing import cast
from uuid import UUID

import logfire
import pytest
from redis.exceptions import RedisError
from sqlalchemy.exc import OperationalError
from sqlmodel import Session

import quiz_app.observability.realtime_failures as failures_module
import quiz_app.realtime.redis_adapter as redis_module
from quiz_app.audience.auth_service import ParticipantAuthService
from quiz_app.main import _preserve_runtime_log_fields
from quiz_app.observability.realtime_failures import (
    log_realtime_failure,
    preserve_realtime_failure_fields,
    realtime_failure_record,
)

HASH_FIXTURE = "43d58b5ecc1f03f61cf9090f5ed5ff22285caa4e3d771e06d2c4ee991f09cc60"
BODY_FIXTURE = "driver-sensitive-body-must-not-appear"
SQL_FIXTURE = "SELECT * FROM participant_sessions WHERE token_hash = %(token_hash)s"


def authentication_failure() -> OperationalError:
    class FailedSession:
        def execute(self, _statement: object) -> None:
            raise OperationalError(
                SQL_FIXTURE, {"token_hash": HASH_FIXTURE}, ValueError(BODY_FIXTURE)
            )

    try:
        ParticipantAuthService._verify_in_session(cast(Session, FailedSession()), HASH_FIXTURE)
    except OperationalError as error:
        return error
    raise AssertionError("fixture authentication unexpectedly succeeded")


def test_realtime_failure_keeps_safe_app_frames_and_discards_sql_and_credentials() -> None:
    record = realtime_failure_record(
        authentication_failure(), "state_or_authentication", "participant"
    )
    assert record.failure_kind == "database"
    assert record.stack_state == "available"
    assert len(record.stack) == 1
    assert record.stack[0].source_module == "quiz_app.audience.auth_service"
    assert record.stack[0].source_function == "_verify_in_session"
    assert record.stack[0].source_line > 0
    serialized = record.model_dump_json()
    for sensitive in (SQL_FIXTURE, HASH_FIXTURE, BODY_FIXTURE, str(Path(__file__))):
        assert sensitive not in serialized


def test_realtime_failure_marks_missing_app_stack_without_external_source() -> None:
    try:
        raise TimeoutError(BODY_FIXTURE)
    except TimeoutError as error:
        record = realtime_failure_record(error, "shared_state", "operator")
    assert record.failure_kind == "timeout"
    assert record.stack_state == "unavailable"
    assert record.stack == []
    assert BODY_FIXTURE not in record.model_dump_json()


def _recurse(depth: int) -> None:
    if depth:
        _recurse(depth - 1)
    else:
        raise TimeoutError("sensitive-body")


def test_realtime_failure_bounds_application_stack() -> None:
    namespace: dict[str, object] = {"TimeoutError": TimeoutError}
    recurse = cast(
        Callable[[int], None],
        FunctionType(_recurse.__code__.replace(co_filename=failures_module.__file__), namespace),
    )
    namespace["_recurse"] = recurse
    try:
        recurse(40)
    except TimeoutError as error:
        record = realtime_failure_record(error, "shared_state", "participant")
    assert record.stack_state == "truncated"
    assert len(record.stack) == 32
    assert "sensitive-body" not in record.model_dump_json()


def test_native_realtime_log_does_not_capture_active_database_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = StringIO()
    logger = logfire.configure(
        local=True,
        send_to_logfire=False,
        scrubbing=logfire.ScrubbingOptions(callback=_preserve_runtime_log_fields),
        console=logfire.ConsoleOptions(
            colors="never", include_timestamps=False, output=output, verbose=True
        ),
    )
    monkeypatch.setattr(failures_module.logfire, "error", logger.error)
    try:
        try:
            raise authentication_failure()
        except OperationalError as error:
            log_realtime_failure(error, "authentication_refresh", audience="participant")
    finally:
        assert logger.shutdown(timeout_millis=1000)
    rendered = output.getvalue()
    assert "realtime failure" in rendered
    assert "database" in rendered
    assert "authentication_refresh" in rendered
    assert "_verify_in_session" in rendered
    for sensitive in (SQL_FIXTURE, HASH_FIXTURE, BODY_FIXTURE, "ValueError", "OperationalError"):
        assert sensitive not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [RedisError, OSError])
async def test_actual_redis_publish_failures_do_not_log_secret_exception_body(
    monkeypatch: pytest.MonkeyPatch, error_type: type[RedisError] | type[OSError]
) -> None:
    class FailedSyncPublish:
        def publish(self, _channel: str, _message: str) -> int:
            raise error_type(f"{BODY_FIXTURE} {HASH_FIXTURE}")

        def ping(self) -> bool:
            raise error_type(BODY_FIXTURE)

    class FailedAsyncPublish:
        async def publish(self, _channel: str, _message: str) -> int:
            raise error_type(f"{BODY_FIXTURE} {HASH_FIXTURE}")

    output = StringIO()
    logger = logfire.configure(
        local=True,
        send_to_logfire=False,
        scrubbing=logfire.ScrubbingOptions(callback=_preserve_runtime_log_fields),
        console=logfire.ConsoleOptions(
            colors="never", include_timestamps=False, output=output, verbose=True
        ),
    )
    monkeypatch.setattr(failures_module.logfire, "error", logger.error)
    monkeypatch.setattr(redis_module, "invalidate_local_common_state", lambda *_: None)
    try:
        assert not redis_module.RealtimePublisher(FailedSyncPublish()).notify(
            UUID(int=1), ("operator",)
        )
        assert not await redis_module.AsyncRealtimePublisher(FailedAsyncPublish()).notify(
            UUID(int=1), ("operator",)
        )
        assert redis_module.RedisHealthProbe(FailedSyncPublish()).status() == "degraded"
    finally:
        assert logger.shutdown(timeout_millis=1000)
    rendered = output.getvalue()
    assert "redis_publish" in rendered
    assert "redis_readiness" in rendered
    assert "quiz_app.realtime.redis_adapter" in rendered
    for sensitive in (BODY_FIXTURE, HASH_FIXTURE, "RedisError", "OSError"):
        assert sensitive not in rendered


@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [
        (
            ("attributes", "record", "boundary"),
            "state_or_authentication",
            "state_or_authentication",
        ),
        (
            ("attributes", "record", "boundary"),
            "authentication_refresh",
            "authentication_refresh",
        ),
        (("attributes", "record", "boundary"), "state_send", "state_send"),
        (("attributes", "record", "boundary"), "heartbeat_send", "heartbeat_send"),
        (("attributes", "record", "boundary"), "session_change_send", "session_change_send"),
        (
            ("attributes", "record", "stack", 0, "source_function"),
            "_verify_in_session",
            "_verify_in_session",
        ),
        (
            ("message", "record", "stack", 31, "source_module"),
            "quiz_app.audience.auth_service",
            "quiz_app.audience.auth_service",
        ),
        (("attributes", "record", "stack", 32, "source_function"), "_verify_in_session", None),
        (("attributes", "body", "stack", 0, "source_function"), "_verify_in_session", None),
        (
            ("attributes", "record", "stack", 0, "source_module"),
            "quiz_app.private.auth_secret",
            None,
        ),
        (("attributes", "record", "stack", 0, "source_function"), BODY_FIXTURE, None),
        (("attributes", "record", "boundary"), BODY_FIXTURE, None),
    ],
)
def test_safe_failure_scrubbing_exception_is_exact_and_keeps_other_redaction(
    path: tuple[str | int, ...], value: object, expected: str | None
) -> None:
    pattern_match = re.search("auth", "authentication")
    assert pattern_match is not None
    match = logfire.ScrubMatch(path, value, pattern_match)
    assert preserve_realtime_failure_fields(match) == expected
