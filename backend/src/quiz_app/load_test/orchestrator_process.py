from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from datetime import UTC, datetime
from types import FrameType

from quiz_app.load_test.orchestrator_contracts import OrchestratorError, RunFiles


class RunInterrupted(OrchestratorError):
    pass


class RunClock:
    """signalを例外処理内のI/Oに割り込ませず、短い待機の境界で停止する。"""

    def __init__(self, deadline: datetime) -> None:
        self.deadline = deadline
        self.stop = threading.Event()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def check(self) -> None:
        if self.stop.is_set():
            raise RunInterrupted("orchestrator_interrupted")
        if self.now() >= self.deadline:
            raise OrchestratorError("orchestrator_deadline_exceeded")

    def sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while (remaining := end - time.monotonic()) > 0:
            self.check()
            self.stop.wait(min(remaining, 0.25))
        self.check()

    def signal(self, _number: int, _frame: FrameType | None) -> None:
        self.stop.set()


class GeneratorProcess:
    def __init__(self, files: RunFiles) -> None:
        self.files = files
        self.process: subprocess.Popen[bytes] | None = None
        self.output = files.output("generator-process") / "output.jsonl"

    def start(self) -> None:
        descriptor = os.open(
            self.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        argv = (
            "/usr/bin/node",
            str(self.files.repository_root / "load-test/k6/dist/generator-cli.js"),
            "--manifest",
            str(self.files.input("manifest.json")),
            "--output-directory",
            str(self.files.output("generator")),
            "--preparation-evidence",
            str(self.files.input("preparation.json")),
            "--application-ca-file",
            str(self.files.input("application-ca.pem")),
        )
        with os.fdopen(descriptor, "wb") as output:
            self.process = subprocess.Popen(  # noqa: S603 -- 固定実行ファイル・固定flagのみ。
                argv,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                cwd=self.files.repository_root,
                start_new_session=True,
                close_fds=True,
                env={
                    "PATH": "/usr/bin:/bin",
                    "HOME": str(self.files.root),
                    "LANG": "C",
                    "TZ": "UTC",
                    "NODE_EXTRA_CA_CERTS": str(self.files.input("application-ca.pem")),
                },
            )

    def check(self, *, allow_completed: bool = False) -> bool:
        if self.process is None:
            raise OrchestratorError("orchestrator_generator_not_started")
        if self.output.stat().st_size > 1_000_000:
            raise OrchestratorError("orchestrator_generator_output_exceeded")
        code = self.process.poll()
        if code is None:
            return False
        if code != 0 or not allow_completed:
            raise OrchestratorError("orchestrator_generator_failed")
        return True

    def close(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=10)
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=5)
