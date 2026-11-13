import ssl
import stat
from pathlib import Path

import pytest
from cryptography import x509
from pydantic import ValidationError

from validation.tls_local_certificates import CertificateRequest, generate_certificates


def test_generated_certificates_preserve_private_key_permissions_and_name_checks(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "certificates"
    generate_certificates(CertificateRequest(directory=directory, gateway_hosts=["192.168.1.2"]))
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for key in directory.rglob("*.key"):
        assert stat.S_IMODE(key.stat().st_mode) == 0o600
    context = ssl.create_default_context(cafile=str(directory / "ca.crt"))
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    leaf = x509.load_pem_x509_certificate((directory / "gateway/server.crt").read_bytes())
    names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "localhost" in names.get_values_for_type(x509.DNSName)
    assert "192.168.1.2" in [str(ip) for ip in names.get_values_for_type(x509.IPAddress)]
    assert not (directory / "gateway/ca.key").exists()
    with pytest.raises(FileExistsError):
        generate_certificates(CertificateRequest(directory=directory, gateway_hosts=["localhost"]))


@pytest.mark.parametrize("host", ["https://example.test", "x\nIP:1.2.3.4", "*.test", "a/b"])
def test_certificate_names_reject_configuration_injection(tmp_path: Path, host: str) -> None:
    with pytest.raises(ValidationError):
        CertificateRequest(directory=tmp_path / "certs", gateway_hosts=[host])
