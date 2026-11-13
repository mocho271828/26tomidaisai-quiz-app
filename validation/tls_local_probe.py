"""Redisの実TLSと信頼CA・接続先名の拒否を非秘密JSONに記録する。"""

import json
import socket
import ssl
from pathlib import Path
from time import monotonic
from typing import Literal

from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings
from redis import Redis


class ProbeSettings(BaseSettings):
    local_tls_redis_password: SecretStr
    local_tls_ca_bundle: Path


class ProbeResult(BaseModel):
    kind: Literal["trusted", "untrusted_ca", "wrong_hostname", "authenticated_ping"]
    passed: bool
    elapsed_seconds: float
    tls_version: str | None = None


def handshake(
    context: ssl.SSLContext,
    server_hostname: str,
    kind: Literal["trusted", "untrusted_ca", "wrong_hostname"],
) -> ProbeResult:
    """失敗理由の生文字列は出さず、検証例外の種類だけで負例を判定する。"""

    started = monotonic()
    try:
        with socket.create_connection(("redis", 6379), timeout=2) as connection:
            with context.wrap_socket(connection, server_hostname=server_hostname) as secure:
                return ProbeResult(
                    kind=kind,
                    passed=kind == "trusted",
                    elapsed_seconds=monotonic() - started,
                    tls_version=secure.version(),
                )
    except ssl.SSLCertVerificationError:
        return ProbeResult(
            kind=kind, passed=kind != "trusted", elapsed_seconds=monotonic() - started
        )


def main() -> None:
    settings = ProbeSettings()
    trusted_context = ssl.create_default_context(cafile=str(settings.local_tls_ca_bundle))
    results = [
        handshake(trusted_context, "redis", "trusted"),
        handshake(ssl.create_default_context(), "redis", "untrusted_ca"),
        handshake(trusted_context, "wrong-host.invalid", "wrong_hostname"),
    ]
    started = monotonic()
    with Redis(
        host="redis",
        port=6379,
        password=settings.local_tls_redis_password.get_secret_value(),
        ssl=True,
        ssl_cert_reqs="required",
        ssl_check_hostname=True,
        ssl_ca_certs=str(settings.local_tls_ca_bundle),
        socket_connect_timeout=2,
        socket_timeout=2,
    ) as client:
        results.append(
            ProbeResult(
                kind="authenticated_ping",
                passed=client.ping() is True,
                elapsed_seconds=monotonic() - started,
            )
        )
    print(json.dumps([result.model_dump() for result in results]))
    if not all(result.passed for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
