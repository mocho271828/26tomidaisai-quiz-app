"""実Toxiproxy daemon containerの固定runtime identity検証。"""

from __future__ import annotations

import json
from hashlib import sha256
from ipaddress import IPv4Address
from typing import Annotated, Final, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)

from quiz_app.load_test.controller_contracts import canonical_json_bytes
from quiz_app.load_test.fault_docker_network import TOXIPROXY_CONTAINER_NAME
from quiz_app.load_test.fault_host_config import FaultControllerHostError

TOXIPROXY_CONTAINER_FORMAT: Final = (
    '{"CapDrop":{{json .HostConfig.CapDrop}},'
    '"CapAdd":{{json .HostConfig.CapAdd}},'
    '"Cmd":{{json .Config.Cmd}},'
    '"Entrypoint":{{json .Config.Entrypoint}},'
    '"ExposedPorts":{{json .Config.ExposedPorts}},'
    '"Health":{{json .State.Health.Status}},'
    '"Id":{{json .Id}},'
    '"Image":{{json .Config.Image}},'
    '"Name":{{json .Name}},'
    '"Networks":{{json .NetworkSettings.Networks}},'
    '"Mounts":{{json .Mounts}},'
    '"PortBindings":{{json .HostConfig.PortBindings}},'
    '"Privileged":{{json .HostConfig.Privileged}},'
    '"Project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"ReadonlyRootfs":{{json .HostConfig.ReadonlyRootfs}},'
    '"RestartPolicy":{{json .HostConfig.RestartPolicy}},'
    '"Running":{{json .State.Running}},'
    '"SecurityOpt":{{json .HostConfig.SecurityOpt}},'
    '"Service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"User":{{json .Config.User}}}'
)
MAXIMUM_TOXIPROXY_INSPECT_BYTES: Final = 16 * 1024
COMPOSE_PROJECT: Final = "gs-app-tomidaisai-aws-demo"
TOXIPROXY_SERVICE: Final = "toxiproxy"
TOXIPROXY_COMMAND: Final = (
    "-host",
    "172.30.251.2",
    "-port",
    "8474",
    "-config",
    "/run/load-test/toxiproxy.json",
)
EXPECTED_NETWORKS: Final = frozenset(
    {
        f"{COMPOSE_PROJECT}_default",
        f"{COMPOSE_PROJECT}_fault-data",
        f"{COMPOSE_PROJECT}_fault-control",
    }
)


class _DuplicateJsonKeyError(ValueError):
    """重複keyを含む曖昧なDocker JSONを示す内部signal。"""


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError
        result[key] = value
    return result


def _reject_json_constant(_: str) -> None:
    raise ValueError


class _StrictDockerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _EndpointIpamConfig(_StrictDockerModel):
    ipv4_address: StrictStr | None = Field(default=None, alias="IPv4Address")
    ipv6_address: StrictStr | None = Field(default=None, alias="IPv6Address")
    link_local_ips: tuple[StrictStr, ...] | None = Field(default=None, alias="LinkLocalIPs")


class _ContainerNetworkEndpoint(_StrictDockerModel):
    ipam_config: _EndpointIpamConfig | None = Field(alias="IPAMConfig")
    links: tuple[StrictStr, ...] | None = Field(alias="Links")
    aliases: tuple[StrictStr, ...] | None = Field(alias="Aliases")
    mac_address: Annotated[
        StrictStr,
        Field(pattern=r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$"),
    ] = Field(alias="MacAddress")
    driver_options: dict[StrictStr, StrictStr] | None = Field(alias="DriverOpts")
    gateway_priority: StrictInt = Field(alias="GwPriority")
    network_id: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")] = Field(alias="NetworkID")
    endpoint_id: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")] = Field(alias="EndpointID")
    gateway: StrictStr = Field(alias="Gateway")
    ip_address: StrictStr = Field(alias="IPAddress")
    ip_prefix_length: StrictInt = Field(alias="IPPrefixLen", ge=1, le=32)
    ipv6_gateway: Literal[""] = Field(alias="IPv6Gateway")
    global_ipv6_address: Literal[""] = Field(alias="GlobalIPv6Address")
    global_ipv6_prefix_length: Literal[0] = Field(alias="GlobalIPv6PrefixLen")
    dns_names: tuple[StrictStr, ...] | None = Field(alias="DNSNames")


class _RestartPolicy(_StrictDockerModel):
    name: Literal["no"] = Field(alias="Name")
    maximum_retry_count: Literal[0] = Field(alias="MaximumRetryCount")


class _PortBinding(_StrictDockerModel):
    host_ip: StrictStr = Field(alias="HostIp")
    host_port: StrictStr = Field(alias="HostPort")


class _Mount(_StrictDockerModel):
    mount_type: Literal["bind"] = Field(alias="Type")
    source: StrictStr = Field(alias="Source")
    destination: Literal["/run/load-test/toxiproxy.json"] = Field(alias="Destination")
    mode: Literal["ro"] = Field(alias="Mode")
    read_write: Literal[False] = Field(alias="RW")
    propagation: Literal["rprivate"] = Field(alias="Propagation")


class _ToxiproxyContainerInspect(_StrictDockerModel):
    cap_add: tuple[StrictStr, ...] | None = Field(alias="CapAdd")
    cap_drop: tuple[Literal["ALL"]] = Field(alias="CapDrop")
    command: tuple[StrictStr, ...] = Field(alias="Cmd")
    entrypoint: tuple[StrictStr, ...] = Field(alias="Entrypoint")
    exposed_ports: dict[StrictStr, dict[StrictStr, StrictStr]] | None = Field(alias="ExposedPorts")
    health: Literal["healthy"] = Field(alias="Health")
    container_id: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")] = Field(alias="Id")
    image: StrictStr = Field(alias="Image")
    name: Literal[f"/{TOXIPROXY_CONTAINER_NAME}"] = Field(alias="Name")
    networks: dict[StrictStr, _ContainerNetworkEndpoint] = Field(alias="Networks")
    mounts: tuple[_Mount] = Field(alias="Mounts")
    port_bindings: dict[StrictStr, tuple[_PortBinding, ...] | None] | None = Field(
        alias="PortBindings"
    )
    project: Literal[COMPOSE_PROJECT] = Field(alias="Project")
    privileged: Literal[False] = Field(alias="Privileged")
    read_only_root_filesystem: Literal[True] = Field(alias="ReadonlyRootfs")
    restart_policy: _RestartPolicy = Field(alias="RestartPolicy")
    running: Literal[True] = Field(alias="Running")
    security_options: tuple[Literal["no-new-privileges:true"]] = Field(alias="SecurityOpt")
    service: Literal[TOXIPROXY_SERVICE] = Field(alias="Service")
    user: StrictStr = Field(alias="User")

    @model_validator(mode="after")
    def validate_runtime_contract(self) -> Self:
        if self.command != TOXIPROXY_COMMAND or self.entrypoint != ("/toxiproxy",):
            raise ValueError("Toxiproxy commandがCompose定義と一致しません")
        if self.exposed_ports != {"8474/tcp": {}} or self.port_bindings:
            raise ValueError("Toxiproxy port公開状態が期待値と一致しません")
        if self.cap_add:
            raise ValueError("Toxiproxy capability追加は許可されません")
        if set(self.networks) != EXPECTED_NETWORKS:
            raise ValueError("Toxiproxy network集合が期待値と一致しません")
        fixed_addresses = {
            f"{COMPOSE_PROJECT}_fault-data": ("172.30.250.2", 29),
            f"{COMPOSE_PROJECT}_fault-control": ("172.30.251.2", 29),
        }
        for network_name, endpoint in self.networks.items():
            try:
                IPv4Address(endpoint.ip_address)
                if network_name == f"{COMPOSE_PROJECT}_default":
                    IPv4Address(endpoint.gateway)
            except ValueError as error:
                raise ValueError("Toxiproxy IPv4設定が不正です") from error
            # Dockerはisolated modeに限らず、internal bridgeのendpointにgatewayを設定しない。
            if network_name in fixed_addresses and endpoint.gateway != "":
                raise ValueError("internal networkにgatewayは許可されません")
            if (
                network_name in fixed_addresses
                and (
                    endpoint.ip_address,
                    endpoint.ip_prefix_length,
                )
                != fixed_addresses[network_name]
            ):
                raise ValueError("Toxiproxy固定IPv4が期待値と一致しません")
        return self

    def identity_sha256(self) -> str:
        """secretを含まないselected inspect fieldだけをdigest化する。"""

        selected = self.model_dump(mode="json")
        selected["port_bindings"] = {}
        selected["cap_add"] = []
        selected["mounts"] = [
            {
                "destination": mount.destination,
                "mode": mount.mode,
                "propagation": mount.propagation,
                "read_write": mount.read_write,
                "type": mount.mount_type,
            }
            for mount in self.mounts
        ]
        return sha256(canonical_json_bytes(selected)).hexdigest()


class ToxiproxyContainerObservation(_StrictDockerModel):
    container_id: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    control_network_id: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    identity_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]


def parse_toxiproxy_container(
    body: bytes,
    *,
    expected_image: str,
    expected_user: str,
    expected_config_source: str,
) -> ToxiproxyContainerObservation:
    """selected Docker inspectを検証し、Toxiproxy runtime identityへ正規化する。"""

    if (
        len(body) > MAXIMUM_TOXIPROXY_INSPECT_BYTES
        or not body.endswith(b"\n")
        or body.count(b"\n") != 1
    ):
        raise FaultControllerHostError("fault_host_toxiproxy_container_invalid")
    try:
        json.loads(
            body[:-1],
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
        container = _ToxiproxyContainerInspect.model_validate_json(body[:-1], strict=True)
        if (
            container.image != expected_image
            or container.user != expected_user
            or len(container.mounts) != 1
            or container.mounts[0].source != expected_config_source
        ):
            raise ValueError("Toxiproxy release/runtime identityが一致しません")
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKeyError,
        ValueError,
        ValidationError,
    ) as error:
        raise FaultControllerHostError("fault_host_toxiproxy_container_invalid") from error
    return ToxiproxyContainerObservation(
        container_id=container.container_id,
        control_network_id=container.networks[f"{COMPOSE_PROJECT}_fault-control"].network_id,
        identity_sha256=container.identity_sha256(),
    )


__all__ = (
    "TOXIPROXY_CONTAINER_FORMAT",
    "TOXIPROXY_CONTAINER_NAME",
    "ToxiproxyContainerObservation",
    "parse_toxiproxy_container",
)
