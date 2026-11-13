"""内部probeとCloudWatch送信だけを行う、有限期限のhost collector。"""

import argparse
import fcntl
import os
import signal
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from types import FrameType

import httpx
import logfire
from botocore.exceptions import BotoCoreError, ClientError

from quiz_app.system_monitoring.cloudwatch import CloudWatchClient, create_client
from quiz_app.system_monitoring.collector_config import (
    NAMESPACE,
    STATE_DIRECTORY,
    CollectorConfig,
    load_collector_config,
)
from quiz_app.system_monitoring.diagnostics import record_failure
from quiz_app.system_monitoring.probes import (
    PROBE_ORIGINS,
    ApiKey,
    CounterReading,
    ProbeResult,
    probe_api,
    request_rate,
)

COLLECTION_INTERVAL_SECONDS = 60


def _publish(
    client: CloudWatchClient,
    config: CollectorConfig,
    probes: list[ProbeResult],
    rate: float | None,
    now: datetime,
) -> None:
    dimensions = [{"Name": "Environment", "Value": config.environment_id}]
    values: list[dict[str, object]] = []
    for probe in probes:
        for kind, is_up in (("live", probe.live), ("ready", probe.ready)):
            values.append(
                {
                    "MetricName": "ApiHealth",
                    "Dimensions": [
                        *dimensions,
                        {"Name": "Api", "Value": probe.key},
                        {"Name": "Health", "Value": kind},
                    ],
                    "Timestamp": now,
                    "Value": float(is_up),
                    "Unit": "None",
                }
            )
    if rate is not None:
        values.append(
            {
                "MetricName": "ApiRequestsPerMinute",
                "Dimensions": dimensions,
                "Timestamp": now,
                "Value": rate,
                "Unit": "None",
            }
        )
    client.put_metric_data(Namespace=NAMESPACE, MetricData=values)


class Collector:
    """60秒以上の間隔で内部probeを行い、Dashboardへ最大5系列を送信する。"""

    def __init__(
        self,
        config: CollectorConfig,
        cloudwatch: CloudWatchClient,
        client: httpx.Client,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.config = config
        self.cloudwatch = cloudwatch
        self.http = client
        self.clock = clock
        self.previous: dict[ApiKey, CounterReading | None] = {}
        self.last_attempt: datetime | None = None

    def collect(self, now: datetime) -> bool:
        """期限内だけ採取・送信し、期限を過ぎたらFalseを返す。"""
        if now >= self.config.expires_at or self.clock() >= self.config.expires_at:
            return False
        if (
            self.last_attempt is not None
            and (now - self.last_attempt).total_seconds() < COLLECTION_INTERVAL_SECONDS
        ):
            return True
        self.last_attempt = now
        probes = [
            probe_api(
                self.http,
                key,
                origin,
                now,
                expires_at=self.config.expires_at,
            )
            for key, origin in PROBE_ORIGINS
        ]
        if self.clock() >= self.config.expires_at:
            return False
        rates = [request_rate(self.previous.get(probe.key), probe.counter) for probe in probes]
        self.previous = {probe.key: probe.counter for probe in probes}
        rate = sum(value for value in rates if value is not None) if None not in rates else None
        try:
            _publish(self.cloudwatch, self.config, probes, rate, now)
        except (BotoCoreError, ClientError, ValueError) as error:
            record_failure("cloudwatch_write", error)
        return self.clock() < self.config.expires_at


def run(config: CollectorConfig, stopped: Event, *, once: bool = False) -> int:
    """期限切れはAWS資格情報の取得も行わず終了する。"""
    if datetime.now(UTC) >= config.expires_at:
        logfire.info("system monitoring expired")
        return 0
    cloudwatch = create_client(config.region)
    with httpx.Client(timeout=1.0, trust_env=False, follow_redirects=False) as client:
        collector = Collector(config, cloudwatch, client)
        while not stopped.is_set():
            if not collector.collect(datetime.now(UTC)):
                break
            logfire.info("system monitoring sampled")
            if once:
                break
            # 巡回が長引いても並列化せず、期限後に新規I/Oを開始しない。
            remaining = (config.expires_at - datetime.now(UTC)).total_seconds()
            stopped.wait(max(0, min(COLLECTION_INTERVAL_SECONDS, remaining)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AWS Dashboard用metricの有限host collector")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    arguments = parser.parse_args(argv)
    logfire.configure(send_to_logfire=False, inspect_arguments=False)
    stopped = Event()

    def stop(_signum: int, _frame: FrameType | None) -> None:
        stopped.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stop)
    descriptor: int | None = None
    try:
        config = load_collector_config(arguments.config)
        if datetime.now(UTC) >= config.expires_at:
            return 0
        descriptor = os.open(
            STATE_DIRECTORY / "collector.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
        ):
            raise ValueError("monitoring_lock_invalid")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        logfire.info("system monitoring started")
        return run(config, stopped, once=arguments.once)
    except (OSError, ValueError, BotoCoreError, ClientError) as error:
        record_failure("configuration" if descriptor is None else "collector", error)
        return 1
    finally:
        if descriptor is not None:
            os.close(descriptor)
        logfire.info("system monitoring stopped")


if __name__ == "__main__":
    raise SystemExit(main())
