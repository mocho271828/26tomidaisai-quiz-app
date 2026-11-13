from __future__ import annotations

import json

import pytest

from quiz_app.load_test.fault_docker_network import (
    FAULT_CONTROL_GATEWAY_MODE_OPTION,
    TOXIPROXY_CONTAINER_NAME,
    parse_docker_engine_version,
    parse_fault_control_network,
    parse_fault_control_network_identity,
)
from quiz_app.load_test.fault_host_config import FaultControllerHostError

NETWORK_ID = "a" * 64


def _network_output(**overrides: object) -> bytes:
    value: dict[str, object] = {
        "Attachable": False,
        "Containers": {
            "b" * 64: {
                "Name": TOXIPROXY_CONTAINER_NAME,
                "EndpointID": "c" * 64,
                "MacAddress": "02:42:ac:1e:fb:02",
                "IPv4Address": "172.30.251.2/29",
                "IPv6Address": "",
            }
        },
        "Driver": "bridge",
        "EnableIPv6": False,
        "Id": NETWORK_ID,
        "Ingress": False,
        "Internal": True,
        "IPAM": {
            "Driver": "default",
            "Options": {},
            "Config": [{"Subnet": "172.30.251.0/29"}],
        },
        "Name": "gs-app-tomidaisai-aws-demo_fault-control",
        "Options": {FAULT_CONTROL_GATEWAY_MODE_OPTION: "isolated"},
        "Scope": "local",
    }
    value.update(overrides)
    return json.dumps(value, separators=(",", ":")).encode("ascii") + b"\n"


def _assert_code(expected: str, operation: object) -> None:
    with pytest.raises(FaultControllerHostError) as captured:
        operation()  # type: ignore[operator]
    assert captured.value.code == expected


def test_engine_version_is_minimum_bounded_and_canonical() -> None:
    assert parse_docker_engine_version(b"28.0.0\n") == "28.0.0"
    assert parse_docker_engine_version(b"029.001.002\n") == "29.1.2"


@pytest.mark.parametrize(
    "body",
    [
        b"27.9.9\n",
        b"28.0\n",
        b"v28.0.0\n",
        b"28.0.0+vendor\n",
        b"28.0.0\nextra\n",
    ],
)
def test_engine_version_rejects_old_or_ambiguous_output(body: bytes) -> None:
    _assert_code(
        "fault_host_engine_version_invalid",
        lambda: parse_docker_engine_version(body),
    )


def test_network_identity_is_stable_for_exact_isolated_network() -> None:
    first = parse_fault_control_network_identity(_network_output())
    second = parse_fault_control_network_identity(
        _network_output(
            Options={FAULT_CONTROL_GATEWAY_MODE_OPTION: "isolated"},
            Name="gs-app-tomidaisai-aws-demo_fault-control",
        )
    )

    assert len(first) == 64
    assert first == second
    assert parse_fault_control_network(_network_output()).container_id == "b" * 64


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("Attachable", True),
        ("Driver", "overlay"),
        ("EnableIPv6", True),
        ("Id", "a" * 63),
        ("Ingress", True),
        ("Internal", False),
        ("Name", "attacker-network"),
        ("Options", {}),
        ("Options", {FAULT_CONTROL_GATEWAY_MODE_OPTION: "nat"}),
        (
            "Options",
            {
                FAULT_CONTROL_GATEWAY_MODE_OPTION: "isolated",
                "com.docker.network.bridge.enable_icc": "true",
            },
        ),
        ("Scope", "swarm"),
        ("Containers", {}),
        (
            "Containers",
            {
                "b" * 64: {
                    "Name": TOXIPROXY_CONTAINER_NAME,
                    "EndpointID": "c" * 64,
                    "MacAddress": "02:42:ac:1e:fb:02",
                    "IPv4Address": "172.30.251.2/29",
                    "IPv6Address": "",
                },
                "d" * 64: {
                    "Name": TOXIPROXY_CONTAINER_NAME,
                    "EndpointID": "e" * 64,
                    "MacAddress": "02:42:ac:1e:fb:03",
                    "IPv4Address": "172.30.251.2/29",
                    "IPv6Address": "",
                },
            },
        ),
        (
            "Containers",
            {
                "b" * 64: {
                    "Name": TOXIPROXY_CONTAINER_NAME,
                    "EndpointID": "c" * 64,
                    "MacAddress": "02:42:ac:1e:fb:02",
                    "IPv4Address": "172.30.251.3/29",
                    "IPv6Address": "",
                }
            },
        ),
    ],
)
def test_network_identity_rejects_control_plane_drift(
    field: str,
    value: object,
) -> None:
    _assert_code(
        "fault_host_control_network_invalid",
        lambda: parse_fault_control_network_identity(_network_output(**{field: value})),
    )


@pytest.mark.parametrize(
    "ipam",
    [
        {"Driver": "other", "Options": {}, "Config": [{"Subnet": "172.30.251.0/29"}]},
        {
            "Driver": "default",
            "Options": {"opaque": "value"},
            "Config": [{"Subnet": "172.30.251.0/29"}],
        },
        {"Driver": "default", "Options": {}, "Config": []},
        {"Driver": "default", "Options": {}, "Config": [{"Subnet": "172.30.252.0/29"}]},
        {
            "Driver": "default",
            "Options": {},
            "Config": [{"Subnet": "172.30.251.0/29", "Gateway": "172.30.251.1"}],
        },
        {
            "Driver": "default",
            "Options": {},
            "Config": [{"Subnet": "172.30.251.0/29"}, {"Subnet": "172.30.252.0/29"}],
        },
    ],
)
def test_network_identity_rejects_ipam_drift(ipam: object) -> None:
    _assert_code(
        "fault_host_control_network_invalid",
        lambda: parse_fault_control_network_identity(_network_output(IPAM=ipam)),
    )


def test_network_identity_rejects_extra_and_duplicate_fields() -> None:
    _assert_code(
        "fault_host_control_network_invalid",
        lambda: parse_fault_control_network_identity(_network_output(Unexpected="value")),
    )
    duplicate = _network_output().replace(b'"Internal":true', b'"Internal":false,"Internal":true')
    _assert_code(
        "fault_host_control_network_invalid",
        lambda: parse_fault_control_network_identity(duplicate),
    )


@pytest.mark.parametrize(
    "body",
    [
        b"{}",
        b"{}\nextra\n",
        b"x" * 4097 + b"\n",
    ],
)
def test_network_identity_rejects_noncanonical_command_output(body: bytes) -> None:
    _assert_code(
        "fault_host_control_network_invalid",
        lambda: parse_fault_control_network_identity(body),
    )
