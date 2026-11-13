"""専用Redis実TLS上の購読層だけを比較する。ブラウザ/API合格には使わない。"""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import resource
import ssl
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Literal, cast
from uuid import UUID

import logfire
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from redis.asyncio.client import PubSub
from redis.asyncio.connection import SSLConnection
from redis.exceptions import RedisError

from quiz_app.config import get_settings
from quiz_app.realtime import redis_adapter
from quiz_app.realtime.contracts import StateInvalidatedMessage
from quiz_app.realtime.redis_adapter import (
    RealtimeSubscription,
    RealtimeSubscriptionFactory,
    RealtimeUnavailableError,
    RedisRealtimeSubscriptionFactory,
    get_sync_redis_client,
    realtime_channel,
)

EVENT_ID = UUID("e52e8e75-70b0-4d66-9197-68803d204ee1")
SAMPLE_INTERVAL_SECONDS = 0.02


class BenchmarkInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variant: Literal["baseline", "p0"]
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_file: Path
    output: Path
    sessions: int = Field(default=100, ge=1, le=1000)
    cycles: int = Field(default=2, ge=1, le=5)


class ResourceSample(BaseModel):
    elapsed_seconds: float
    loop_lag_seconds: float
    fd_open: int | None
    fd_soft_limit: int
    cpu_user_seconds: float
    cpu_system_seconds: float
    maximum_rss_bytes: int


class CycleResult(BaseModel):
    attempted: int
    connected: int
    failures: dict[str, int]
    connect_seconds: float
    recipients: int
    receive_seconds: float
    cleanup_seconds: float
    fd_after_cleanup: int | None
    redis_connections_observed: int
    tls_protocols: list[str]
    verified_tls_connections: int


def verified_tls_connections(subscriptions: list[RealtimeSubscription]) -> tuple[int, list[str]]:
    """旧/新両factoryが実際に使用したsocketのTLS条件を確認する。"""

    connections: set[int] = set()
    protocols: set[str] = set()
    for subscription in subscriptions:
        hub = getattr(subscription, "_hub", None)
        source = getattr(hub, "source", subscription)
        pubsub = getattr(source, "_pubsub", None)
        if not isinstance(pubsub, PubSub) or not isinstance(pubsub.connection, SSLConnection):
            raise ValueError("benchmark_tls_connection_unavailable")
        connection = pubsub.connection
        context = connection.ssl_context.get()
        writer = connection._writer
        tls = writer.get_extra_info("ssl_object") if writer is not None else None
        if (
            context.verify_mode != ssl.CERT_REQUIRED
            or not context.check_hostname
            or not isinstance(tls, ssl.SSLObject)
            or tls.server_hostname != connection.host
            or not tls.getpeercert()
        ):
            raise ValueError("benchmark_tls_verification_required")
        protocol = tls.version()
        if protocol not in {"TLSv1.2", "TLSv1.3"}:
            raise ValueError("benchmark_tls_protocol_invalid")
        connections.add(id(connection))
        protocols.add(protocol)
    return len(connections), sorted(protocols)


def ca_bundle_digest() -> str:
    """局所再現を軽いlocal CAだけで実行しないよう、通常CAの保存も強制する。"""

    configured = os.environ.get("SSL_CERT_FILE")
    if not configured:
        raise ValueError("benchmark_explicit_ca_bundle_required")
    bundle = Path(configured).read_bytes()
    system_bundle = Path("/etc/ssl/certs/ca-certificates.crt").read_bytes()
    if not bundle.startswith(system_bundle) or len(bundle) <= len(system_bundle):
        raise ValueError("benchmark_system_plus_local_ca_required")
    return hashlib.sha256(bundle).hexdigest()


def write_private(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(text)


def resource_sample(epoch: float, lag: float = 0) -> ResourceSample:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    fd_directory = Path("/proc/self/fd")
    return ResourceSample(
        elapsed_seconds=monotonic() - epoch,
        loop_lag_seconds=lag,
        fd_open=len(list(fd_directory.iterdir())) if fd_directory.exists() else None,
        fd_soft_limit=resource.getrlimit(resource.RLIMIT_NOFILE)[0],
        cpu_user_seconds=usage.ru_utime,
        cpu_system_seconds=usage.ru_stime,
        maximum_rss_bytes=int(usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024)),
    )


def factory_for(config: BenchmarkInput) -> RealtimeSubscriptionFactory:
    if config.variant == "p0":
        if config.source_file.resolve() != Path(redis_adapter.__file__).resolve():
            raise ValueError("p0_source_file_mismatch")
        return RedisRealtimeSubscriptionFactory()
    spec = importlib.util.spec_from_file_location("baseline_redis_adapter", config.source_file)
    if spec is None or spec.loader is None:
        raise ValueError("baseline_module_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    constructor = getattr(module, "RedisRealtimeSubscriptionFactory", None)
    if not callable(constructor):
        raise ValueError("baseline_factory_unavailable")
    return cast(Callable[[], RealtimeSubscriptionFactory], constructor)()


async def sample_resources(samples: list[ResourceSample], epoch: float) -> None:
    deadline = monotonic() + SAMPLE_INTERVAL_SECONDS
    while True:
        await asyncio.sleep(max(0, deadline - monotonic()))
        samples.append(resource_sample(epoch, max(0, monotonic() - deadline)))
        deadline = monotonic() + SAMPLE_INTERVAL_SECONDS


async def run_cycle(factory: RealtimeSubscriptionFactory, sessions: int) -> CycleResult:
    started = monotonic()
    subscriptions: list[RealtimeSubscription] = []
    failures: dict[str, int] = {}

    async def connect() -> None:
        # 全員のgather完了前に期限へ達しても、成功済みFDをfinallyで回収できるよう保持する。
        subscriptions.append(await factory.subscribe("participant", EVENT_ID))

    try:
        results = await asyncio.gather(
            *(connect() for _ in range(sessions)), return_exceptions=True
        )
        connect_seconds = monotonic() - started
        for result in results:
            if isinstance(result, BaseException):
                # 外部例外本文は資格情報を含み得るため、型分類だけを保存する。
                name = type(result).__name__
                failures[name] = failures.get(name, 0) + 1
        connections, protocols = verified_tls_connections(subscriptions)
        receive_started = monotonic()
        message = StateInvalidatedMessage(
            type="state.invalidated", audience="participant", event_id=EVENT_ID
        )
        recipients = 0
        await asyncio.to_thread(
            get_sync_redis_client().publish,
            realtime_channel("participant", EVENT_ID),
            message.model_dump_json(),
        )
        if subscriptions:
            async with asyncio.timeout(5):
                delivered = await asyncio.gather(
                    *(item.next_message() for item in subscriptions), return_exceptions=True
                )
            recipients = sum(item == message for item in delivered)
        receive_seconds = monotonic() - receive_started
        await asyncio.sleep(1)
    finally:
        cleanup_started = monotonic()
        await asyncio.gather(*(item.close() for item in subscriptions))
    cleanup_seconds = monotonic() - cleanup_started
    await asyncio.sleep(0.1)
    return CycleResult(
        attempted=sessions,
        connected=len(subscriptions),
        failures=failures,
        connect_seconds=connect_seconds,
        recipients=recipients,
        receive_seconds=receive_seconds,
        cleanup_seconds=cleanup_seconds,
        fd_after_cleanup=resource_sample(started).fd_open,
        redis_connections_observed=connections,
        tls_protocols=protocols,
        verified_tls_connections=connections,
    )


async def run_benchmark(config: BenchmarkInput) -> bool:
    if not get_settings().redis_url.startswith("rediss://"):
        raise ValueError("benchmark_requires_real_redis_tls")
    config.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    logfire.configure(send_to_logfire=False, console=False)
    epoch = monotonic()
    samples = [resource_sample(epoch)]
    sampler = asyncio.create_task(sample_resources(samples, epoch))
    factory: RealtimeSubscriptionFactory | None = None
    results: list[CycleResult] = []
    failure_code: str | None = None
    source_digest: str | None = None
    ca_digest: str | None = None
    try:
        source_digest = hashlib.sha256(config.source_file.read_bytes()).hexdigest()
        ca_digest = ca_bundle_digest()
        factory = factory_for(config)
        await asyncio.sleep(0)
        if isinstance(factory, RedisRealtimeSubscriptionFactory):
            await factory.start()
        async with asyncio.timeout(60):
            for _ in range(config.cycles):
                results.append(await run_cycle(factory, config.sessions))
    except (
        OSError,
        ValueError,
        RuntimeError,
        TimeoutError,
        ImportError,
        RedisError,
        RealtimeUnavailableError,
    ) as error:
        failure_code = f"benchmark_{type(error).__name__}"
        logfire.error("redis benchmark failed", failure_type=type(error).__name__)
    except asyncio.CancelledError:
        failure_code = "benchmark_cancelled"
    finally:
        try:
            if isinstance(factory, RedisRealtimeSubscriptionFactory):
                await factory.close()
            if get_sync_redis_client.cache_info().currsize:
                get_sync_redis_client().close()
            get_sync_redis_client.cache_clear()
        except (RedisError, OSError, RuntimeError):
            failure_code = "benchmark_cleanup_failed"
        await asyncio.sleep(0.1)
        samples.append(resource_sample(epoch))
        sampler.cancel()
        await asyncio.gather(sampler, return_exceptions=True)
    summary = {
        "scope": "redis_subscription_only_not_browser_acceptance",
        "variant": config.variant,
        "status": "completed" if failure_code is None else "failed",
        "failure_code": failure_code,
        "source_commit": config.source_commit,
        "source_file_sha256": source_digest,
        "ca_bundle_sha256": ca_digest,
        "recorded_at": datetime.now(UTC).isoformat(),
        "python": sys.version.split()[0],
        "tls_requirement": "CERT_REQUIRED_and_hostname_verified_on_actual_socket",
        "fd_peak": max((sample.fd_open or 0) for sample in samples),
        "loop_lag_max_seconds": max(sample.loop_lag_seconds for sample in samples),
        "cycles": [result.model_dump() for result in results],
    }
    write_private(
        config.output / "resources.ndjson",
        "".join(sample.model_dump_json() + "\n" for sample in samples),
    )
    text = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    write_private(config.output / "summary.json", text)
    sys.stdout.write(text)
    return failure_code is None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=["baseline", "p0"], required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--sessions", type=int, default=100)
    parser.add_argument("--cycles", type=int, default=2)
    try:
        config = BenchmarkInput.model_validate(vars(parser.parse_args()))
        return 0 if asyncio.run(run_benchmark(config)) else 1
    except (
        ValidationError,
        OSError,
        ValueError,
        RuntimeError,
        TimeoutError,
        ImportError,
        RedisError,
        RealtimeUnavailableError,
    ) as error:
        logfire.error("redis benchmark failed", failure_type=type(error).__name__)
        sys.stderr.write(f"redis_benchmark_failed:{type(error).__name__}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
