"""障害制御networkとDocker Engineの実行時identity検証。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Annotated, Final, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    ValidationError,
    model_validator,
)

from quiz_app.load_test.controller_contracts import canonical_json_bytes
from quiz_app.load_test.fault_host_config import FaultControllerHostError

DOCKER_ENGINE_VERSION_FORMAT: Final = "{{.Server.Version}}"
FAULT_CONTROL_NETWORK: Final = "gs-app-tomidaisai-aws-demo_fault-control"
TOXIPROXY_CONTAINER_NAME: Final = "gs-app-tomidaisai-aws-demo-toxiproxy-1"
FAULT_CONTROL_NETWORK_SUBNET: Final = "172.30.251.0/29"
FAULT_CONTROL_GATEWAY_MODE_OPTION: Final = "com.docker.network.bridge.gateway_mode_ipv4"
FAULT_CONTROL_GATEWAY_MODE: Final = "isolated"
MINIMUM_DOCKER_ENGINE_VERSION: Final = (28, 0, 0)
MAXIMUM_NETWORK_INSPECT_BYTES: Final = 4096

_DOCKER_ENGINE_VERSION = re.compile(rb"^([0-9]{2,3})\.([0-9]{1,3})\.([0-9]{1,3})\n$")

# 短命clientが不在のpreflight時は常駐Toxiproxyだけが参加する。隔離fieldに加えて
# その唯一のmembershipもDocker側で選び、厳密なJSON契約とidentityへ束縛する。
FAULT_CONTROL_NETWORK_FORMAT: Final = (
    '{"Attachable":{{json .Attachable}},'
    '"Containers":{{json .Containers}},'
    '"Driver":{{json .Driver}},'
    '"EnableIPv6":{{json .EnableIPv6}},'
    '"Id":{{json .Id}},'
    '"Ingress":{{json .Ingress}},'
    '"Internal":{{json .Internal}},'
    '"IPAM":{{json .IPAM}},'
    '"Name":{{json .Name}},'
    '"Options":{{json .Options}},'
    '"Scope":{{json .Scope}}}'
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


class _NetworkIpamConfig(_StrictDockerModel):
    subnet: Literal[FAULT_CONTROL_NETWORK_SUBNET] = Field(alias="Subnet")
    ip_range: None = Field(default=None, alias="IPRange")
    gateway: None = Field(default=None, alias="Gateway")
    auxiliary_addresses: None = Field(default=None, alias="AuxiliaryAddresses")


class _NetworkIpam(_StrictDockerModel):
    driver: Literal["default"] = Field(alias="Driver")
    options: dict[StrictStr, StrictStr] | None = Field(alias="Options")
    config: tuple[_NetworkIpamConfig] = Field(alias="Config")

    @model_validator(mode="after")
    def validate_empty_options(self) -> Self:
        if self.options:
            raise ValueError("fault-control IPAM optionは空である必要があります")
        return self


class _NetworkContainer(_StrictDockerModel):
    name: Literal[TOXIPROXY_CONTAINER_NAME] = Field(alias="Name")
    endpoint_id: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")] = Field(alias="EndpointID")
    mac_address: Annotated[
        StrictStr,
        Field(pattern=r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$"),
    ] = Field(alias="MacAddress")
    ipv4_address: Literal["172.30.251.2/29"] = Field(alias="IPv4Address")
    ipv6_address: Literal[""] = Field(alias="IPv6Address")


@dataclass(frozen=True, slots=True)
class FaultControlNetworkObservation:
    container_id: str
    identity_sha256: str
    network_id: str


class _FaultControlNetworkInspect(_StrictDockerModel):
    attachable: Literal[False] = Field(alias="Attachable")
    containers: dict[
        Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")],
        _NetworkContainer,
    ] = Field(alias="Containers")
    driver: Literal["bridge"] = Field(alias="Driver")
    enable_ipv6: Literal[False] = Field(alias="EnableIPv6")
    network_id: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")] = Field(alias="Id")
    ingress: Literal[False] = Field(alias="Ingress")
    internal: Literal[True] = Field(alias="Internal")
    ipam: _NetworkIpam = Field(alias="IPAM")
    name: Literal[FAULT_CONTROL_NETWORK] = Field(alias="Name")
    options: dict[StrictStr, StrictStr] = Field(alias="Options")
    scope: Literal["local"] = Field(alias="Scope")

    @model_validator(mode="after")
    def validate_exact_driver_options(self) -> Self:
        if self.options != {FAULT_CONTROL_GATEWAY_MODE_OPTION: FAULT_CONTROL_GATEWAY_MODE}:
            raise ValueError("fault-control bridge optionが期待値と一致しません")
        return self

    def observation(self) -> FaultControlNetworkObservation:
        """隔離構成と唯一の常駐Toxiproxyを正規化する。"""

        if len(self.containers) != 1:
            raise ValueError("fault-control networkの常駐containerは1つだけ必要です")
        container_id, container = next(iter(self.containers.items()))
        identity_sha256 = sha256(
            canonical_json_bytes(
                {
                    "attachable": self.attachable,
                    "container": {
                        "endpoint_id": container.endpoint_id,
                        "id": container_id,
                        "ipv4_address": container.ipv4_address,
                        "ipv6_address": container.ipv6_address,
                        "mac_address": container.mac_address,
                        "name": container.name,
                    },
                    "driver": self.driver,
                    "enable_ipv6": self.enable_ipv6,
                    "id": self.network_id,
                    "ingress": self.ingress,
                    "internal": self.internal,
                    "ipam": {
                        "config": ({"subnet": self.ipam.config[0].subnet},),
                        "driver": self.ipam.driver,
                        "options": {},
                    },
                    "name": self.name,
                    "options": {FAULT_CONTROL_GATEWAY_MODE_OPTION: FAULT_CONTROL_GATEWAY_MODE},
                    "scope": self.scope,
                }
            )
        ).hexdigest()
        return FaultControlNetworkObservation(
            container_id=container_id,
            identity_sha256=identity_sha256,
            network_id=self.network_id,
        )


def parse_docker_engine_version(body: bytes) -> str:
    """Docker server versionを厳密なsemver三要素へ正規化する。"""

    match = _DOCKER_ENGINE_VERSION.fullmatch(body)
    if match is None:
        raise FaultControllerHostError("fault_host_engine_version_invalid")
    version = tuple(int(part) for part in match.groups())
    if version < MINIMUM_DOCKER_ENGINE_VERSION:
        raise FaultControllerHostError("fault_host_engine_version_invalid")
    return ".".join(str(part) for part in version)


def parse_fault_control_network(body: bytes) -> FaultControlNetworkObservation:
    """Docker inspect出力を隔離条件へ照合し、canonical digestを返す。"""

    if (
        len(body) > MAXIMUM_NETWORK_INSPECT_BYTES
        or not body.endswith(b"\n")
        or body.count(b"\n") != 1
    ):
        raise FaultControllerHostError("fault_host_control_network_invalid")
    try:
        json.loads(
            body[:-1],
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
        network = _FaultControlNetworkInspect.model_validate_json(body[:-1], strict=True)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKeyError,
        ValueError,
        ValidationError,
    ) as error:
        raise FaultControllerHostError("fault_host_control_network_invalid") from error
    try:
        return network.observation()
    except ValueError as error:
        raise FaultControllerHostError("fault_host_control_network_invalid") from error


def parse_fault_control_network_identity(body: bytes) -> str:
    """検証済みnetwork observationのdigestだけを返す。"""

    return parse_fault_control_network(body).identity_sha256


__all__ = (
    "DOCKER_ENGINE_VERSION_FORMAT",
    "FAULT_CONTROL_NETWORK",
    "FAULT_CONTROL_NETWORK_FORMAT",
    "FaultControlNetworkObservation",
    "TOXIPROXY_CONTAINER_NAME",
    "parse_docker_engine_version",
    "parse_fault_control_network",
    "parse_fault_control_network_identity",
)
