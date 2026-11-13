from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from tests.test_load_test_aws_deployment import payload_fixture

from quiz_app.config import AWS_DEMO_POSTGRES_CA_CONTAINER_PATH, Settings
from quiz_app.load_test_aws import deployment_host


def test_activation_keeps_container_settings_and_uses_host_ca_for_both_database_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manifest, _, payload = payload_fixture()
    root = tmp_path.resolve()
    for file in payload.app:
        if file.path not in {"runtime/runtime.env", "inputs/operator.json"}:
            continue
        path = root / file.path
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.write_bytes(file.body.get_secret_value())
        path.chmod(0o600)
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.delenv("PGSSLROOTCERT", raising=False)
    monkeypatch.setattr(deployment_host, "load_manifest", lambda *args: manifest)
    monkeypatch.setattr(deployment_host, "validate_remote_dependency_host", MagicMock())
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.mappings.return_value.one.return_value = {
        "safe_role": True,
        "owned_relations": 0,
        "owned_schemas": 0,
        "owns_database": False,
    }

    def connect(*args: object) -> MagicMock:
        assert os.environ["PGSSLROOTCERT"] == str(root / "ca/postgres.pem")
        return engine

    def bootstrap(settings: Settings, *args: object) -> None:
        assert settings.postgres_ssl_root_cert == AWS_DEMO_POSTGRES_CA_CONTAINER_PATH
        assert os.environ["PGSSLROOTCERT"] == str(root / "ca/postgres.pem")
        raise RuntimeError("stop_before_container_operations")

    monkeypatch.setattr(deployment_host, "create_pinned_postgres_engine", connect)
    monkeypatch.setattr(deployment_host, "prepare_fresh_load_test_run", bootstrap)
    with pytest.raises(RuntimeError, match="stop_before_container"):
        deployment_host.activate(root, SimpleNamespace(role="app"))
    engine.dispose.assert_called_once()
