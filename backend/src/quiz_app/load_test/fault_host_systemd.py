"""systemd user manager preflightとtransient recovery timerの厳密readback。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Final

from quiz_app.load_test.controller_contracts import canonical_json_bytes
from quiz_app.load_test.fault_controller_contracts import (
    RecoveryExecutionReceipt,
    RecoveryTimerObservation,
    RecoveryTimerPlan,
)
from quiz_app.load_test.fault_host_commands import (
    LOGINCTL_BIN,
    RECOVERY_RUNTIME_SECONDS,
    SYSTEMCTL_BIN,
    SYSTEMD_RUN_BIN,
    TIMEDATECTL_BIN,
    FaultHostCommands,
)
from quiz_app.load_test.fault_host_config import (
    FaultControllerHostError,
    FaultHostConfiguration,
)
from quiz_app.load_test.fault_recovery_ownership import FaultRecoveryOwnershipStore

MINIMUM_SYSTEMD_VERSION: Final = 254
MINIMUM_TIMER_LEAD_SECONDS: Final = 30
TEST_BIN: Final = "/usr/bin/test"
MKDIR_BIN: Final = "/usr/bin/mkdir"
_SYSTEMD_VERSION: Final = re.compile(rb"^systemd ([0-9]{3,})\b")
# Manager.VersionはCLIの表示ではなくGIT_VERSIONを返す。Ubuntuの255.4-1ubuntu8.xも
# 制限付きで受理し、CLIと同じmajorであることはpreflightで別途照合する。
_SYSTEMD_MANAGER_VERSION: Final = re.compile(
    rb"^([0-9]{3,})(?:[.+:~^_-][A-Za-z0-9][A-Za-z0-9.+:~^_-]{0,126})?"
    rb"(?: \([A-Za-z0-9.+:~^_-]{1,128}\))?\n$"
)
_SYSTEMD_TIME: Final = re.compile(
    r"^(?:[A-Z][a-z]{2} )?(\d{4}-\d{2}-\d{2}) "
    r"(\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?) UTC$"
)
_DURATION_PART: Final = re.compile(r"([0-9]+)(us|ms|s|min|h)")


@dataclass(frozen=True, slots=True)
class HostPreflightObservation:
    systemd_version: int
    ntp_synchronized: bool
    linger_enabled: bool
    user_manager_active: bool


class SystemdRecoveryManager:
    """fixed commandを事前armし、timer/service双方のpropertyを照合する。"""

    def __init__(
        self,
        configuration: FaultHostConfiguration,
        commands: FaultHostCommands,
    ) -> None:
        self.configuration = configuration
        self._commands = commands
        self._ownership = FaultRecoveryOwnershipStore(configuration)

    def claim_recovery_ownership(self, plan: RecoveryTimerPlan) -> bool:
        self._validate_timer_plan(plan)
        return self._ownership.claim(plan)

    def revoke_recovery_ownership(self, plan: RecoveryTimerPlan) -> None:
        self._validate_timer_plan(plan)
        self._ownership.revoke(plan)

    def read_recovery_receipt(
        self,
        plan: RecoveryTimerPlan,
        observed_at: datetime,
    ) -> RecoveryExecutionReceipt | None:
        self._validate_timer_plan(plan)
        return self._ownership.read_receipt(plan, observed_at=observed_at)

    def preflight(self) -> HostPreflightObservation:
        version_body = self._commands.require_success(
            self._commands.run((SYSTEMD_RUN_BIN, "--version"), systemd=True),
            "fault_host_systemd_unavailable",
        )
        match = _SYSTEMD_VERSION.match(version_body)
        if match is None or int(match.group(1)) < MINIMUM_SYSTEMD_VERSION:
            raise FaultControllerHostError("fault_host_systemd_version_invalid")
        version = int(match.group(1))

        manager_version_body = self._commands.require_success(
            self._commands.run(
                (
                    SYSTEMCTL_BIN,
                    "--user",
                    "--no-pager",
                    "show",
                    "--property=Version",
                    "--value",
                ),
                systemd=True,
            ),
            "fault_host_user_manager_unavailable",
        )
        manager_match = _SYSTEMD_MANAGER_VERSION.fullmatch(manager_version_body)
        if manager_match is None or int(manager_match.group(1)) != version:
            raise FaultControllerHostError("fault_host_systemd_version_invalid")

        ntp = self._commands.require_success(
            self._commands.run(
                (TIMEDATECTL_BIN, "show", "--property=NTPSynchronized", "--value"),
                systemd=True,
            ),
            "fault_host_ntp_unavailable",
        )
        if ntp != b"yes\n":
            raise FaultControllerHostError("fault_host_ntp_unsynchronized")
        linger = self._commands.require_success(
            self._commands.run(
                (
                    LOGINCTL_BIN,
                    "show-user",
                    str(os.geteuid()),
                    "--property=Linger",
                    "--value",
                ),
                systemd=True,
            ),
            "fault_host_linger_unavailable",
        )
        if linger != b"yes\n":
            raise FaultControllerHostError("fault_host_linger_disabled")
        manager = self._commands.require_success(
            self._commands.run(
                (SYSTEMCTL_BIN, "--user", "--no-pager", "is-active", "default.target"),
                systemd=True,
            ),
            "fault_host_user_manager_unavailable",
        )
        if manager != b"active\n":
            raise FaultControllerHostError("fault_host_user_manager_inactive")
        self._commands.verify_execution_host()
        self._commands.verify_docker_context()
        return HostPreflightObservation(
            systemd_version=version,
            ntp_synchronized=True,
            linger_enabled=True,
            user_manager_active=True,
        )

    def arm_recovery_timer(
        self,
        plan: RecoveryTimerPlan,
        *,
        now: datetime,
    ) -> RecoveryTimerObservation:
        self._validate_timer_plan(plan)
        self._ownership.prepare_timer(plan)
        if now.tzinfo is None or plan.trigger_at <= now.astimezone(UTC) + timedelta(
            seconds=MINIMUM_TIMER_LEAD_SECONDS
        ):
            raise FaultControllerHostError("fault_host_timer_lead_invalid")
        result = self._commands.run(self._systemd_run_argv(plan), systemd=True)
        try:
            observation = self.read_recovery_timer(plan)
        except FaultControllerHostError as error:
            if result.returncode != 0:
                raise FaultControllerHostError("fault_host_timer_arm_failed") from error
            raise
        # systemd-runは同名unit既存時に1を返す。exact readback時だけresumeとして認める。
        if result.returncode not in {0, 1}:
            raise FaultControllerHostError("fault_host_timer_arm_failed")
        return observation

    def read_recovery_timer(self, plan: RecoveryTimerPlan) -> RecoveryTimerObservation:
        self._validate_timer_plan(plan)
        base = plan.unit_name
        timer_keys = (
            "Id",
            "Description",
            "LoadState",
            "ActiveState",
            "SubState",
            "Conditions",
            "Asserts",
            "Unit",
            "TimersCalendar",
            "TimersMonotonic",
            "OnClockChange",
            "OnTimezoneChange",
            "NextElapseUSecRealtime",
            "NextElapseUSecMonotonic",
            "AccuracyUSec",
            "RandomizedDelayUSec",
            "Persistent",
            "RemainAfterElapse",
            "Result",
            "Transient",
        )
        service_keys = (
            "Id",
            "Description",
            "LoadState",
            "ActiveState",
            "SubState",
            "Conditions",
            "Asserts",
            "Type",
            "ExecCondition",
            "ExecStartPre",
            "ExecStart",
            "ExecStartEx",
            "ExecStartPost",
            "ExecReload",
            "ExecStop",
            "ExecStopPost",
            "TimeoutStartUSec",
            "RuntimeMaxUSec",
            "TimeoutStopUSec",
            "Restart",
            "SuccessExitStatus",
            "RestartPreventExitStatus",
            "RestartForceExitStatus",
            "KillMode",
            "SendSIGKILL",
            "UMask",
            "NoNewPrivileges",
            "StandardInput",
            "StandardOutput",
            "StandardError",
            "Transient",
        )
        timer = self._read_unit_properties(f"{base}.timer", timer_keys)
        service = self._read_unit_properties(f"{base}.service", service_keys)
        if (
            timer["Id"] != f"{base}.timer"
            or timer["Description"] != self._timer_description(plan)
            or timer["LoadState"] != "loaded"
            or timer["ActiveState"] != "active"
            or timer["SubState"] != "waiting"
            or timer["Conditions"] != ""
            or timer["Asserts"] != ""
            or timer["Unit"] != f"{base}.service"
            or not _calendar_timer_matches(timer["TimersCalendar"], plan.trigger_at)
            or timer["TimersMonotonic"] != ""
            or timer["OnClockChange"] != "no"
            or timer["OnTimezoneChange"] != "no"
            or _parse_systemd_timestamp(timer["NextElapseUSecRealtime"]) != plan.trigger_at
            or timer["NextElapseUSecMonotonic"] != "0"
            or _parse_systemd_duration(timer["AccuracyUSec"]) != 1
            or _parse_systemd_duration(timer["RandomizedDelayUSec"]) != 0
            or timer["Persistent"] != "no"
            or timer["RemainAfterElapse"] != "no"
            or timer["Result"] != "success"
            or timer["Transient"] != "yes"
        ):
            raise FaultControllerHostError("fault_host_timer_readback_mismatch")
        exec_argv = self._commands.recovery_command(plan.target)
        condition_argv = (TEST_BIN, "-f", str(self._ownership.marker_path(plan)))
        receipt_argv = (
            MKDIR_BIN,
            "--mode=0700",
            "--",
            str(self._ownership.receipt_path(plan)),
        )
        if (
            service["Id"] != f"{base}.service"
            or service["Description"] != self._timer_description(plan)
            or service["LoadState"] != "loaded"
            or service["ActiveState"] != "inactive"
            or service["SubState"] != "dead"
            or service["Conditions"] != ""
            or service["Asserts"] != ""
            or service["Type"] != "oneshot"
            or service["ExecCondition"] != _format_exec_start(condition_argv)
            or service["ExecStartPre"] != ""
            or service["ExecStart"] != _format_exec_start(exec_argv)
            or service["ExecStartEx"] != _format_exec_start_ex(exec_argv)
            or service["ExecStartPost"] != _format_exec_start(receipt_argv)
            or service["ExecReload"] != ""
            or service["ExecStop"] != ""
            or service["ExecStopPost"] != ""
            or _parse_systemd_duration(service["TimeoutStartUSec"])
            != RECOVERY_RUNTIME_SECONDS * 1_000_000
            or _parse_systemd_duration(service["RuntimeMaxUSec"])
            != RECOVERY_RUNTIME_SECONDS * 1_000_000
            or _parse_systemd_duration(service["TimeoutStopUSec"]) != 10_000_000
            or service["Restart"] != "no"
            or service["SuccessExitStatus"] != ""
            or service["RestartPreventExitStatus"] != ""
            or service["RestartForceExitStatus"] != ""
            or service["KillMode"] != "control-group"
            or service["SendSIGKILL"] != "yes"
            or service["UMask"] != "0077"
            or service["NoNewPrivileges"] != "yes"
            or service["StandardInput"] != "null"
            or service["StandardOutput"] != "null"
            or service["StandardError"] != "null"
            or service["Transient"] != "yes"
        ):
            raise FaultControllerHostError("fault_host_timer_readback_mismatch")
        properties_digest = sha256(
            canonical_json_bytes({"service": service, "timer": timer})
        ).hexdigest()
        return RecoveryTimerObservation(
            unit_name=plan.unit_name,
            trigger_at=plan.trigger_at,
            properties_sha256=properties_digest,
        )

    def _read_unit_properties(
        self,
        unit_name: str,
        keys: tuple[str, ...],
    ) -> dict[str, str]:
        argv = (
            SYSTEMCTL_BIN,
            "--user",
            "--no-pager",
            "show",
            unit_name,
            *(f"--property={key}" for key in keys),
        )
        body = self._commands.require_success(
            self._commands.run(argv, systemd=True), "fault_host_timer_readback_failed"
        )
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise FaultControllerHostError("fault_host_timer_readback_invalid") from error
        properties: dict[str, str] = {}
        for line in text.splitlines():
            key, separator, value = line.partition("=")
            if not separator or key not in keys or key in properties:
                raise FaultControllerHostError("fault_host_timer_readback_invalid")
            properties[key] = value
        if set(properties) != set(keys):
            raise FaultControllerHostError("fault_host_timer_readback_invalid")
        return properties

    def _validate_timer_plan(self, plan: RecoveryTimerPlan) -> None:
        matches = tuple(
            (slot_number, action)
            for slot_number, action in enumerate(
                self.configuration.manifest.scenario.fault_schedule,
                start=1,
            )
            if action.target == plan.target and action.action == "enable"
        )
        if len(matches) != 1:
            raise FaultControllerHostError("fault_host_timer_plan_invalid")
        slot_number, action = matches[0]
        expected_unit = (
            f"gs-loadtest-{self.configuration.manifest.run_id.hex}-{plan.target}-recover"
        )
        trigger_at = self.configuration.manifest.scheduled_start_at + timedelta(
            seconds=action.offset_seconds
        )
        if (
            plan.slot_number != slot_number
            or plan.unit_name != expected_unit
            or plan.trigger_at != trigger_at
            or plan.command_sha256 != self._commands.recovery_command_sha256(plan.target)
        ):
            raise FaultControllerHostError("fault_host_timer_plan_invalid")

    def _systemd_run_argv(self, plan: RecoveryTimerPlan) -> tuple[str, ...]:
        return (
            SYSTEMD_RUN_BIN,
            "--user",
            "--no-ask-password",
            "--quiet",
            f"--unit={plan.unit_name}",
            f"--description={self._timer_description(plan)}",
            f"--on-calendar={_format_systemd_timestamp(plan.trigger_at)}",
            "--timer-property=AccuracySec=1us",
            "--timer-property=RandomizedDelaySec=0",
            "--timer-property=Persistent=no",
            # systemd-run自身もfalseを付与するため、期待値を明示してreadbackと揃える。
            "--timer-property=RemainAfterElapse=no",
            # oneshotのExecStartPostはExecStart正常終了後にだけ実行され、
            # receiptを回復command成功の証跡として扱える。
            "--property=Type=oneshot",
            "--property=Restart=no",
            f"--property=TimeoutStartSec={RECOVERY_RUNTIME_SECONDS}s",
            f"--property=RuntimeMaxSec={RECOVERY_RUNTIME_SECONDS}s",
            "--property=TimeoutStopSec=10s",
            "--property=KillMode=control-group",
            "--property=SendSIGKILL=yes",
            "--property=UMask=0077",
            "--property=NoNewPrivileges=yes",
            f"--property=ExecCondition={TEST_BIN} -f {self._ownership.marker_path(plan)}",
            (
                f"--property=ExecStartPost={MKDIR_BIN} --mode=0700 -- "
                f"{self._ownership.receipt_path(plan)}"
            ),
            "--property=StandardInput=null",
            "--property=StandardOutput=null",
            "--property=StandardError=null",
            "--expand-environment=no",
            "--",
            *self._commands.recovery_command(plan.target),
        )

    def _timer_description(self, plan: RecoveryTimerPlan) -> str:
        return (
            f"gs-loadtest:{self.configuration.manifest.run_id.hex}:"
            f"{plan.target}:{plan.command_sha256}"
        )


def _format_systemd_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise FaultControllerHostError("fault_host_timer_plan_invalid")
    utc_value = value.astimezone(UTC)
    base = utc_value.strftime("%Y-%m-%d %H:%M:%S")
    fraction = f".{utc_value.microsecond:06d}" if utc_value.microsecond else ""
    return f"{base}{fraction} UTC"


def _parse_systemd_timestamp(value: str) -> datetime:
    if value.isdecimal():
        try:
            return datetime.fromtimestamp(int(value) / 1_000_000, UTC)
        except (OverflowError, OSError, ValueError) as error:
            raise FaultControllerHostError("fault_host_timer_readback_invalid") from error
    match = _SYSTEMD_TIME.fullmatch(value)
    if match is None:
        raise FaultControllerHostError("fault_host_timer_readback_invalid")
    try:
        return datetime.fromisoformat(f"{match.group(1)}T{match.group(2)}+00:00")
    except ValueError as error:
        raise FaultControllerHostError("fault_host_timer_readback_invalid") from error


def _parse_systemd_duration(value: str) -> int:
    if len(value) > 128:
        raise FaultControllerHostError("fault_host_timer_readback_invalid")
    if value == "0":
        return 0
    if value.isdecimal():
        return int(value)
    units = {"us": 1, "ms": 1_000, "s": 1_000_000, "min": 60_000_000, "h": 3_600_000_000}
    position = 0
    total = 0
    for match in _DURATION_PART.finditer(value):
        if value[position : match.start()] not in {"", " "}:
            raise FaultControllerHostError("fault_host_timer_readback_invalid")
        total += int(match.group(1)) * units[match.group(2)]
        position = match.end()
    if position != len(value) or position == 0:
        raise FaultControllerHostError("fault_host_timer_readback_invalid")
    return total


def _format_exec_start(argv: tuple[str, ...]) -> str:
    """失敗無視を許さないlegacy ExecStart propertyの未実行shapeを固定する。"""

    command = " ".join(argv)
    return (
        f"{{ path={argv[0]} ; argv[]={command} ; ignore_errors=no ; "
        "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
    )


def _format_exec_start_ex(argv: tuple[str, ...]) -> str:
    """systemd-runの環境展開禁止flagを含むExecStartExを固定する。"""

    command = " ".join(argv)
    return (
        f"{{ path={argv[0]} ; argv[]={command} ; flags=no-env-expand ; "
        "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
    )


def _calendar_timer_matches(value: str, trigger_at: datetime) -> bool:
    prefix = "{ OnCalendar="
    separator = " ; next_elapse="
    if (
        not value.startswith(prefix)
        or not value.endswith(" }")
        or value.count("{") != 1
        or value.count("}") != 1
        or value.count(separator) != 1
    ):
        return False
    specification, next_elapse = value[len(prefix) : -2].split(separator)
    try:
        return (
            _parse_systemd_timestamp(specification) == trigger_at
            and _parse_systemd_timestamp(next_elapse) == trigger_at
        )
    except FaultControllerHostError:
        return False
