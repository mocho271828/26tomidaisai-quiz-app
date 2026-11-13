"""試験専用CAと接続先名を検証するleaf証明書を非上書きで作る。"""

import argparse
import ipaddress
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from pydantic import BaseModel, ConfigDict, field_validator


class CertificateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    directory: Path
    gateway_hosts: list[str]

    @field_validator("gateway_hosts")
    @classmethod
    def validate_hosts(cls, hosts: list[str]) -> list[str]:
        if not hosts:
            raise ValueError("gateway hosts required")
        for host in hosts:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?", host):
                    raise ValueError("invalid gateway DNS name") from None
        return sorted(set(hosts + ["localhost", "127.0.0.1", "gateway"]))


def _write(path: Path, content: bytes, mode: int = 0o600) -> None:
    with path.open("xb") as output:
        os.chmod(path, mode)
        output.write(content)


def _private_bytes(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _san(host: str) -> x509.GeneralName:
    try:
        return x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        return x509.DNSName(host)


def generate_certificates(request: CertificateRequest) -> None:
    """CA秘密鍵はserverへmountするleaf directoryから分離する。"""

    request.directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    now = datetime.now(UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Quiz Local TLS Test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    _write(request.directory / "ca.key", _private_bytes(ca_key))
    ca_bytes = ca.public_bytes(serialization.Encoding.PEM)
    _write(request.directory / "ca.crt", ca_bytes, 0o644)
    for service, hosts in (
        ("gateway", request.gateway_hosts),
        ("postgres", ["postgres"]),
        ("redis", ["redis"]),
    ):
        leaf = request.directory / service
        leaf.mkdir(mode=0o700)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, service)]))
            .issuer_name(ca.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=14))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.SubjectAlternativeName([_san(host) for host in hosts]), critical=False
            )
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        _write(leaf / "server.key", _private_bytes(key))
        _write(leaf / "server.crt", certificate.public_bytes(serialization.Encoding.PEM), 0o644)
        _write(leaf / "ca.crt", ca_bytes, 0o644)
    print(
        json.dumps(
            {
                "status": "generated",
                "gateway_hosts": request.gateway_hosts,
                "ca_sha256": ca.fingerprint(hashes.SHA256()).hex(),
                "valid_days": 14,
            }
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(".local/tls/certificates"))
    parser.add_argument("--gateway-host", action="append", required=True)
    arguments = parser.parse_args()
    generate_certificates(
        CertificateRequest(directory=arguments.directory, gateway_hosts=arguments.gateway_host)
    )


if __name__ == "__main__":
    main()
