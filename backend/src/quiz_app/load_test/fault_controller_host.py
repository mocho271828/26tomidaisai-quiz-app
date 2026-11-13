"""AWS負荷試験host上の固定障害操作とsystemd復旧timer facade。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, cast

from quiz_app.load_test.controller_contracts import manifest_context
from quiz_app.load_test.fault_control_probe import TOXIPROXY_CONTROL_ORIGIN
from quiz_app.load_test.fault_controller_contracts import (
    FaultAction,
    FaultCommandIdentity,
    FaultControllerIdentity,
    FaultMutationResult,
    FaultTarget,
    FaultTargetObservation,
    FaultTopologyIdentity,
    RecoveryExecutionReceipt,
    RecoveryTimerObservation,
    RecoveryTimerPlan,
)
from quiz_app.load_test.fault_host_commands import (
    FaultHostCommands,
    command_argv_sha256,
)
from quiz_app.load_test.fault_host_config import (
    FaultControllerHostError,
    FaultHostConfiguration,
    load_fault_host_configuration,
)
from quiz_app.load_test.fault_host_executor import (
    CommandEnvironment,
    CommandExecutor,
    CommandResult,
    SubprocessCommandExecutor,
)
from quiz_app.load_test.fault_host_systemd import (
    HostPreflightObservation,
    SystemdRecoveryManager,
)
from quiz_app.load_test.fault_recovery_snapshot import (
    FaultRecoveryComposeSnapshot,
    validate_fault_recovery_compose_snapshot,
)

Clock = Callable[[], datetime]


class FaultHostAdapter:
    """controller向けに固定Compose操作と復旧timerだけを公開する。"""

    def __init__(
        self,
        configuration: FaultHostConfiguration,
        recovery_snapshot: FaultRecoveryComposeSnapshot,
        executor: CommandExecutor | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.configuration = configuration
        recovery_snapshot = validate_fault_recovery_compose_snapshot(
            configuration,
            recovery_snapshot,
        )
        commands = FaultHostCommands(
            configuration,
            recovery_snapshot,
            executor if executor is not None else SubprocessCommandExecutor(),
        )
        self._commands = commands
        self._systemd = SystemdRecoveryManager(configuration, commands)
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._docker_compose_version: str | None = None
        self._docker_engine_version: str | None = None
        self._fault_control_network_sha256: str | None = None
        self._fault_control_network_id: str | None = None
        self._fault_control_container_id: str | None = None
        self._toxiproxy_container_sha256: str | None = None

    def preflight(self) -> HostPreflightObservation:
        self._require_docker_runtime()
        return self._systemd.preflight()

    def recovery_preflight(self) -> None:
        """failed-resume時にhostと固定Docker endpointだけを再照合する。"""

        self._require_docker_runtime()
        self._commands.verify_execution_host()
        self._commands.verify_docker_context()

    def bind_docker_compose_runtime(self) -> str:
        """実Compose versionを初回identityへ固定し、以後のdriftを拒否する。"""

        self._require_docker_compose_runtime()
        if self._docker_compose_version is None:
            raise FaultControllerHostError("fault_host_compose_version_invalid")
        return self._docker_compose_version

    def bind_docker_engine_runtime(self) -> str:
        """実Engine versionを初回identityへ固定し、以後のdriftを拒否する。"""

        self._require_docker_engine_runtime()
        if self._docker_engine_version is None:
            raise FaultControllerHostError("fault_host_engine_version_invalid")
        return self._docker_engine_version

    def bind_fault_control_network_runtime(self) -> str:
        """実network隔離構成を初回identityへ固定し、以後のdriftを拒否する。"""

        self._require_fault_control_network_runtime()
        if self._fault_control_network_sha256 is None:
            raise FaultControllerHostError("fault_host_control_network_invalid")
        return self._fault_control_network_sha256

    def bind_toxiproxy_container_runtime(self) -> str:
        """実Toxiproxy daemonを初回identityへ固定し、以後のdriftを拒否する。"""

        if self._fault_control_container_id is None:
            self._require_fault_control_network_runtime()
        self._require_toxiproxy_container_runtime()
        if self._toxiproxy_container_sha256 is None:
            raise FaultControllerHostError("fault_host_toxiproxy_container_invalid")
        return self._toxiproxy_container_sha256

    def _require_docker_runtime(self) -> None:
        self._require_docker_compose_runtime()
        self._require_docker_engine_runtime()
        self._require_fault_control_network_runtime()
        self._require_toxiproxy_container_runtime()

    def _require_docker_compose_runtime(self) -> None:
        current = self._commands.read_docker_compose_version()
        if self._docker_compose_version is None:
            self._docker_compose_version = current
        elif current != self._docker_compose_version:
            raise FaultControllerHostError("fault_host_compose_version_invalid")

    def _require_docker_engine_runtime(self) -> None:
        current = self._commands.read_docker_engine_version()
        if self._docker_engine_version is None:
            self._docker_engine_version = current
        elif current != self._docker_engine_version:
            raise FaultControllerHostError("fault_host_engine_version_invalid")

    def _require_fault_control_network_runtime(self) -> None:
        current = self._commands.read_fault_control_network()
        if self._fault_control_network_sha256 is None:
            self._fault_control_network_sha256 = current.identity_sha256
        elif current.identity_sha256 != self._fault_control_network_sha256:
            raise FaultControllerHostError("fault_host_control_network_invalid")
        self._fault_control_container_id = current.container_id
        self._fault_control_network_id = current.network_id

    def _require_toxiproxy_container_runtime(self) -> None:
        current = self._commands.read_toxiproxy_container()
        if (
            current.container_id != self._fault_control_container_id
            or current.control_network_id != self._fault_control_network_id
        ):
            raise FaultControllerHostError("fault_host_toxiproxy_container_invalid")
        if self._toxiproxy_container_sha256 is None:
            self._toxiproxy_container_sha256 = current.identity_sha256
        elif current.identity_sha256 != self._toxiproxy_container_sha256:
            raise FaultControllerHostError("fault_host_toxiproxy_container_invalid")

    def observe_target(self, target: FaultTarget) -> FaultTargetObservation:
        return self._commands.observe_target(target)

    def apply_action(
        self,
        target: FaultTarget,
        action: FaultAction,
        expected_before: FaultTargetObservation,
    ) -> FaultMutationResult:
        return self._commands.apply_action(target, action, expected_before)

    def claim_recovery_ownership(self, plan: RecoveryTimerPlan) -> bool:
        return self._systemd.claim_recovery_ownership(plan)

    def revoke_recovery_ownership(self, plan: RecoveryTimerPlan) -> None:
        self._systemd.revoke_recovery_ownership(plan)

    def read_recovery_receipt(
        self,
        plan: RecoveryTimerPlan,
        observed_at: datetime,
    ) -> RecoveryExecutionReceipt | None:
        return self._systemd.read_recovery_receipt(plan, observed_at)

    def arm_recovery_timer(self, plan: RecoveryTimerPlan) -> RecoveryTimerObservation:
        return self._systemd.arm_recovery_timer(plan, now=self._clock())

    def read_recovery_timer(self, plan: RecoveryTimerPlan) -> RecoveryTimerObservation:
        return self._systemd.read_recovery_timer(plan)

    def compose_prefix(self) -> tuple[str, ...]:
        return self._commands.compose_prefix()

    def fault_control_command(
        self,
        operation: Literal["probe", "disable", "enable"],
    ) -> tuple[str, ...]:
        return self._commands.fault_control_command(operation)

    def api_state_command(self) -> tuple[str, ...]:
        return self._commands.api_state_command()

    def recovery_command(self, target: FaultTarget) -> tuple[str, ...]:
        return self._commands.recovery_command(target)

    def recovery_command_sha256(self, target: FaultTarget) -> str:
        return self._commands.recovery_command_sha256(target)

    @property
    def recovery_compose_sha256(self) -> str:
        """journal identityへseal済みCompose contentを明示的に結合する。"""

        return self._commands.recovery_snapshot.sha256

    def action_command_sha256(self, target: FaultTarget, action: FaultAction) -> str:
        if action not in {"disable", "enable"}:
            raise FaultControllerHostError("fault_host_action_invalid")
        argv = (
            self._commands.recovery_command(target)
            if action == "enable"
            else self._commands.disable_command(target)
        )
        return command_argv_sha256(argv)


def build_fault_controller_identity(
    configuration: FaultHostConfiguration,
    adapter: FaultHostAdapter,
) -> FaultControllerIdentity:
    """検証済みmanifest/release/topologyと実際の固定argvを一つのidentityへ束ねる。"""

    if adapter.configuration != configuration:
        raise FaultControllerHostError("fault_host_configuration_mismatch")
    proxy = configuration.toxiproxy_proxy
    topology = FaultTopologyIdentity(
        configuration_sha256=configuration.toxiproxy_configuration_sha256,
        docker_compose_version=adapter.bind_docker_compose_runtime(),
        docker_engine_version=adapter.bind_docker_engine_runtime(),
        fault_control_network_sha256=adapter.bind_fault_control_network_runtime(),
        toxiproxy_container_sha256=adapter.bind_toxiproxy_container_runtime(),
        recovery_compose_sha256=adapter.recovery_compose_sha256,
        proxy_name=proxy.name,
        control_origin=TOXIPROXY_CONTROL_ORIGIN,
        probe_command_sha256=command_argv_sha256(adapter.fault_control_command("probe")),
        api_probe_command_sha256=command_argv_sha256(adapter.api_state_command()),
        redis_hostname=configuration.redis_hostname,
        listen=proxy.listen,
        upstream=proxy.upstream,
    )
    identities = tuple(
        FaultCommandIdentity(
            slot_number=slot_number,
            target=action.target,
            action=action.action,
            command_sha256=adapter.action_command_sha256(action.target, action.action),
        )
        for slot_number, action in enumerate(
            configuration.manifest.scenario.fault_schedule,
            start=1,
        )
    )
    if len(identities) != 4:
        raise FaultControllerHostError("fault_host_fault_schedule_invalid")
    commands = cast(
        tuple[
            FaultCommandIdentity,
            FaultCommandIdentity,
            FaultCommandIdentity,
            FaultCommandIdentity,
        ],
        identities,
    )
    return FaultControllerIdentity(
        context=manifest_context(configuration.manifest),
        release=configuration.release_identity,
        topology=topology,
        commands=commands,
    )


__all__ = (
    "CommandEnvironment",
    "CommandExecutor",
    "CommandResult",
    "FaultControllerHostError",
    "FaultHostAdapter",
    "FaultHostConfiguration",
    "FaultRecoveryComposeSnapshot",
    "HostPreflightObservation",
    "SubprocessCommandExecutor",
    "build_fault_controller_identity",
    "command_argv_sha256",
    "load_fault_host_configuration",
)
