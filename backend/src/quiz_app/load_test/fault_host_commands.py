"""障害注入hostのambient-clean command境界と固定Compose操作。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from quiz_app.load_test.controller_contracts import canonical_json_bytes
from quiz_app.load_test.fault_control_probe import (
    TOXIPROXY_CONTROL_ORIGIN,
    FaultControlCliResult,
)
from quiz_app.load_test.fault_controller_contracts import (
    FaultAction,
    FaultMutationResult,
    FaultTarget,
    FaultTargetObservation,
)
from quiz_app.load_test.fault_docker_network import (
    DOCKER_ENGINE_VERSION_FORMAT,
    FAULT_CONTROL_NETWORK,
    FAULT_CONTROL_NETWORK_FORMAT,
    FaultControlNetworkObservation,
    parse_docker_engine_version,
    parse_fault_control_network,
)
from quiz_app.load_test.fault_docker_runtime import (
    DOCKER_CONFIG_DIRECTORY,
    require_root_owned_docker_config_directory,
)
from quiz_app.load_test.fault_docker_toxiproxy import (
    TOXIPROXY_CONTAINER_FORMAT,
    TOXIPROXY_CONTAINER_NAME,
    ToxiproxyContainerObservation,
    parse_toxiproxy_container,
)
from quiz_app.load_test.fault_host_config import (
    DOCKER_ENDPOINT,
    FaultControllerHostError,
    FaultHostConfiguration,
    FaultMutationOutcomeUncertainError,
    FaultMutationPreconditionError,
    FaultTargetRecoveryPendingError,
)
from quiz_app.load_test.fault_host_executor import CommandExecutor, CommandResult
from quiz_app.load_test.fault_recovery_snapshot import (
    API_SECONDARY_SERVICE,
    COMPOSE_PROJECT,
    FAULT_CONTROL_SERVICE,
    FaultRecoveryComposeSnapshot,
)

API_SECONDARY_CONTAINER_PORT: Final = 8000

DOCKER_BIN: Final = "/usr/bin/docker"
EMPTY_ENV_FILE: Final = "/dev/null"
ENV_BIN: Final = "/usr/bin/env"
HOSTNAME_BIN: Final = "/usr/bin/hostname"
LOGINCTL_BIN: Final = "/usr/bin/loginctl"
SYSTEMCTL_BIN: Final = "/usr/bin/systemctl"
SYSTEMD_RUN_BIN: Final = "/usr/bin/systemd-run"
TIMEDATECTL_BIN: Final = "/usr/bin/timedatectl"
COMMAND_TIMEOUT_SECONDS: Final = 10.0
FAULT_CONTROL_RUNTIME_SECONDS: Final = 30.0
RECOVERY_RUNTIME_SECONDS: Final = 115
MINIMUM_COMPOSE_VERSION: Final = (2, 30, 0)
FIXED_PATH: Final = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_COMPOSE_VERSION: Final = re.compile(rb"^(2)\.([0-9]{1,3})\.([0-9]{1,3})\n$")


class _ComposePublisher(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    url: StrictStr = Field(alias="URL")
    target_port: StrictInt = Field(alias="TargetPort", ge=0, le=65535)
    published_port: StrictInt = Field(alias="PublishedPort", ge=0, le=65535)
    protocol: StrictStr = Field(alias="Protocol")


class _ComposePsRecord(BaseModel):
    """Compose JSONLのdocumented fieldと既知のformatter fieldだけを許可する。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    container_id: StrictStr = Field(alias="ID", pattern=r"^[0-9a-f]{64}$")
    name: StrictStr = Field(alias="Name", min_length=1, max_length=255)
    command: StrictStr = Field(alias="Command", max_length=4096)
    project: StrictStr = Field(alias="Project")
    service: StrictStr = Field(alias="Service")
    state: StrictStr = Field(alias="State")
    health: StrictStr = Field(alias="Health")
    exit_code: StrictInt = Field(alias="ExitCode")
    publishers: tuple[_ComposePublisher, ...] | None = Field(alias="Publishers")
    image: StrictStr | None = Field(default=None, alias="Image")
    created_at: StrictStr | None = Field(default=None, alias="CreatedAt")
    labels: StrictStr | None = Field(default=None, alias="Labels")
    local_volumes: StrictStr | None = Field(default=None, alias="LocalVolumes")
    mounts: StrictStr | None = Field(default=None, alias="Mounts")
    names: StrictStr | None = Field(default=None, alias="Names")
    networks: StrictStr | None = Field(default=None, alias="Networks")
    ports: StrictStr | None = Field(default=None, alias="Ports")
    running_for: StrictStr | None = Field(default=None, alias="RunningFor")
    size: StrictStr | None = Field(default=None, alias="Size")
    status: StrictStr | None = Field(default=None, alias="Status")


@dataclass(frozen=True, slots=True)
class _ApiState:
    record: _ComposePsRecord
    observation: FaultTargetObservation


class _DuplicateJsonKeyError(ValueError):
    """曖昧なcommand JSONを拒否する内部signal。"""


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError
        result[key] = value
    return result


def _reject_json_constant(_: str) -> None:
    raise ValueError


def _require_unambiguous_json(body: bytes, code: str) -> None:
    try:
        json.loads(
            body,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKeyError, ValueError) as error:
        raise FaultControllerHostError(code) from error


def command_argv_sha256(argv: tuple[str, ...]) -> str:
    """argv境界を曖昧な文字列連結なしでdigest化する。"""

    return sha256(json.dumps(argv, separators=(",", ":")).encode("ascii")).hexdigest()


class FaultHostCommands:
    """固定Docker Compose操作と、その出力検証を提供する。"""

    def __init__(
        self,
        configuration: FaultHostConfiguration,
        recovery_snapshot: FaultRecoveryComposeSnapshot,
        executor: CommandExecutor,
    ) -> None:
        self.configuration = configuration
        self.recovery_snapshot = recovery_snapshot
        self._executor = executor

    def base_environment(self) -> dict[str, str]:
        return {
            "HOME": str(self.configuration.home_directory),
            "PATH": FIXED_PATH,
            "LANG": "C",
            "LC_ALL": "C",
            "TZ": "UTC",
        }

    def systemd_environment(self) -> dict[str, str]:
        environment = self.base_environment()
        uid = os.geteuid()
        environment.update(
            {
                "XDG_RUNTIME_DIR": f"/run/user/{uid}",
                "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus",
                "SYSTEMD_COLORS": "0",
                "SYSTEMD_PAGER": "cat",
            }
        )
        return environment

    def compose_environment(self) -> dict[str, str]:
        return self.base_environment()

    def run(
        self,
        argv: tuple[str, ...],
        *,
        systemd: bool = False,
        timeout_seconds: float = COMMAND_TIMEOUT_SECONDS,
    ) -> CommandResult:
        return self._executor.run(
            argv,
            environment=self.systemd_environment() if systemd else self.base_environment(),
            timeout_seconds=timeout_seconds,
        )

    def run_compose(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float = COMMAND_TIMEOUT_SECONDS,
    ) -> CommandResult:
        return self._executor.run(
            argv,
            environment=self.compose_environment(),
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    def require_success(result: CommandResult, code: str) -> bytes:
        if result.returncode != 0:
            raise FaultControllerHostError(code)
        return result.stdout

    def compose_prefix(self) -> tuple[str, ...]:
        return self.docker_runtime_prefix() + (
            "compose",
            "--env-file",
            EMPTY_ENV_FILE,
            "--file",
            str(self.recovery_snapshot.path),
            "--project-name",
            COMPOSE_PROJECT,
        )

    @staticmethod
    def docker_runtime_prefix() -> tuple[str, ...]:
        """ambient contextを使わないlocal Docker daemon argvを返す。"""

        return (
            DOCKER_BIN,
            "--config",
            str(DOCKER_CONFIG_DIRECTORY),
            "--host",
            DOCKER_ENDPOINT,
        )

    def fault_control_command(
        self,
        operation: Literal["probe", "disable", "enable"],
    ) -> tuple[str, ...]:
        if operation not in {"probe", "disable", "enable"}:
            raise FaultControllerHostError("fault_host_action_invalid")
        return self.compose_prefix() + (
            "run",
            "--rm",
            "--no-deps",
            "--pull",
            "never",
            "--no-tty",
            FAULT_CONTROL_SERVICE,
            operation,
        )

    def api_state_command(self) -> tuple[str, ...]:
        return self.compose_prefix() + (
            "ps",
            "--all",
            "--no-trunc",
            "--format",
            "json",
            API_SECONDARY_SERVICE,
        )

    def recovery_command(self, target: FaultTarget) -> tuple[str, ...]:
        if target == "redis":
            docker_argv = self.fault_control_command("enable")
        elif target == "api-secondary":
            docker_argv = self.compose_prefix() + (
                "start",
                "--wait",
                "--wait-timeout",
                "105",
                API_SECONDARY_SERVICE,
            )
        else:
            raise FaultControllerHostError("fault_host_target_invalid")
        environment = self.compose_environment()
        return (
            ENV_BIN,
            "--ignore-environment",
            *(f"{key}={value}" for key, value in sorted(environment.items())),
            *docker_argv,
        )

    def recovery_command_sha256(self, target: FaultTarget) -> str:
        return command_argv_sha256(self.recovery_command(target))

    def disable_command(self, target: FaultTarget) -> tuple[str, ...]:
        """controller identityと実操作が共有する固定disable argv。"""

        if target == "redis":
            return self.fault_control_command("disable")
        if target == "api-secondary":
            return self.compose_prefix() + (
                "stop",
                "--timeout",
                "20",
                API_SECONDARY_SERVICE,
            )
        raise FaultControllerHostError("fault_host_target_invalid")

    def observe_target(self, target: FaultTarget) -> FaultTargetObservation:
        if target == "redis":
            return self._run_fault_control("probe")[0]
        if target == "api-secondary":
            return self._observe_api_state().observation
        raise FaultControllerHostError("fault_host_target_invalid")

    def apply_action(
        self,
        target: FaultTarget,
        action: FaultAction,
        expected_before: FaultTargetObservation,
    ) -> FaultMutationResult:
        if action not in {"disable", "enable"}:
            raise FaultControllerHostError("fault_host_action_invalid")
        desired = action == "enable"
        if expected_before.target != target:
            raise FaultControllerHostError("fault_host_action_invalid")
        if target == "redis":
            try:
                before, _ = self._run_fault_control("probe")
            except FaultControllerHostError as error:
                raise FaultMutationPreconditionError(error.code) from error
            if before != expected_before:
                raise FaultMutationPreconditionError("fault_host_redis_state_mismatch")
            if before.enabled is desired:
                return FaultMutationResult(before=before, after=before, reconciled=True)
            try:
                after, reconciled = self._run_fault_control(action)
            except FaultControllerHostError as error:
                raise FaultMutationOutcomeUncertainError(error.code) from error
            if after.enabled is not desired:
                raise FaultMutationOutcomeUncertainError("fault_host_redis_state_mismatch")
            return FaultMutationResult(before=before, after=after, reconciled=reconciled)
        if target != "api-secondary":
            raise FaultControllerHostError("fault_host_target_invalid")
        try:
            before_state = self._observe_api_state()
        except FaultControllerHostError as error:
            raise FaultMutationPreconditionError(error.code) from error
        if before_state.observation != expected_before:
            raise FaultMutationPreconditionError("fault_host_api_state_mismatch")
        if before_state.observation.enabled is desired:
            return FaultMutationResult(
                before=before_state.observation,
                after=before_state.observation,
                reconciled=True,
            )
        argv = (
            self.compose_prefix()
            + ("start", "--wait", "--wait-timeout", "105", API_SECONDARY_SERVICE)
            if desired
            else self.disable_command("api-secondary")
        )
        try:
            self.require_success(
                self.run_compose(argv, timeout_seconds=RECOVERY_RUNTIME_SECONDS),
                "fault_host_api_action_failed",
            )
            after_state = self._observe_api_state()
        except FaultTargetRecoveryPendingError:
            # request送信後のstartingはidentity付きでcontrollerのbounded pollへ渡す。
            raise
        except FaultControllerHostError as error:
            raise FaultMutationOutcomeUncertainError(error.code) from error
        if (
            after_state.record.container_id != before_state.record.container_id
            or after_state.observation.enabled is not desired
        ):
            raise FaultMutationOutcomeUncertainError("fault_host_api_state_mismatch")
        return FaultMutationResult(
            before=before_state.observation,
            after=after_state.observation,
            reconciled=False,
        )

    def _run_fault_control(
        self,
        operation: Literal["probe", "disable", "enable"],
    ) -> tuple[FaultTargetObservation, bool]:
        body = self.require_success(
            self.run_compose(
                self.fault_control_command(operation),
                timeout_seconds=FAULT_CONTROL_RUNTIME_SECONDS,
            ),
            "fault_host_redis_command_failed",
        )
        if len(body) > 4096 or not body.endswith(b"\n") or body.count(b"\n") != 1:
            raise FaultControllerHostError("fault_host_redis_output_invalid")
        _require_unambiguous_json(body[:-1], "fault_host_redis_output_invalid")
        try:
            result = FaultControlCliResult.model_validate_json(body[:-1], strict=True)
        except ValidationError as error:
            raise FaultControllerHostError("fault_host_redis_output_invalid") from error
        if result.operation != operation or (
            operation != "probe" and result.enabled is not (operation == "enable")
        ):
            raise FaultControllerHostError("fault_host_redis_output_invalid")
        identity = sha256(
            canonical_json_bytes(
                {
                    "configuration_sha256": self.configuration.toxiproxy_configuration_sha256,
                    "control_origin": TOXIPROXY_CONTROL_ORIGIN,
                    "listen": self.configuration.toxiproxy_proxy.listen,
                    "proxy_name": self.configuration.toxiproxy_proxy.name,
                    "redis_hostname": self.configuration.redis_hostname,
                    "service": FAULT_CONTROL_SERVICE,
                    "toxics": (),
                    "upstream": self.configuration.toxiproxy_proxy.upstream,
                }
            )
        ).hexdigest()
        return (
            FaultTargetObservation(
                target="redis", enabled=result.enabled, identity_sha256=identity
            ),
            result.reconciled,
        )

    def _observe_api_state(self) -> _ApiState:
        body = self.require_success(
            self.run_compose(self.api_state_command()), "fault_host_api_state_unavailable"
        )
        if len(body) > 16_384 or not body.endswith(b"\n") or body.count(b"\n") != 1:
            raise FaultControllerHostError("fault_host_api_state_invalid")
        _require_unambiguous_json(body[:-1], "fault_host_api_state_invalid")
        try:
            record = _ComposePsRecord.model_validate_json(body[:-1], strict=True)
        except ValidationError as error:
            raise FaultControllerHostError("fault_host_api_state_invalid") from error
        expected_name = f"{COMPOSE_PROJECT}-{API_SECONDARY_SERVICE}-1"
        expected_networks = frozenset(
            {
                f"{COMPOSE_PROJECT}_default",
                f"{COMPOSE_PROJECT}_metrics",
                f"{COMPOSE_PROJECT}_fault-data",
            }
        )
        networks = () if record.networks is None else tuple(record.networks.split(","))
        publisher_is_internal_only = not record.publishers or (
            len(record.publishers) == 1
            and record.publishers[0].url == ""
            and record.publishers[0].target_port == API_SECONDARY_CONTAINER_PORT
            and record.publishers[0].published_port == 0
            and record.publishers[0].protocol == "tcp"
        )
        if (
            record.project != COMPOSE_PROJECT
            or record.service != API_SECONDARY_SERVICE
            or record.name != expected_name
            or record.image != self.configuration.api_image
            or not publisher_is_internal_only
            or record.ports not in {None, "", f"{API_SECONDARY_CONTAINER_PORT}/tcp"}
            or len(networks) != len(expected_networks)
            or frozenset(networks) != expected_networks
        ):
            raise FaultControllerHostError("fault_host_api_identity_mismatch")
        identity = sha256(
            canonical_json_bytes(
                {
                    "container_id": record.container_id,
                    "image": record.image,
                    "name": record.name,
                    "networks": tuple(sorted(networks)),
                    "project": record.project,
                    "service": record.service,
                }
            )
        ).hexdigest()
        if record.state == "running" and record.health == "healthy" and record.exit_code == 0:
            enabled = True
        elif record.state == "running" and record.health == "starting" and record.exit_code == 0:
            raise FaultTargetRecoveryPendingError(
                "fault_host_api_recovery_pending",
                target="api-secondary",
                identity_sha256=identity,
            )
        elif record.state == "exited" and record.health == "" and record.exit_code == 0:
            enabled = False
        else:
            raise FaultControllerHostError("fault_host_api_state_invalid")
        return _ApiState(
            record=record,
            observation=FaultTargetObservation(
                target="api-secondary", enabled=enabled, identity_sha256=identity
            ),
        )

    def verify_execution_host(self) -> None:
        hostname = self.require_success(
            self.run((HOSTNAME_BIN, "-f")), "fault_host_execution_host_unavailable"
        )
        if hostname != f"{self.configuration.execution_host}\n".encode("ascii"):
            raise FaultControllerHostError("fault_host_execution_host_mismatch")

    def read_docker_compose_version(self) -> str:
        """ambient Docker設定を遮断し、実plugin versionをcanonical化する。"""

        require_root_owned_docker_config_directory()
        body = self.require_success(
            self.run(
                (
                    DOCKER_BIN,
                    "--config",
                    str(DOCKER_CONFIG_DIRECTORY),
                    "--host",
                    DOCKER_ENDPOINT,
                    "compose",
                    "version",
                    "--short",
                )
            ),
            "fault_host_compose_unavailable",
        )
        match = _COMPOSE_VERSION.fullmatch(body)
        if match is None:
            raise FaultControllerHostError("fault_host_compose_version_invalid")
        version = tuple(int(part) for part in match.groups())
        if version < MINIMUM_COMPOSE_VERSION:
            raise FaultControllerHostError("fault_host_compose_version_invalid")
        return ".".join(str(part) for part in version)

    def read_docker_engine_version(self) -> str:
        """isolated gateway modeを提供する実Docker server versionを読む。"""

        require_root_owned_docker_config_directory()
        body = self.require_success(
            self.run(
                self.docker_runtime_prefix() + ("version", "--format", DOCKER_ENGINE_VERSION_FORMAT)
            ),
            "fault_host_engine_unavailable",
        )
        return parse_docker_engine_version(body)

    def read_fault_control_network(self) -> FaultControlNetworkObservation:
        """障害制御networkの実隔離条件を読み戻し、identity化する。"""

        require_root_owned_docker_config_directory()
        body = self.require_success(
            self.run(
                self.docker_runtime_prefix()
                + (
                    "network",
                    "inspect",
                    "--format",
                    FAULT_CONTROL_NETWORK_FORMAT,
                    FAULT_CONTROL_NETWORK,
                )
            ),
            "fault_host_control_network_unavailable",
        )
        return parse_fault_control_network(body)

    def read_toxiproxy_container(self) -> ToxiproxyContainerObservation:
        """常駐Toxiproxy daemonのselected inspectをidentity化する。"""

        require_root_owned_docker_config_directory()
        body = self.require_success(
            self.run(
                self.docker_runtime_prefix()
                + (
                    "inspect",
                    "--type",
                    "container",
                    "--format",
                    TOXIPROXY_CONTAINER_FORMAT,
                    TOXIPROXY_CONTAINER_NAME,
                )
            ),
            "fault_host_toxiproxy_container_unavailable",
        )
        return parse_toxiproxy_container(
            body,
            expected_image=self.configuration.toxiproxy_image,
            expected_user=f"{os.geteuid()}:{os.getegid()}",
            expected_config_source=str(self.configuration.toxiproxy_configuration_file),
        )

    def verify_docker_context(self) -> None:
        require_root_owned_docker_config_directory()
        endpoint = self.require_success(
            self.run(
                (
                    DOCKER_BIN,
                    "--config",
                    str(DOCKER_CONFIG_DIRECTORY),
                    "--context",
                    self.configuration.docker_context,
                    "context",
                    "inspect",
                    self.configuration.docker_context,
                    "--format",
                    '{{ (index .Endpoints "docker").Host }}',
                )
            ),
            "fault_host_docker_context_unavailable",
        )
        if endpoint != f"{DOCKER_ENDPOINT}\n".encode("ascii"):
            raise FaultControllerHostError("fault_host_docker_endpoint_mismatch")
