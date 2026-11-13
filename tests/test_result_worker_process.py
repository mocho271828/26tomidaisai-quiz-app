import asyncio
from typing import cast

import pytest

import quiz_app.result_worker_process as worker_process_module
from quiz_app.result_worker_process import (
    AutomaticResultProcessRunner,
    ResultWorkerCleanupError,
    ResultWorkerExitedError,
)


class FakeProcess:
    def __init__(self, *, ignores_termination: bool = False, ignores_kill: bool = False) -> None:
        self.returncode: int | None = None
        self.exit = asyncio.Event()
        self.terminated = 0
        self.killed = 0
        self.ignores_termination = ignores_termination
        self.ignores_kill = ignores_kill
        self.termination_started = asyncio.Event()
        self.reaped = False

    async def wait(self) -> int:
        await self.exit.wait()
        assert self.returncode is not None
        self.reaped = True
        return self.returncode

    def terminate(self) -> None:
        self.terminated += 1
        self.termination_started.set()
        if not self.ignores_termination:
            self.returncode = 0
            self.exit.set()

    def kill(self) -> None:
        self.killed += 1
        if not self.ignores_kill:
            self.returncode = -9
            self.exit.set()


async def start_runner(
    monkeypatch: pytest.MonkeyPatch,
    process: FakeProcess,
    stop_requested: asyncio.Event,
) -> asyncio.Task[None]:
    spawned = asyncio.Event()

    async def spawn(*arguments: str) -> asyncio.subprocess.Process:
        assert arguments == (worker_process_module.sys.executable, "-m", "quiz_app.result_worker")
        spawned.set()
        return cast(asyncio.subprocess.Process, process)

    monkeypatch.setattr(worker_process_module.asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(AutomaticResultProcessRunner().run(stop_requested))
    await asyncio.wait_for(spawned.wait(), timeout=1)
    return task


@pytest.mark.asyncio
async def test_worker_stops_and_reaps_its_child(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess()
    stop_requested = asyncio.Event()
    task = await start_runner(monkeypatch, process, stop_requested)
    stop_requested.set()
    await task
    assert process.terminated == 1
    assert process.returncode == 0
    assert process.killed == 0


@pytest.mark.asyncio
async def test_worker_exit_is_not_reported_as_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess()
    task = await start_runner(monkeypatch, process, asyncio.Event())
    process.returncode = 7
    process.exit.set()
    with pytest.raises(ResultWorkerExitedError, match="automatic_result_worker_exited"):
        await task
    assert process.terminated == 0


@pytest.mark.asyncio
async def test_cancellation_also_reaps_child(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess()
    task = await start_runner(monkeypatch, process, asyncio.Event())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.terminated == 1
    assert process.returncode == 0


@pytest.mark.asyncio
async def test_unresponsive_child_is_killed_after_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(ignores_termination=True)
    monkeypatch.setattr(worker_process_module, "RESULT_WORKER_SHUTDOWN_SECONDS", 0.01)
    stop_requested = asyncio.Event()
    task = await start_runner(monkeypatch, process, stop_requested)
    stop_requested.set()
    await task
    assert process.terminated == 1
    assert process.killed == 1
    assert process.returncode == -9
    assert process.reaped is True


@pytest.mark.asyncio
async def test_cancel_during_grace_wait_still_kills_and_reaps_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess(ignores_termination=True)
    monkeypatch.setattr(worker_process_module, "RESULT_WORKER_SHUTDOWN_SECONDS", 0.01)
    stop_requested = asyncio.Event()
    task = await start_runner(monkeypatch, process, stop_requested)
    stop_requested.set()
    await process.termination_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.terminated == 1
    assert process.killed == 1
    assert process.reaped is True


@pytest.mark.asyncio
async def test_cancel_while_spawning_keeps_ownership_until_child_is_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess()
    spawn_started = asyncio.Event()
    spawn_ready = asyncio.Event()
    spawn_cancelled = False

    async def spawn(*arguments: str) -> asyncio.subprocess.Process:
        nonlocal spawn_cancelled
        assert arguments == (worker_process_module.sys.executable, "-m", "quiz_app.result_worker")
        spawn_started.set()
        try:
            await spawn_ready.wait()
        except asyncio.CancelledError:
            spawn_cancelled = True
            raise
        return cast(asyncio.subprocess.Process, process)

    monkeypatch.setattr(worker_process_module.asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(AutomaticResultProcessRunner().run(asyncio.Event()))
    await spawn_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    spawn_ready.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert spawn_cancelled is False
    assert process.terminated == 1
    assert process.reaped is True


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_cleanup_blocks_all_further_spawns_even_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    cancel: bool,
) -> None:
    process = FakeProcess(ignores_termination=True, ignores_kill=True)
    calls = 0

    async def spawn(*_arguments: str) -> asyncio.subprocess.Process:
        nonlocal calls
        calls += 1
        return cast(asyncio.subprocess.Process, process)

    monkeypatch.setattr(worker_process_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(worker_process_module, "RESULT_WORKER_SHUTDOWN_SECONDS", 0.01)
    monkeypatch.setattr(worker_process_module, "RESULT_WORKER_KILL_SECONDS", 0.01)
    runner = AutomaticResultProcessRunner()
    stop_requested = asyncio.Event()
    task = asyncio.create_task(runner.run(stop_requested))
    await asyncio.sleep(0)
    stop_requested.set()
    await process.termination_started.wait()
    if cancel:
        task.cancel()
    try:
        with pytest.raises(ResultWorkerCleanupError):
            await task
        with pytest.raises(
            ResultWorkerCleanupError, match="automatic_result_worker_cleanup_failed"
        ):
            await runner.run(asyncio.Event())
        assert calls == 1
        assert process.killed == 1
        assert process.reaped is False
    finally:
        # 未回収waiterもテスト終了時には完了させ、実装の失敗状態を隠さない。
        process.returncode = -9
        process.exit.set()
        assert runner._exit_waiter is not None
        await runner._exit_waiter


@pytest.mark.asyncio
async def test_spawn_failure_does_not_disable_crash_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess()
    spawned = asyncio.Event()
    calls = 0

    async def spawn(*_arguments: str) -> asyncio.subprocess.Process:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("fixture spawn failure")
        spawned.set()
        return cast(asyncio.subprocess.Process, process)

    monkeypatch.setattr(worker_process_module.asyncio, "create_subprocess_exec", spawn)
    runner = AutomaticResultProcessRunner()
    with pytest.raises(OSError, match="fixture spawn failure"):
        await runner.run(asyncio.Event())
    stop_requested = asyncio.Event()
    task = asyncio.create_task(runner.run(stop_requested))
    await spawned.wait()
    stop_requested.set()
    await task
    assert calls == 2
    assert process.reaped is True


@pytest.mark.asyncio
async def test_cancel_during_spawn_reaps_a_native_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_spawn = asyncio.create_subprocess_exec
    child: asyncio.subprocess.Process | None = None
    spawn_started = asyncio.Event()
    spawn_ready = asyncio.Event()

    async def spawn(*arguments: str) -> asyncio.subprocess.Process:
        nonlocal child
        assert arguments == (worker_process_module.sys.executable, "-m", "quiz_app.result_worker")
        # DBへ接続するworker本体を起動せず、実OS childの回収だけを確認する。
        child = await native_spawn(
            worker_process_module.sys.executable, "-c", "import time; time.sleep(60)"
        )
        spawn_started.set()
        await spawn_ready.wait()
        return child

    monkeypatch.setattr(worker_process_module.asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(AutomaticResultProcessRunner().run(asyncio.Event()))
    try:
        await asyncio.wait_for(spawn_started.wait(), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        spawn_ready.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert child is not None and child.returncode is not None
    finally:
        spawn_ready.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if child is not None and child.returncode is None:
            child.kill()
            await child.wait()
