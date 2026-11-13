import io
import json
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from dotenv import dotenv_values

from validation import lan_test_config as lan

# 実資格情報ではなく、失敗時の出力漏洩を検出する固定fixtureである。
DB_PASSWORD = "existing-db-secret-do-not-log"  # noqa: S105
SESSION_SECRET = "existing-session-secret-do-not-log"  # noqa: S105
OPERATOR_PASSWORD = "existing-operator-secret-do-not-log"  # noqa: S105
BACKUP_KEY = "existing-backup-key-do-not-log="
REDIS_PASSWORD = "b" * 64


def compose_body(certificates: Path, *, tls: bool = False) -> bytes:
    environment = {
        "APP_ENV": "local-demo",
        "DATABASE_URL": f"postgresql+psycopg://quiz:{DB_PASSWORD}@postgres:5432/quiz_demo",
        "SESSION_SECRET": SESSION_SECRET,
        "DEMO_OPERATOR_USERNAME": "existing-operator",
        "DEMO_OPERATOR_PASSWORD": OPERATOR_PASSWORD,
        "ENABLE_DEMO_AUTH": "true",
        "ROUND_BACKUP_STORAGE_MODE": "filesystem",
        "ROUND_BACKUP_DIRECTORY": "/var/lib/quiz-round-backups",
        "ROUND_BACKUP_ENCRYPTION_KEY": BACKUP_KEY,
        "ENABLE_DEMO_SEED": "false" if tls else "true",
        "SESSION_COOKIE_SECURE": "true" if tls else "false",
        "LOGFIRE_SEND_TO_LOGFIRE": "false",
        "REALTIME_ALLOWED_ORIGINS": json.dumps([lan.ORIGIN if tls else "http://localhost:8080"]),
        "REDIS_URL": f"rediss://:{REDIS_PASSWORD}@redis:6379/0" if tls else "redis://redis:6379/0",
    }
    if tls:
        environment.update(PGSSLMODE="verify-full", PGSSLROOTCERT="/run/local-ca/ca.crt")

    def mounts(service: str, target: str = "/run/tls") -> list[dict[str, str | bool]]:
        return (
            [
                {
                    "type": "bind",
                    "source": str(certificates / service),
                    "target": target,
                    "read_only": True,
                }
            ]
            if tls
            else []
        )

    api_mounts = mounts("ca.crt", "/run/local-ca/ca.crt")
    return json.dumps(
        {
            "name": "gs-app-tomidaisai-tls" if tls else "gs-app-tomidaisai",
            "services": {
                "postgres": {
                    "environment": {
                        "POSTGRES_DB": "quiz_demo",
                        "POSTGRES_USER": "quiz",
                        "POSTGRES_PASSWORD": DB_PASSWORD,
                    },
                    "volumes": [
                        {
                            "type": "volume",
                            "source": "postgres-data",
                            "target": "/var/lib/postgresql/data",
                        },
                        *mounts("postgres"),
                    ],
                },
                "api": {"environment": environment, "volumes": api_mounts},
                "api-secondary": {"environment": environment, "volumes": api_mounts},
                "redis": {
                    "volumes": mounts("redis"),
                    "environment": {"REDIS_PASSWORD": REDIS_PASSWORD} if tls else {},
                },
                "gateway": {
                    "volumes": mounts("gateway"),
                    "ports": [
                        {
                            "host_ip": "10.0.1.2" if tls else "127.0.0.1",
                            "published": "8443" if tls else "8080",
                            "target": 8443 if tls else 8080,
                        }
                    ],
                    "build": {
                        "args": {
                            "VITE_EVENT_START_AT": "2026-10-24T10:00:00+09:00",
                            "VITE_SURVEY_URL": "https://example.test/survey?a=1&b=2",
                            "VITE_HOME_URL": "",
                        }
                    },
                },
            },
            "volumes": {"postgres-data": {"name": lan.POSTGRES_VOLUME, "external": tls}},
        }
    ).encode()


@pytest.fixture
def directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "lan-server"
    root.mkdir(mode=0o700)
    (root / "certificates").mkdir(mode=0o700)
    monkeypatch.setattr(lan.secrets, "token_hex", lambda _count: REDIS_PASSWORD)
    return root


def test_generate_preserves_existing_db_auth_backups_and_ui_without_overwriting(
    directory: Path,
) -> None:
    lan.generate_runtime(compose_body(directory / "certificates"), directory)
    path = directory / "runtime.env"
    values = dotenv_values(path, interpolate=False)
    assert values["POSTGRES_PASSWORD"] == DB_PASSWORD
    assert values["SESSION_SECRET"] == SESSION_SECRET
    assert values["DEMO_OPERATOR_PASSWORD"] == OPERATOR_PASSWORD
    assert values["ROUND_BACKUP_ENCRYPTION_KEY"] == BACKUP_KEY
    assert values["LOCAL_TLS_POSTGRES_VOLUME"] == lan.POSTGRES_VOLUME
    assert values["LOCAL_TLS_CERT_DIRECTORY"] == str(directory / "certificates")
    assert values["LOCAL_TLS_REDIS_PASSWORD"] == REDIS_PASSWORD
    assert values["LOCAL_TLS_BIND_ADDRESS"] == "10.0.1.2"
    assert values["LOCAL_TLS_PORT"] == "8443"
    assert json.loads(values["LOCAL_TLS_ALLOWED_ORIGINS"] or "null") == [lan.ORIGIN]
    assert values["VITE_SURVEY_URL"] == "https://example.test/survey?a=1&b=2"
    assert values["VITE_HOME_URL"] == ""
    assert values["ENABLE_DEMO_SEED"] == values["LOGFIRE_SEND_TO_LOGFIRE"] == "false"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        lan.generate_runtime(compose_body(directory / "certificates"), directory)
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("gs-app-tomidaisai_postgres-data", "other_postgres-data"),
        ('"name": "gs-app-tomidaisai"', '"name": "gs-app-tomidaisai-tls"'),
        ('"POSTGRES_DB": "quiz_demo"', '"POSTGRES_DB": "other"'),
        ('"POSTGRES_USER": "quiz"', '"POSTGRES_USER": "other"'),
        ('"source": "postgres-data"', '"source": "unrelated-data"'),
        (DB_PASSWORD, "unsafe@password"),
        (DB_PASSWORD, "unsafe%40password"),
        (DB_PASSWORD, "unsafe/password"),
        (SESSION_SECRET, "unsafe$EXPANSION"),
        (OPERATOR_PASSWORD, "unsafe#comment"),
        (BACKUP_KEY, "unsafe\\nNEXT=secret"),
        ("https://example.test/survey?a=1&b=2", "https://example.test/#fragment"),
    ],
)
def test_source_configuration_refuses_unknown_target_and_ambiguous_values(
    directory: Path,
    before: str,
    after: str,
) -> None:
    body = compose_body(directory / "certificates").decode().replace(before, after).encode()
    with pytest.raises(ValueError):
        lan.generate_runtime(body, directory)
    assert not (directory / "runtime.env").exists()


def test_source_rejects_mismatched_api_credentials(directory: Path) -> None:
    body = (
        compose_body(directory / "certificates")
        .decode()
        .replace(SESSION_SECRET, "mismatched-secret", 1)
        .encode()
    )
    with pytest.raises(ValueError, match="api_configuration_mismatch"):
        lan.generate_runtime(body, directory)


def test_tls_check_verifies_generated_env_and_does_not_change_it(directory: Path) -> None:
    lan.generate_runtime(compose_body(directory / "certificates"), directory)
    before = (directory / "runtime.env").read_bytes()
    lan.check_runtime(compose_body(directory / "certificates", tls=True), directory)
    assert (directory / "runtime.env").read_bytes() == before


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (DB_PASSWORD, "overridden-password"),
        (SESSION_SECRET, "overridden-secret"),
        (BACKUP_KEY, "overridden-backup-key"),
        (REDIS_PASSWORD, "overridden-redis-password"),
        ('"host_ip": "10.0.1.2"', '"host_ip": "0.0.0.0"'),
        ('"published": "8443"', '"published": "8444"'),
        ('"target": 8443', '"target": 8080'),
        ('"SESSION_COOKIE_SECURE": "true"', '"SESSION_COOKIE_SECURE": "false"'),
        ('"ENABLE_DEMO_SEED": "false"', '"ENABLE_DEMO_SEED": "true"'),
        ('"LOGFIRE_SEND_TO_LOGFIRE": "false"', '"LOGFIRE_SEND_TO_LOGFIRE": "true"'),
        ('"verify-full"', '"require"'),
        ("/run/local-ca/ca.crt", "/run/unrelated-ca/ca.crt"),
        ("https://10.0.1.2:8443", "https://10.0.1.3:8443"),
        ('"read_only": true', '"read_only": false'),
        ('"external": true', '"external": false'),
    ],
)
def test_tls_check_refuses_ambient_overrides_or_weakened_boundary(
    directory: Path,
    before: str,
    after: str,
) -> None:
    lan.generate_runtime(compose_body(directory / "certificates"), directory)
    body = (
        compose_body(directory / "certificates", tls=True).decode().replace(before, after).encode()
    )
    with pytest.raises(ValueError):
        lan.check_runtime(body, directory)


def test_tls_check_refuses_wrong_certificate_source(directory: Path) -> None:
    lan.generate_runtime(compose_body(directory / "certificates"), directory)
    body = (
        compose_body(directory / "certificates", tls=True)
        .decode()
        .replace(str(directory / "certificates" / "redis"), "/other/redis")
        .encode()
    )
    with pytest.raises(ValueError, match="certificate_mount"):
        lan.check_runtime(body, directory)


def test_private_file_boundaries_reject_symlink_hardlink_and_shared_mode(directory: Path) -> None:
    lan.generate_runtime(compose_body(directory / "certificates"), directory)
    path = directory / "runtime.env"
    body = compose_body(directory / "certificates", tls=True)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="runtime_file_invalid"):
        lan.check_runtime(body, directory)
    path.chmod(0o600)
    alias = directory / "duplicate.env"
    os.link(path, alias)
    with pytest.raises(ValueError, match="runtime_file_invalid"):
        lan.check_runtime(body, directory)
    path.unlink()
    path.symlink_to(alias)
    with pytest.raises(OSError):
        lan.check_runtime(body, directory)


def test_shared_or_symlink_parent_is_rejected(directory: Path) -> None:
    directory.chmod(0o755)
    with pytest.raises(ValueError, match="directory_permissions"):
        lan.generate_runtime(compose_body(directory / "certificates"), directory)
    directory.chmod(0o700)
    alias = directory.parent / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    with pytest.raises(ValueError, match="directory_invalid"):
        lan.generate_runtime(compose_body(directory / "certificates"), alias)


@pytest.mark.parametrize("case", ["success", "validation", "io", "oversize"])
def test_cli_never_emits_secret_values_or_validation_error_contents(
    directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    body = compose_body(directory / "certificates")
    if case == "validation":
        body = body.replace(b'"APP_ENV": "local-demo"', f'"APP_ENV": "{SESSION_SECRET}"'.encode())
    if case == "io":
        (directory / "runtime.env").write_text(OPERATOR_PASSWORD)
    if case == "oversize":
        body = SESSION_SECRET.encode() * (lan.MAX_CONFIG_BYTES // len(SESSION_SECRET) + 1)
    monkeypatch.setattr(lan, "RUNTIME_DIRECTORY", directory)
    monkeypatch.setattr(lan.sys, "stdin", io.TextIOWrapper(io.BytesIO(body)))
    monkeypatch.setattr(lan.logfire, "configure", MagicMock())
    info, error = MagicMock(), MagicMock()
    monkeypatch.setattr(lan.logfire, "info", info)
    monkeypatch.setattr(lan.logfire, "error", error)
    assert lan.main([]) == (0 if case == "success" else 1)
    captured = capsys.readouterr()
    output = captured.out + captured.err + str(info.call_args_list) + str(error.call_args_list)
    for secret in (DB_PASSWORD, SESSION_SECRET, OPERATOR_PASSWORD, BACKUP_KEY):
        assert secret not in output
    assert "input_value" not in output
    if case != "success":
        assert error.call_args.kwargs["stack"]
        assert error.call_args.kwargs["_exc_info"] is False


@pytest.mark.parametrize("service", ["postgres", "redis", "api", "api-secondary"])
def test_tls_check_rejects_published_internal_service_ports(directory: Path, service: str) -> None:
    lan.generate_runtime(compose_body(directory / "certificates"), directory)
    body = (
        compose_body(directory / "certificates", tls=True)
        .decode()
        .replace(
            f'"{service}": {{',
            f'"{service}": {{"ports":[{{"published":"5432","target":5432}}],',
            1,
        )
        .encode()
    )
    with pytest.raises(ValueError):
        lan.check_runtime(body, directory)


def test_custom_postgres_data_directory_cannot_point_away_from_retained_volume(
    directory: Path,
) -> None:
    body = (
        compose_body(directory / "certificates")
        .decode()
        .replace(
            '"POSTGRES_DB": "quiz_demo"',
            '"POSTGRES_DB": "quiz_demo", "PGDATA": "/other/data"',
        )
        .encode()
    )
    with pytest.raises(ValueError):
        lan.generate_runtime(body, directory)


def test_cli_check_mode_reuses_fixed_private_file_without_writing(
    directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lan.generate_runtime(compose_body(directory / "certificates"), directory)
    before = (directory / "runtime.env").stat()
    monkeypatch.setattr(lan, "RUNTIME_DIRECTORY", directory)
    monkeypatch.setattr(
        lan.sys,
        "stdin",
        io.TextIOWrapper(io.BytesIO(compose_body(directory / "certificates", tls=True))),
    )
    monkeypatch.setattr(lan.logfire, "configure", MagicMock())
    monkeypatch.setattr(lan.logfire, "info", MagicMock())
    assert lan.main(["--check"]) == 0
    assert capsys.readouterr().out == '{"status":"ok"}\n'
    after = (directory / "runtime.env").stat()
    assert (before.st_ino, before.st_mtime_ns, before.st_size) == (
        after.st_ino,
        after.st_mtime_ns,
        after.st_size,
    )
