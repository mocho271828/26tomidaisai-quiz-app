"""障害注入host用のambient-clean bounded subprocess executor。"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from time import monotonic
from typing import Final, Protocol, TypeAlias, cast

from quiz_app.load_test.fault_host_config import FaultControllerHostError

MAXIMUM_COMMAND_OUTPUT_BYTES: Final = 64 * 1024
CommandEnvironment: TypeAlias = Mapping[str, str]


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class CommandExecutor(Protocol):
    """argvと環境を分離して実行する境界。"""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        environment: CommandEnvironment,
        timeout_seconds: float,
    ) -> CommandResult: ...


class SubprocessCommandExecutor:
    """shellを介さずstdout/stderrを有限長で回収するexecutor。"""

    def __init__(self, maximum_output_bytes: int = MAXIMUM_COMMAND_OUTPUT_BYTES) -> None:
        if maximum_output_bytes < 1:
            raise ValueError("maximum_output_bytesは1以上にしてください")
        self._maximum_output_bytes = maximum_output_bytes

    def run(
        self,
        argv: tuple[str, ...],
        *,
        environment: CommandEnvironment,
        timeout_seconds: float,
    ) -> CommandResult:
        if (
            not argv
            or timeout_seconds <= 0
            or any(not argument or "\x00" in argument for argument in argv)
            or any("\x00" in key or "\x00" in value for key, value in environment.items())
        ):
            raise FaultControllerHostError("fault_host_command_invalid")
        try:
            process = subprocess.Popen(  # noqa: S603 -- facadeは固定argv builderだけを渡す。
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(environment),
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as error:
            raise FaultControllerHostError("fault_host_command_unavailable") from error
        return self._collect(process, timeout_seconds)

    def _collect(
        self,
        process: subprocess.Popen[bytes],
        timeout_seconds: float,
    ) -> CommandResult:
        if process.stdout is None or process.stderr is None:
            self._kill(process)
            raise FaultControllerHostError("fault_host_command_io_invalid")
        stdout_descriptor = process.stdout.fileno()
        stderr_descriptor = process.stderr.fileno()
        streams = {stdout_descriptor: bytearray(), stderr_descriptor: bytearray()}
        selector = selectors.DefaultSelector()
        try:
            for descriptor in streams:
                os.set_blocking(descriptor, False)
                selector.register(descriptor, selectors.EVENT_READ)
            deadline = monotonic() + timeout_seconds
            while selector.get_map():
                remaining = deadline - monotonic()
                if remaining <= 0:
                    self._kill(process)
                    raise FaultControllerHostError("fault_host_command_timeout")
                for key, _ in selector.select(min(remaining, 0.1)):
                    descriptor = cast(int, key.fd)
                    try:
                        chunk = os.read(descriptor, 8192)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(descriptor)
                        continue
                    streams[descriptor].extend(chunk)
                    if sum(len(body) for body in streams.values()) > self._maximum_output_bytes:
                        self._kill(process)
                        raise FaultControllerHostError("fault_host_command_output_too_large")
            remaining = deadline - monotonic()
            if remaining <= 0:
                self._kill(process)
                raise FaultControllerHostError("fault_host_command_timeout")
            try:
                returncode = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as error:
                self._kill(process)
                raise FaultControllerHostError("fault_host_command_timeout") from error
            return CommandResult(
                returncode=returncode,
                stdout=bytes(streams[stdout_descriptor]),
                stderr=bytes(streams[stderr_descriptor]),
            )
        except OSError as error:
            self._kill(process)
            raise FaultControllerHostError("fault_host_command_io_invalid") from error
        finally:
            selector.close()
            process.stdout.close()
            process.stderr.close()

    @staticmethod
    def _kill(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                # D-state等でreap不能でもcallerの安全側timeout境界を無期限化しない。
                pass
