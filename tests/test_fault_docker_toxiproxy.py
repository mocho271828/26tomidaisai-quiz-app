from __future__ import annotations

import json
import os

import pytest

from quiz_app.load_test.fault_docker_network import TOXIPROXY_CONTAINER_NAME
from quiz_app.load_test.fault_docker_toxiproxy import (
    COMPOSE_PROJECT,
    TOXIPROXY_COMMAND,
    parse_toxiproxy_container,
)
from quiz_app.load_test.fault_host_config import FaultControllerHostError

CONTAINER_ID = "b" * 64
TOXIPROXY_IMAGE = f"registry/toxiproxy@sha256:{'c' * 64}"
EXPECTED_USER = f"{os.geteuid()}:{os.getegid()}"
CONFIG_SOURCE = "/external/toxiproxy.json"


def _network(
    ip_address: str,
    prefix_length: int,
    *,
    gateway: str = "172.30.0.1",
) -> dict[str, object]:
    return {
        "IPAMConfig": None,
        "Links": None,
        "Aliases": [TOXIPROXY_CONTAINER_NAME, "toxiproxy"],
        "MacAddress": "02:42:ac:1e:00:02",
        "DriverOpts": None,
        "GwPriority": 0,
        "NetworkID": "d" * 64,
        "EndpointID": "e" * 64,
        "Gateway": gateway,
        "IPAddress": ip_address,
        "IPPrefixLen": prefix_length,
        "IPv6Gateway": "",
        "GlobalIPv6Address": "",
        "GlobalIPv6PrefixLen": 0,
        "DNSNames": [TOXIPROXY_CONTAINER_NAME, "toxiproxy"],
    }


def _networks() -> dict[str, object]:
    # Dockerはinternal bridgeのendpointへgatewayを設定しない。
    return {
        f"{COMPOSE_PROJECT}_default": _network("172.30.0.2", 16),
        f"{COMPOSE_PROJECT}_fault-data": _network("172.30.250.2", 29, gateway=""),
        f"{COMPOSE_PROJECT}_fault-control": _network("172.30.251.2", 29, gateway=""),
    }


def _output(**overrides: object) -> bytes:
    value: dict[str, object] = {
        "CapAdd": [],
        "CapDrop": ["ALL"],
        "Cmd": list(TOXIPROXY_COMMAND),
        "Entrypoint": ["/toxiproxy"],
        "ExposedPorts": {"8474/tcp": {}},
        "Health": "healthy",
        "Id": CONTAINER_ID,
        "Image": TOXIPROXY_IMAGE,
        "Name": f"/{TOXIPROXY_CONTAINER_NAME}",
        "Networks": _networks(),
        "Mounts": [
            {
                "Type": "bind",
                "Source": CONFIG_SOURCE,
                "Destination": "/run/load-test/toxiproxy.json",
                "Mode": "ro",
                "RW": False,
                "Propagation": "rprivate",
            }
        ],
        "PortBindings": {},
        "Privileged": False,
        "Project": COMPOSE_PROJECT,
        "ReadonlyRootfs": True,
        "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
        "Running": True,
        "SecurityOpt": ["no-new-privileges:true"],
        "Service": "toxiproxy",
        "User": EXPECTED_USER,
    }
    value.update(overrides)
    return json.dumps(value, separators=(",", ":")).encode("ascii") + b"\n"


def _assert_invalid(body: bytes, *, image: str = TOXIPROXY_IMAGE) -> None:
    with pytest.raises(FaultControllerHostError) as captured:
        parse_toxiproxy_container(
            body,
            expected_image=image,
            expected_user=EXPECTED_USER,
            expected_config_source=CONFIG_SOURCE,
        )
    assert captured.value.code == "fault_host_toxiproxy_container_invalid"


def test_toxiproxy_container_identity_is_exact_and_normalizes_empty_bindings() -> None:
    first = parse_toxiproxy_container(
        _output(),
        expected_image=TOXIPROXY_IMAGE,
        expected_user=EXPECTED_USER,
        expected_config_source=CONFIG_SOURCE,
    )
    second = parse_toxiproxy_container(
        _output(PortBindings=None),
        expected_image=TOXIPROXY_IMAGE,
        expected_user=EXPECTED_USER,
        expected_config_source=CONFIG_SOURCE,
    )
    alternate_source = "/external/alternate-toxiproxy.json"
    alternate_body = json.loads(_output())
    alternate_body["Mounts"][0]["Source"] = alternate_source
    third = parse_toxiproxy_container(
        json.dumps(alternate_body, separators=(",", ":")).encode() + b"\n",
        expected_image=TOXIPROXY_IMAGE,
        expected_user=EXPECTED_USER,
        expected_config_source=alternate_source,
    )

    assert first.container_id == CONTAINER_ID
    assert first.control_network_id == "d" * 64
    assert len(first.identity_sha256) == 64
    assert first.identity_sha256 == second.identity_sha256
    assert first.identity_sha256 == third.identity_sha256


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("CapDrop", []),
        ("CapAdd", ["NET_ADMIN"]),
        ("Cmd", ["-host", "127.0.0.1"]),
        ("Entrypoint", ["/bin/sh"]),
        ("ExposedPorts", {"8474/tcp": {}, "8475/tcp": {}}),
        ("Health", "starting"),
        ("Id", "b" * 63),
        ("Image", f"registry/toxiproxy@sha256:{'d' * 64}"),
        ("Name", "/attacker"),
        ("Mounts", []),
        ("PortBindings", {"8474/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8474"}]}),
        ("Privileged", True),
        ("Project", "attacker"),
        ("ReadonlyRootfs", False),
        ("RestartPolicy", {"Name": "always", "MaximumRetryCount": 0}),
        ("Running", False),
        ("SecurityOpt", []),
        ("Service", "fault-control-client"),
        ("User", "0:0" if EXPECTED_USER != "0:0" else "65534:65534"),
    ],
)
def test_toxiproxy_container_rejects_runtime_drift(field: str, value: object) -> None:
    _assert_invalid(_output(**{field: value}))


def test_toxiproxy_container_rejects_image_outside_release_identity() -> None:
    _assert_invalid(
        _output(),
        image=f"registry/toxiproxy@sha256:{'d' * 64}",
    )


def test_toxiproxy_container_rejects_wrong_config_bind_source() -> None:
    mounts = _output()
    value = json.loads(mounts)
    value["Mounts"][0]["Source"] = "/external/replaced.json"
    _assert_invalid(json.dumps(value, separators=(",", ":")).encode() + b"\n")


def test_internal_data_gateway_empty_accepts_real_endpoint_and_rejects_legacy_fixture() -> None:
    # 実Dockerの空Gatewayを正本にし、旧fixtureの架空gateway一fieldだけで拒否する。
    actual = json.loads(_output())
    data = actual["Networks"][f"{COMPOSE_PROJECT}_fault-data"]
    assert data["Gateway"] == ""
    observation = parse_toxiproxy_container(
        _output(),
        expected_image=TOXIPROXY_IMAGE,
        expected_user=EXPECTED_USER,
        expected_config_source=CONFIG_SOURCE,
    )
    assert observation.container_id == CONTAINER_ID

    data["Gateway"] = "172.30.0.1"
    _assert_invalid(json.dumps(actual, separators=(",", ":")).encode() + b"\n")


@pytest.mark.parametrize(
    ("network", "gateway"),
    [
        ("default", ""),
        ("default", "not-an-address"),
        ("default", "::1"),
        ("fault-data", "172.30.250.1"),
        ("fault-control", "172.30.251.1"),
    ],
)
def test_toxiproxy_gateway_contract_rejects_one_field_drift(network: str, gateway: str) -> None:
    value = json.loads(_output())
    value["Networks"][f"{COMPOSE_PROJECT}_{network}"]["Gateway"] = gateway
    _assert_invalid(json.dumps(value, separators=(",", ":")).encode() + b"\n")


@pytest.mark.parametrize(
    "networks",
    [
        {},
        {
            **_networks(),
            f"{COMPOSE_PROJECT}_metrics": _network("172.31.0.2", 16),
        },
        {
            name: endpoint
            for name, endpoint in _networks().items()
            if name != f"{COMPOSE_PROJECT}_fault-control"
        },
        {
            **_networks(),
            f"{COMPOSE_PROJECT}_fault-control": _network("172.30.251.3", 29, gateway=""),
        },
        {
            **_networks(),
            f"{COMPOSE_PROJECT}_fault-data": _network("172.30.250.2", 28, gateway=""),
        },
        {
            **_networks(),
            f"{COMPOSE_PROJECT}_fault-control": _network("172.30.251.2", 29),
        },
    ],
)
def test_toxiproxy_container_rejects_network_drift(networks: object) -> None:
    _assert_invalid(_output(Networks=networks))


def test_toxiproxy_container_rejects_ambiguous_or_oversized_output() -> None:
    duplicate = _output().replace(b'"Running":true', b'"Running":false,"Running":true')
    _assert_invalid(duplicate)
    _assert_invalid(b"{}")
    _assert_invalid(b"x" * (16 * 1024 + 1) + b"\n")
