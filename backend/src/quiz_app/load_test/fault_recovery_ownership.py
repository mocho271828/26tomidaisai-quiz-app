"""事前armした復旧timerをcontroller所有のdisableだけへ限定する。"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from quiz_app.load_test.controller_contracts import canonical_json_bytes
from quiz_app.load_test.fault_controller_contracts import (
    RecoveryExecutionReceipt,
    RecoveryTimerPlan,
    recovery_execution_receipt_sha256,
)
from quiz_app.load_test.fault_host_config import (
    FaultControllerHostError,
    FaultHostConfiguration,
)

_STATE_FORMAT: Final = "load-test-fault-recovery-ownership-v1"


class FaultRecoveryOwnershipStore:
    """user home直下のrun固有directoryでownershipと実行receiptを管理する。"""

    def __init__(self, configuration: FaultHostConfiguration) -> None:
        self._configuration = configuration
        run_id = configuration.manifest.run_id.hex
        self.directory = configuration.home_directory / f".gs-loadtest-fault-{run_id}"

    def marker_path(self, plan: RecoveryTimerPlan) -> Path:
        return self.directory / self._name(plan, "owned")

    def receipt_path(self, plan: RecoveryTimerPlan) -> Path:
        return self.directory / self._name(plan, "receipt")

    def prepare_timer(self, plan: RecoveryTimerPlan) -> None:
        """arm前にdirectory inventoryと既存markerを検証し、receipt衝突を拒否する。"""

        descriptor = self._open_directory()
        try:
            marker_name = self.marker_path(plan).name
            if marker_name in os.listdir(descriptor):
                self._read_marker(descriptor, plan)
            if self.receipt_path(plan).name in os.listdir(descriptor):
                raise FaultControllerHostError("fault_host_recovery_receipt_invalid")
        finally:
            os.close(descriptor)

    def claim(self, plan: RecoveryTimerPlan) -> bool:
        """disable intentを先行fsyncし、新規取得だけTrueで返す。"""

        descriptor = self._open_directory()
        name = self.marker_path(plan).name
        temporary_name = f".{name}.tmp"
        body = self._marker_body(plan)
        file_descriptor = -1
        try:
            if name in os.listdir(descriptor):
                self._read_marker(descriptor, plan)
                return False
            try:
                file_descriptor = os.open(
                    temporary_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=descriptor,
                )
            except FileExistsError:
                raise FaultControllerHostError(
                    "fault_host_recovery_ownership_write_failed"
                ) from None
            os.fchmod(file_descriptor, 0o600)
            written = 0
            while written < len(body):
                count = os.write(file_descriptor, body[written:])
                if count <= 0:
                    raise OSError
                written += count
            os.fsync(file_descriptor)
            self._require_regular(os.fstat(file_descriptor), len(body))
            try:
                os.link(
                    temporary_name,
                    name,
                    src_dir_fd=descriptor,
                    dst_dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError:
                self._read_marker(descriptor, plan)
                return False
            os.unlink(temporary_name, dir_fd=descriptor)
            os.fsync(descriptor)
            self._read_marker(descriptor, plan)
            return True
        except FaultControllerHostError:
            raise
        except OSError as error:
            raise FaultControllerHostError("fault_host_recovery_ownership_write_failed") from error
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)
            try:
                os.unlink(temporary_name, dir_fd=descriptor)
                os.fsync(descriptor)
            except FileNotFoundError:
                pass
            except OSError:
                # 元のclaim失敗を保持する。次回inventory検証は残存tempを拒否する。
                pass
            os.close(descriptor)

    def revoke(self, plan: RecoveryTimerPlan) -> None:
        """future timerが実行すべきでないownership markerを除去する。"""

        descriptor = self._open_directory()
        name = self.marker_path(plan).name
        try:
            if name not in os.listdir(descriptor):
                return
            self._read_marker(descriptor, plan)
            os.unlink(name, dir_fd=descriptor)
            os.fsync(descriptor)
        except FaultControllerHostError:
            raise
        except OSError as error:
            raise FaultControllerHostError("fault_host_recovery_ownership_write_failed") from error
        finally:
            os.close(descriptor)

    def read_receipt(
        self,
        plan: RecoveryTimerPlan,
        *,
        observed_at: datetime,
    ) -> RecoveryExecutionReceipt | None:
        """ExecStartPostが作成したsecure empty directoryをreceiptへ正規化する。"""

        descriptor = self._open_directory()
        receipt_descriptor = -1
        try:
            name = self.receipt_path(plan).name
            try:
                before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                receipt_descriptor = os.open(
                    name,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                return None
            opened = os.fstat(receipt_descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or stat.S_IMODE(opened.st_mode) != 0o700
                or opened.st_uid != os.geteuid()
                or opened.st_gid != os.getegid()
                or os.listdir(receipt_descriptor)
            ):
                raise FaultControllerHostError("fault_host_recovery_receipt_invalid")
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise FaultControllerHostError("fault_host_recovery_receipt_invalid")
            recorded_at = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
                microseconds=opened.st_mtime_ns // 1_000
            )
            if recorded_at < plan.trigger_at:
                raise FaultControllerHostError("fault_host_recovery_receipt_invalid")
            if recorded_at > observed_at:
                # checked_at後にExecStartPostが完了した競合は次pollで再検証する。
                return None
            return RecoveryExecutionReceipt(
                unit_name=plan.unit_name,
                trigger_at=plan.trigger_at,
                recorded_at=recorded_at,
                receipt_sha256=recovery_execution_receipt_sha256(plan, recorded_at),
            )
        except FaultControllerHostError:
            raise
        except OSError as error:
            raise FaultControllerHostError("fault_host_recovery_receipt_invalid") from error
        finally:
            if receipt_descriptor >= 0:
                os.close(receipt_descriptor)
            os.close(descriptor)

    def _open_directory(self) -> int:
        home_descriptor = -1
        directory_descriptor = -1
        try:
            home_descriptor = os.open(
                self._configuration.home_directory,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.mkdir(self.directory.name, 0o700, dir_fd=home_descriptor)
                os.fsync(home_descriptor)
            except FileExistsError:
                pass
            directory_descriptor = os.open(
                self.directory.name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=home_descriptor,
            )
            status = os.fstat(directory_descriptor)
            if (
                not stat.S_ISDIR(status.st_mode)
                or stat.S_IMODE(status.st_mode) != 0o700
                or status.st_uid != os.geteuid()
                or status.st_gid != os.getegid()
            ):
                raise FaultControllerHostError("fault_host_recovery_ownership_directory_invalid")
            expected_names = {
                f"slot-{slot_number:02d}-{action.target}.{suffix}"
                for slot_number, action in enumerate(
                    self._configuration.manifest.scenario.fault_schedule,
                    start=1,
                )
                if action.action == "enable"
                for suffix in ("owned", "receipt")
            }
            if set(os.listdir(directory_descriptor)) - expected_names:
                raise FaultControllerHostError("fault_host_recovery_ownership_directory_invalid")
            result = directory_descriptor
            directory_descriptor = -1
            return result
        except FaultControllerHostError:
            raise
        except OSError as error:
            raise FaultControllerHostError(
                "fault_host_recovery_ownership_directory_invalid"
            ) from error
        finally:
            if directory_descriptor >= 0:
                os.close(directory_descriptor)
            if home_descriptor >= 0:
                os.close(home_descriptor)

    @staticmethod
    def _name(plan: RecoveryTimerPlan, suffix: str) -> str:
        return f"slot-{plan.slot_number:02d}-{plan.target}.{suffix}"

    def _marker_body(self, plan: RecoveryTimerPlan) -> bytes:
        return (
            canonical_json_bytes(
                {
                    "command_sha256": plan.command_sha256,
                    "format": _STATE_FORMAT,
                    "run_id": self._configuration.manifest.run_id,
                    "slot_number": plan.slot_number,
                    "target": plan.target,
                    "trigger_at": plan.trigger_at,
                    "unit_name": plan.unit_name,
                }
            )
            + b"\n"
        )

    def _read_marker(self, directory_descriptor: int, plan: RecoveryTimerPlan) -> None:
        name = self.marker_path(plan).name
        expected = self._marker_body(plan)
        file_descriptor = -1
        try:
            before = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
            file_descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_descriptor,
            )
            opened = os.fstat(file_descriptor)
            self._require_regular(
                opened,
                len(expected),
                code="fault_host_recovery_ownership_invalid",
            )
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise FaultControllerHostError("fault_host_recovery_ownership_invalid")
            body = b""
            while len(body) < len(expected):
                chunk = os.read(file_descriptor, len(expected) - len(body))
                if not chunk:
                    break
                body += chunk
            if body != expected:
                raise FaultControllerHostError("fault_host_recovery_ownership_invalid")
        except FileNotFoundError:
            raise
        except FaultControllerHostError:
            raise
        except OSError as error:
            raise FaultControllerHostError("fault_host_recovery_ownership_invalid") from error
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)

    @staticmethod
    def _require_regular(
        status: os.stat_result,
        expected_size: int,
        *,
        code: str = "fault_host_recovery_ownership_invalid",
    ) -> None:
        if (
            not stat.S_ISREG(status.st_mode)
            or stat.S_IMODE(status.st_mode) != 0o600
            or status.st_uid != os.geteuid()
            or status.st_gid != os.getegid()
            or status.st_nlink != 1
            or status.st_size != expected_size
        ):
            raise FaultControllerHostError(code)
