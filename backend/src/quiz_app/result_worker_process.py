"""APIから結果計算のGIL・接続poolを分離し、子processを監督する。"""

import asyncio
import sys
from typing import Protocol

import logfire

RESULT_WORKER_SHUTDOWN_SECONDS = 10.0
RESULT_WORKER_KILL_SECONDS = 5.0


class AutomaticResultRunner(Protocol):
    async def run(self, stop_requested: asyncio.Event) -> None: ...


class ResultWorkerExitedError(RuntimeError):
    """専用workerが停止要求なしに終了した。"""


class ResultWorkerCleanupError(RuntimeError):
    """子の終了を確認できず、新しいworkerを起動してはいけない。"""


class AutomaticResultProcessRunner:
    """同じimageの固定moduleを起動し、秘密値をargvへ渡さない。"""

    def __init__(self) -> None:
        self._cleanup_failed = False
        self._lifecycle: asyncio.Task[None] | None = None
        self._unreaped_child: asyncio.subprocess.Process | None = None
        self._exit_waiter: asyncio.Task[int] | None = None

    async def run(self, stop_requested: asyncio.Event) -> None:
        if self._cleanup_failed or self._unreaped_child is not None:
            raise ResultWorkerCleanupError("automatic_result_worker_cleanup_failed")
        if self._lifecycle is not None and not self._lifecycle.done():
            raise ResultWorkerCleanupError("automatic_result_worker_already_running")
        if stop_requested.is_set():
            return

        cancelled_stop = asyncio.Event()
        lifecycle = asyncio.create_task(
            self._run_child(stop_requested, cancelled_stop), name="result-worker-child-lifecycle"
        )
        self._lifecycle = lifecycle
        cancelled = False
        try:
            while not lifecycle.done():
                try:
                    await asyncio.shield(lifecycle)
                except asyncio.CancelledError:
                    # spawnの結果も終了処理も失わず、回収後にcallerへcancelを返す。
                    cancelled = True
                    cancelled_stop.set()
            lifecycle.result()
        except ResultWorkerCleanupError:
            self._cleanup_failed = True
            raise
        finally:
            if lifecycle.done():
                self._lifecycle = None
        if cancelled:
            raise asyncio.CancelledError

    async def _run_child(
        self,
        stop_requested: asyncio.Event,
        cancelled_stop: asyncio.Event,
    ) -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "quiz_app.result_worker",
        )
        self._unreaped_child = process
        exit_waiter = asyncio.create_task(process.wait())
        self._exit_waiter = exit_waiter
        stop_waiter = asyncio.create_task(stop_requested.wait())
        cancellation_waiter = asyncio.create_task(cancelled_stop.wait())
        try:
            completed, _ = await asyncio.wait(
                {exit_waiter, stop_waiter, cancellation_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not completed.intersection({stop_waiter, cancellation_waiter}):
                raise ResultWorkerExitedError("automatic_result_worker_exited")
        finally:
            stop_waiter.cancel()
            cancellation_waiter.cancel()
            cleanup = asyncio.create_task(
                self._cleanup_process(process, exit_waiter, stop_waiter, cancellation_waiter),
                name="result-worker-child-cleanup",
            )
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup.result()
            if cancelled:
                raise asyncio.CancelledError

    async def _cleanup_process(
        self,
        process: asyncio.subprocess.Process,
        exit_waiter: asyncio.Task[int],
        stop_waiter: asyncio.Task[bool],
        cancellation_waiter: asyncio.Task[bool],
    ) -> None:
        try:
            try:
                await self._stop_process(process, exit_waiter)
            except (OSError, RuntimeError) as error:
                raise ResultWorkerCleanupError("automatic_result_worker_cleanup_failed") from error
        finally:
            await asyncio.gather(stop_waiter, cancellation_waiter, return_exceptions=True)
        self._unreaped_child = None
        self._exit_waiter = None

    @staticmethod
    async def _stop_process(
        process: asyncio.subprocess.Process,
        exit_waiter: asyncio.Task[int],
    ) -> None:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(
                    asyncio.shield(exit_waiter),
                    timeout=RESULT_WORKER_SHUTDOWN_SECONDS,
                )
            except TimeoutError:
                logfire.error("result worker did not stop within grace period")
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        # timeoutでexit waiterをcancelせず、未回収の子とともにrunnerへ残す。
        completed, _ = await asyncio.wait({exit_waiter}, timeout=RESULT_WORKER_KILL_SECONDS)
        if not completed:
            raise ResultWorkerCleanupError("automatic_result_worker_reap_timeout")
        exit_waiter.result()
