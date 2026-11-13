"""API内の常駐採取とホスト側の有限な管理。秘密値を持つ例外本文は保存しない。"""

import argparse
import asyncio
import math
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import IO

import httpx
import logfire
from pydantic import ValidationError

from quiz_app.observability.database_snapshot import DatabaseWaitSnapshot
from quiz_app.observability.local_observation_models import (
    ApiInstance,
    CollectorFinished,
    ContainerSample,
    ContainersObservation,
    DatabaseObservation,
    MetricsObservation,
    ObservationCompletion,
    ObservationManifest,
    ObservationModel,
    ProcessCompletion,
)

SCRAPE_SECONDS = 1.0
AUXILIARY_SECONDS = 15.0
PROBE_TIMEOUT_SECONDS = 4.0


def write_record(stream: IO[str], record: ObservationModel) -> None:
    stream.write(record.model_dump_json() + "\n")
    stream.flush()


async def stop_process(process: asyncio.subprocess.Process) -> None:
    """stdin EOFを先に通知し、docker exec内の採取も終了させる。"""
    if process.stdin is not None:
        process.stdin.close()
    if process.returncode is not None:
        return
    try:
        await asyncio.wait_for(process.wait(), 3)
        return
    except TimeoutError:
        try:
            process.terminate()
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(process.wait(), 2)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            return
        await asyncio.wait_for(process.wait(), 2)


async def command_output(command: list[str]) -> tuple[str | None, str | None]:
    """重いprobeだけを別processで実行し、timeout時は子を回収する。"""
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
    except OSError:
        logfire.exception("local_observation_probe_spawn_failed")
        return None, "spawn"
    try:
        output, _ = await asyncio.wait_for(process.communicate(), PROBE_TIMEOUT_SECONDS)
        if process.returncode != 0:
            return None, "process_exit"
        return output.decode("utf-8"), None
    except TimeoutError:
        process.kill()
        await asyncio.wait_for(process.wait(), 2)
        return None, "timeout"
    except UnicodeDecodeError:
        return None, "invalid_output"
    finally:
        if process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), 2)


async def periodic(
    stop: asyncio.Event, deadline: float, interval: float, action: Callable[[], Awaitable[None]]
) -> None:
    """遅れた周期をまとめ実行せず、metricsと重いprobeを独立させる。"""
    while not stop.is_set() and monotonic() < deadline:
        scheduled = monotonic()
        await action()
        completed = monotonic()
        next_due = scheduled + (math.floor((completed - scheduled) / interval) + 1) * interval
        remaining = min(deadline - completed, next_due - completed)
        if remaining <= 0:
            continue
        try:
            await asyncio.wait_for(stop.wait(), remaining)
        except TimeoutError:
            pass


async def scrape_metrics(client: httpx.AsyncClient, instance: ApiInstance) -> MetricsObservation:
    started_at, started = datetime.now(UTC), monotonic()
    exposition: str | None = None
    failure = None
    try:
        # 内部固定URLだけを読み、外部入力のURLや資格情報を受け取らない。
        async with asyncio.timeout(2):
            response = await client.get("http://127.0.0.1:8000/internal/metrics")
            if response.status_code == 200:
                exposition = response.text
            else:
                failure = "http_status"
    except (TimeoutError, httpx.TimeoutException):
        failure = "timeout"
    except httpx.RequestError:
        failure = "transport"
    return MetricsObservation(
        instance=instance,
        started_at=started_at,
        monotonic_seconds=started,
        elapsed_seconds=monotonic() - started,
        status="ok" if exposition is not None else "unavailable",
        exposition=exposition,
        failure=failure,
    )


async def sample_database() -> DatabaseObservation:
    started_at, started = datetime.now(UTC), monotonic()
    output, failure = await command_output(
        [sys.executable, "-m", "quiz_app.observability.database_snapshot"]
    )
    snapshot = None
    if output is not None:
        try:
            snapshot = DatabaseWaitSnapshot.model_validate_json(output)
        except ValidationError:
            failure = "invalid_output"
    return DatabaseObservation.model_validate(
        {
            "started_at": started_at,
            "monotonic_seconds": started,
            "elapsed_seconds": monotonic() - started,
            "status": "ok" if snapshot is not None and snapshot.status == "ok" else "unavailable",
            "snapshot": snapshot,
            "failure": failure,
        }
    )


async def collect(instance: ApiInstance, duration: int, database: bool) -> None:
    stop = asyncio.Event()
    deadline = monotonic() + duration
    reason = "duration"
    loop = asyncio.get_running_loop()

    def parent_input() -> None:
        nonlocal reason
        if not os.read(sys.stdin.fileno(), 1024):
            reason = "parent_closed"
            stop.set()
            loop.remove_reader(sys.stdin.fileno())

    def signalled() -> None:
        nonlocal reason
        reason = "signal"
        stop.set()

    # docker execの親CLIが終了しても、stdin EOFでcontainer内の常駐採取を回収する。
    loop.add_reader(sys.stdin.fileno(), parent_input)
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, signalled)
    try:
        async with httpx.AsyncClient(timeout=2, trust_env=False) as client:

            async def metrics() -> None:
                write_record(sys.stdout, await scrape_metrics(client, instance))

            async def database_probe() -> None:
                write_record(sys.stdout, await sample_database())

            jobs = [periodic(stop, deadline, SCRAPE_SECONDS, metrics)]
            if database:
                jobs.append(periodic(stop, deadline, AUXILIARY_SECONDS, database_probe))
            await asyncio.gather(*jobs)
        write_record(
            sys.stdout,
            CollectorFinished.model_validate(
                {
                    "instance": instance,
                    "finished_at": datetime.now(UTC),
                    "reason": reason,
                }
            ),
        )
    finally:
        loop.remove_reader(sys.stdin.fileno())
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)


async def containers_probe(container_ids: list[str], stream: IO[str]) -> None:
    started_at, started = datetime.now(UTC), monotonic()
    output, failure = await command_output(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}", *container_ids]
    )
    samples: list[ContainerSample] = []
    if output is not None:
        try:
            samples = [ContainerSample.model_validate_json(line) for line in output.splitlines()]
            if len(samples) != len(container_ids):
                failure = "invalid_output"
        except ValidationError:
            failure = "invalid_output"
    write_record(
        stream,
        ContainersObservation.model_validate(
            {
                "started_at": started_at,
                "monotonic_seconds": started,
                "elapsed_seconds": monotonic() - started,
                "status": "ok" if failure is None else "unavailable",
                "samples": samples,
                "failure": failure,
            }
        ),
    )


async def observe(
    directory: Path,
    duration: int,
    instances: list[ApiInstance],
    compose: list[str],
    container_ids: list[str],
) -> None:
    """各APIは一度だけexecし、重いcontainer観測とは別に待つ。"""
    await asyncio.to_thread(directory.mkdir, mode=0o700)
    manifest = ObservationManifest(
        started_at=datetime.now(UTC),
        duration_seconds=duration,
        instances=instances,
    )
    (directory / "observation.json").write_text(manifest.model_dump_json(indent=2) + "\n")
    processes: list[tuple[ApiInstance, asyncio.subprocess.Process]] = []
    streams: list[IO[str]] = []
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    probe_task: asyncio.Task[None] | None = None
    try:
        for instance in instances:
            folder = directory / instance
            folder.mkdir(mode=0o700)
            stream = (folder / "samples.jsonl").open("w")
            streams.append(stream)
            command = [
                *compose,
                "exec",
                "-T",
                instance,
                "python",
                "-m",
                "quiz_app.observability.local_observation",
                "collect",
                "--instance",
                instance,
                "--duration",
                str(duration),
            ]
            if instance == "api":
                command.append("--database")
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=stream,
                stderr=asyncio.subprocess.DEVNULL,
            )
            processes.append((instance, process))
        containers = (directory / "containers.jsonl").open("w")
        streams.append(containers)

        async def probe() -> None:
            await containers_probe(container_ids, containers)

        probe_task = asyncio.create_task(
            periodic(stop, monotonic() + duration, AUXILIARY_SECONDS, probe)
        )
        waits = asyncio.gather(*(process.wait() for _, process in processes))
        stopped = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait(
                [waits, stopped], timeout=duration + 8, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)
        if not waits.done():
            for _, process in processes:
                await stop_process(process)
        await waits
    except asyncio.CancelledError:
        stop.set()
        raise
    finally:
        interrupted = stop.is_set()
        stop.set()
        for _, process in processes:
            await stop_process(process)
        if probe_task is not None:
            await probe_task
        for stream in streams:
            stream.close()
        completion = ObservationCompletion(
            finished_at=datetime.now(UTC),
            interrupted=interrupted,
            processes=[
                ProcessCompletion(instance=name, exit_code=process.returncode)
                for name, process in processes
            ],
        )
        (directory / "completion.json").write_text(completion.model_dump_json(indent=2) + "\n")
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)
    if (
        interrupted
        or len(processes) != len(instances)
        or any(p.returncode != 0 for _, p in processes)
    ):
        raise SystemExit("local_observation_incomplete")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="mode", required=True)
    collector = subcommands.add_parser("collect")
    collector.add_argument("--instance", choices=("api", "api-secondary"), required=True)
    collector.add_argument("--duration", type=int, required=True)
    collector.add_argument("--database", action="store_true")
    observer = subcommands.add_parser("observe")
    observer.add_argument("--directory", type=Path, required=True)
    observer.add_argument("--duration", type=int, required=True)
    observer.add_argument("--api-mode", choices=("single", "dual"), required=True)
    observer.add_argument("--container", action="append", required=True)
    observer.add_argument("compose", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not 1 <= args.duration <= 7200:
        parser.error("duration must be between 1 and 7200 seconds")
    if args.mode == "collect":
        asyncio.run(collect(args.instance, args.duration, args.database))
    else:
        compose = args.compose[1:] if args.compose[:1] == ["--"] else args.compose
        if compose[:2] != ["docker", "compose"]:
            parser.error("docker compose command required")
        instances: list[ApiInstance] = (
            ["api", "api-secondary"] if args.api_mode == "dual" else ["api"]
        )
        asyncio.run(observe(args.directory, args.duration, instances, compose, args.container))


if __name__ == "__main__":
    main()
