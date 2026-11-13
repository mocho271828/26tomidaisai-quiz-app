"""通常local DBの資格情報を保持して、固定LAN用TLS設定を非上書き生成する。"""

import argparse
import io
import json
import os
import re
import secrets
import stat
import sys
import traceback
from pathlib import Path

import logfire
from dotenv import dotenv_values

from validation.lan_test_config_models import ComposeInput, Port, RuntimeValues

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIRECTORY = ROOT / ".local" / "lan-server"
POSTGRES_VOLUME = "gs-app-tomidaisai_postgres-data"
ORIGIN = "https://10.0.1.2:8443"
MAX_CONFIG_BYTES = 2 * 1024 * 1024


def safe_env(value: str, *, empty: bool = False) -> str:
    """dotenvで展開・切捨て・改行される値を、独自escapeで変換せず拒否する。"""
    if (not value and not empty) or any(
        ord(character) < 33 or ord(character) > 126 or character in "\"'`\\$#"
        for character in value
    ):
        raise ValueError("lan_env_value_unsupported")
    return value


def expected_values(
    config: ComposeInput, certificates: Path, redis_password: str
) -> dict[str, str]:
    password = config.password.get_secret_value()
    if re.fullmatch(r"[A-Za-z0-9_.~-]+", password) is None:
        raise ValueError("lan_database_password_requires_url_unreserved_characters")
    database_url = f"postgresql+psycopg://quiz:{password}@postgres:5432/quiz_demo"
    if any(
        service.environment.database_url.get_secret_value() != database_url
        for service in (config.api, config.secondary)
    ):
        raise ValueError("lan_database_connection_mismatch")
    if config.api.environment.retained() != config.secondary.environment.retained():
        raise ValueError("lan_api_configuration_mismatch")
    data_mounts = [
        mount for mount in config.postgres_mounts if mount.target == "/var/lib/postgresql/data"
    ]
    if (
        len(data_mounts) != 1
        or data_mounts[0].type != "volume"
        or data_mounts[0].source != "postgres-data"
        or data_mounts[0].read_only
    ):
        raise ValueError("lan_postgres_mount_mismatch")
    values = {
        **config.api.environment.retained(),
        "POSTGRES_PASSWORD": password,
        "LOCAL_TLS_CERT_DIRECTORY": str(certificates),
        "LOCAL_TLS_POSTGRES_VOLUME": POSTGRES_VOLUME,
        "LOCAL_TLS_REDIS_PASSWORD": redis_password,
        "LOCAL_TLS_BIND_ADDRESS": "10.0.1.2",
        "LOCAL_TLS_PORT": "8443",
        "LOGFIRE_SEND_TO_LOGFIRE": "false",
        "ENABLE_DEMO_SEED": "false",
        "SESSION_COOKIE_SECURE": "true",
        "VITE_EVENT_START_AT": config.gateway_args.event_start,
        "VITE_SURVEY_URL": config.gateway_args.survey,
        "VITE_HOME_URL": config.gateway_args.home,
    }
    for key, value in values.items():
        safe_env(value, empty=key.startswith("VITE_"))
    values["LOCAL_TLS_ALLOWED_ORIGINS"] = json.dumps([ORIGIN], separators=(",", ":"))
    return values


def check_directory(directory: Path) -> None:
    if not directory.is_absolute() or any(
        parent.is_symlink() for parent in (directory, *directory.parents)
    ):
        raise ValueError("lan_directory_invalid")
    status = directory.stat()
    if (
        not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) != 0o700
    ):
        raise ValueError("lan_directory_permissions_invalid")


def generate_runtime(body: bytes, directory: Path) -> None:
    """DBへ接続せず、検証済み通常Composeの値だけを新規0600 fileへ保存する。"""
    config = ComposeInput.model_validate_json(body)
    if config.name != "gs-app-tomidaisai":
        raise ValueError("lan_source_project_invalid")
    certificates = directory / "certificates"
    check_directory(certificates)
    values = expected_values(config, certificates, secrets.token_hex(32))
    check_directory(directory)
    with os.fdopen(
        os.open(
            directory / "runtime.env", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        ),
        "w",
    ) as file:
        os.fchmod(file.fileno(), 0o600)
        file.write("".join(f"{key}={value}\n" for key, value in values.items()))
        file.flush()
        os.fsync(file.fileno())


def check_runtime(body: bytes, directory: Path) -> None:
    """TLS合成結果を保存済み設定と照合し、ambient envによる切替を拒否する。"""
    check_directory(directory)
    certificates = directory / "certificates"
    check_directory(certificates)
    with os.fdopen(
        os.open(directory / "runtime.env", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "r"
    ) as file:
        status = os.fstat(file.fileno())
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.geteuid()
            or status.st_nlink != 1
            or stat.S_IMODE(status.st_mode) != 0o600
            or not 0 < status.st_size <= 65536
        ):
            raise ValueError("lan_runtime_file_invalid")
        saved = RuntimeValues.model_validate(
            dotenv_values(stream=io.StringIO(file.read(65537)), interpolate=False)
        )
    config = ComposeInput.model_validate_json(body)
    if (
        config.name != "gs-app-tomidaisai-tls"
        or config.redis_password is None
        or not config.external_volume
        or config.postgres_ports
        or config.redis_ports
    ):
        raise ValueError("lan_tls_project_invalid")
    redis_password = config.redis_password.get_secret_value()
    if saved != RuntimeValues.model_validate(expected_values(config, certificates, redis_password)):
        raise ValueError("lan_runtime_configuration_mismatch")
    for service in (config.api, config.secondary):
        env = service.environment
        if (
            (env.secure, env.seed, env.logfire, env.ssl_mode, env.ssl_root)
            != ("true", "false", "false", "verify-full", "/run/local-ca/ca.crt")
            or json.loads(env.origins) != [ORIGIN]
            or env.redis_url.get_secret_value() != f"rediss://:{redis_password}@redis:6379/0"
            or service.ports
        ):
            raise ValueError("lan_tls_environment_invalid")
    if len(config.gateway_ports) != 1 or config.gateway_ports[0] != Port(
        host_ip="10.0.1.2", published="8443", target=8443
    ):
        raise ValueError("lan_tls_gateway_bind_invalid")
    for mounts, source, target in (
        (config.postgres_mounts, certificates / "postgres", "/run/tls"),
        (config.redis_mounts, certificates / "redis", "/run/tls"),
        (config.gateway_mounts, certificates / "gateway", "/run/tls"),
        (config.api.volumes, certificates / "ca.crt", "/run/local-ca/ca.crt"),
        (config.secondary.volumes, certificates / "ca.crt", "/run/local-ca/ca.crt"),
    ):
        selected = [mount for mount in mounts if mount.target == target]
        if (
            len(selected) != 1
            or selected[0].type != "bind"
            or selected[0].source != str(source)
            or not selected[0].read_only
        ):
            raise ValueError("lan_tls_certificate_mount_invalid")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args(argv)
    logfire.configure(send_to_logfire=False, console=logfire.ConsoleOptions(output=sys.stderr))
    try:
        logfire.info(
            "lan_test_config_started", operation="check" if arguments.check else "generate"
        )
        body = sys.stdin.buffer.read(MAX_CONFIG_BYTES + 1)
        if not 0 < len(body) <= MAX_CONFIG_BYTES:
            raise ValueError("lan_compose_input_size_invalid")
        (check_runtime if arguments.check else generate_runtime)(body, RUNTIME_DIRECTORY)
        logfire.info("lan_test_config_complete")
        print('{"status":"ok"}')
        return 0
    except (OSError, ValueError) as error:
        stack = [
            {"function": frame.name, "line": frame.lineno}
            for frame in traceback.extract_tb(error.__traceback__)
            if frame.filename == __file__
        ]
        logfire.error("lan_test_config_failed", stack=stack, _exc_info=False)
        print(
            '{"status":"failed","code":"lan_test_config_invalid_or_unavailable"}', file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
