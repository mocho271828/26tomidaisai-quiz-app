"""Gateway/APIのidle期限を共通契約として確認する。"""

import json
import re
import shlex
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def _api_idle_seconds() -> tuple[int, int]:
    command_line = next(
        line
        for line in (ROOT / "backend/Dockerfile").read_text().splitlines()
        if line.startswith("CMD ")
    )
    image_command: list[str] = json.loads(command_line.removeprefix("CMD "))
    tls_command = shlex.split(
        next(
            line
            for line in (ROOT / "infra/local-tls/api-start.sh").read_text().splitlines()
            if line.startswith("exec uvicorn ")
        )
    )
    return (
        int(image_command[image_command.index("--timeout-keep-alive") + 1]),
        int(tls_command[tls_command.index("--timeout-keep-alive") + 1]),
    )


@pytest.mark.parametrize(
    "configuration",
    [
        "infra/Caddyfile",
        "infra/Caddyfile.load-test",
        "infra/local-tls/Caddyfile",
        "infra/Caddyfile.local-multi-api",
        "infra/local-tls/Caddyfile.multi-api",
    ],
)
def test_gateway_retires_idle_connections_before_both_api_startup_paths(
    configuration: str,
) -> None:
    image_idle, tls_idle = _api_idle_seconds()
    source = (ROOT / configuration).read_text()
    budgets = re.findall(r"^\s*keepalive\s+(\d+)s\s*$", source, flags=re.MULTILINE)
    assert len(budgets) == source.count("reverse_proxy ")
    assert image_idle == tls_idle
    assert all(0 < int(budget) < image_idle for budget in budgets)
