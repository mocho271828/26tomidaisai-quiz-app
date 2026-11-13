from pathlib import Path

import pytest
from pydantic import ValidationError

from quiz_app.config import Settings
from quiz_app.realtime import redis_adapter
from validation.redis_subscription_benchmark import (
    BenchmarkInput,
    factory_for,
    resource_sample,
    run_benchmark,
)


def _input(tmp_path: Path, **updates: object) -> BenchmarkInput:
    return BenchmarkInput.model_validate(
        {
            "variant": "p0",
            "source_commit": "a" * 40,
            "source_file": redis_adapter.__file__,
            "output": str(tmp_path / "run"),
            **updates,
        }
    )


@pytest.mark.parametrize("sessions", [0, -1, 1001])
def test_benchmark_rejects_unbounded_session_count(tmp_path: Path, sessions: int) -> None:
    with pytest.raises(ValidationError):
        _input(tmp_path, sessions=sessions)


def test_p0_benchmark_refuses_unrelated_source_identity(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="p0_source_file_mismatch"):
        factory_for(_input(tmp_path, source_file=tmp_path / "other.py"))


def test_resource_sample_never_contains_payload_or_identity() -> None:
    sample = resource_sample(0)
    assert set(sample.model_dump()) == {
        "elapsed_seconds",
        "loop_lag_seconds",
        "fd_open",
        "fd_soft_limit",
        "cpu_user_seconds",
        "cpu_system_seconds",
        "maximum_rss_bytes",
    }
    assert sample.fd_soft_limit > 0


@pytest.mark.asyncio
async def test_benchmark_refuses_plaintext_before_creating_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "validation.redis_subscription_benchmark.get_settings",
        lambda: Settings(_env_file=None, redis_url="redis://localhost:6379/0"),
    )
    with pytest.raises(ValueError, match="benchmark_requires_real_redis_tls"):
        await run_benchmark(_input(tmp_path))
    assert not (tmp_path / "run").exists()


@pytest.mark.asyncio
async def test_failed_benchmark_seals_private_sanitized_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json
    import stat

    monkeypatch.setattr(
        "validation.redis_subscription_benchmark.get_settings",
        lambda: Settings(_env_file=None, redis_url="rediss://redis:6379/0"),
    )

    def fail_ca_bundle() -> str:
        raise ValueError("secret-token-must-not-appear")

    monkeypatch.setattr("validation.redis_subscription_benchmark.ca_bundle_digest", fail_ca_bundle)
    config = _input(tmp_path)
    assert await run_benchmark(config) is False
    summary_path = config.output / "summary.json"
    summary = json.loads(summary_path.read_text())
    assert summary["status"] == "failed"
    assert summary["failure_code"] == "benchmark_ValueError"
    assert summary["cycles"] == []
    assert "secret-token" not in summary_path.read_text()
    assert "secret-token" not in capsys.readouterr().out
    assert stat.S_IMODE(summary_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((config.output / "resources.ndjson").stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_benchmark_rejects_socket_with_hostname_verification_disabled() -> None:
    from uuid import UUID

    from redis.asyncio import ConnectionPool, Redis
    from redis.asyncio.client import PubSub
    from redis.asyncio.connection import SSLConnection

    from quiz_app.realtime.redis_adapter import RedisRealtimeSubscription
    from validation.redis_subscription_benchmark import verified_tls_connections

    pool = ConnectionPool(connection_class=SSLConnection, host="redis", ssl_check_hostname=False)
    client = Redis.from_pool(pool)
    pubsub = PubSub(pool)
    pubsub.connection = pool.get_available_connection()
    subscription = RedisRealtimeSubscription(client, pubsub, "public", UUID(int=1))
    try:
        with pytest.raises(ValueError, match="benchmark_tls_verification_required"):
            verified_tls_connections([subscription])
    finally:
        await subscription.close()
