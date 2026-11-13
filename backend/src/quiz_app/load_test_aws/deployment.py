"""run専用runtimeと秘密値を生成する。SQLは人向けartifactだけにし実行しない。"""

from __future__ import annotations

import io
import json
import secrets
import tarfile
from datetime import timedelta
from hashlib import sha256
from ipaddress import IPv4Address
from pathlib import Path
from urllib.parse import quote

from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from psycopg import sql
from pydantic import SecretBytes, SecretStr

from quiz_app.load_test.controller_contracts import manifest_context
from quiz_app.load_test.db_verifier_queries import APPLICATION_SELECT_COLUMN_ALLOWLIST
from quiz_app.load_test.manifest import LoadTestManifest
from quiz_app.load_test.preparation import run_operator_username
from quiz_app.load_test_aws.deployment_models import (
    DeploymentFile,
    DeploymentSettings,
    RuntimePayload,
    RuntimeSecrets,
    TlsMaterial,
)
from quiz_app.load_test_aws.models import AwsRunPlan

RUNTIME_UID = 1001
RUN_BASE = Path("/var/lib/gs-loadtest")
REPOSITORY = Path("/opt/gs-loadtest/repository")


def new_runtime_secrets(redis_application: SecretStr, redis_monitor: SecretStr) -> RuntimeSecrets:
    """AWS Redis ACLと独立したrun固有credentialを作り、表示は常に秘匿する。"""

    def password() -> SecretStr:
        return SecretStr("Aa1" + secrets.token_urlsafe(36))

    return RuntimeSecrets(
        application_database_password=password(),
        verifier_database_password=password(),
        postgres_exporter_password=password(),
        redis_application_password=redis_application,
        redis_monitor_password=redis_monitor,
        session_secret=password(),
        backup_encryption_key=SecretStr(Fernet.generate_key().decode("ascii")),
        operator_password=password(),
    )


def create_tls_material(plan: AwsRunPlan, address: IPv4Address) -> TlsMaterial:
    """CA秘密鍵は返さず破棄し、run期限と同時に失効するIP SAN証明書を作る。"""
    authority_key = ec.generate_private_key(ec.SECP256R1())
    server_key = ec.generate_private_key(ec.SECP256R1())
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, plan.prefix)])

    def builder(public_key: ec.EllipticCurvePublicKey) -> x509.CertificateBuilder:
        return (
            x509.CertificateBuilder()
            .issuer_name(issuer)
            .public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(plan.created_at - timedelta(minutes=5))
            .not_valid_after(plan.expires_at)
        )

    authority = (
        builder(authority_key.public_key())
        .subject_name(issuer)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            critical=True,
        )
        .sign(authority_key, hashes.SHA256())
    )
    server = (
        builder(server_key.public_key())
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, str(address))]))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(address)]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(authority_key, hashes.SHA256())
    )
    return TlsMaterial(
        ca_certificate=SecretBytes(authority.public_bytes(serialization.Encoding.PEM)),
        server_certificate=SecretBytes(server.public_bytes(serialization.Encoding.PEM)),
        server_key=SecretBytes(
            server_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        ),
        expires_at=plan.expires_at,
    )


def human_database_sql(credentials: RuntimeSecrets) -> bytes:
    """人がschema適用後quizadminで実行するcolumn最小権限SQLを生成する。"""
    statements = [
        "-- Human only: apply the repository schema as quizadmin first, then run this file.",
        "-- This file creates roles/grants; the agent never executes it.",
        "BEGIN;",
    ]
    for role, password in (
        ("quiz_app", credentials.application_database_password),
        ("quiz_verify", credentials.verifier_database_password),
        ("quiz_metrics", credentials.postgres_exporter_password),
    ):
        statements.append(
            sql.SQL(
                "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION PASSWORD {};"
            )
            .format(sql.Identifier(role), sql.Literal(password.get_secret_value()))
            .as_string()
        )
    statements.extend(
        (
            "REVOKE CREATE, TEMPORARY ON DATABASE quiz FROM PUBLIC;",
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC;",
            "GRANT CONNECT ON DATABASE quiz TO quiz_app, quiz_verify, quiz_metrics;",
            "GRANT USAGE ON SCHEMA public TO quiz_app, quiz_verify, quiz_metrics;",
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO quiz_app;",
            "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO quiz_app;",
            "GRANT pg_read_all_stats TO quiz_verify;",
            "GRANT pg_monitor TO quiz_metrics;",
        )
    )
    tables = sorted({table for table, _ in APPLICATION_SELECT_COLUMN_ALLOWLIST})
    for table in tables:
        columns = sorted(
            column for target, column in APPLICATION_SELECT_COLUMN_ALLOWLIST if target == table
        )
        statements.append(
            sql.SQL("GRANT SELECT ({}) ON TABLE public.{} TO quiz_verify;")
            .format(
                sql.SQL(", ").join(sql.Identifier(column) for column in columns),
                sql.Identifier(table),
            )
            .as_string()
        )
    return ("\n".join((*statements, "COMMIT;", ""))).encode()


def _json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _env(values: dict[str, str]) -> bytes:
    if any("\n" in value or "\r" in value for value in values.values()):
        raise ValueError("aws_load_test_environment_multiline_forbidden")
    return "".join(f"{key}={value}\n" for key, value in values.items()).encode()


def database_url(host: str, role: str, password: SecretStr) -> str:
    credential = quote(password.get_secret_value(), safe="")
    return f"postgresql+psycopg://{role}:{credential}@{host}:5432/quiz?sslmode=verify-full"


def build_runtime_payload(
    plan: AwsRunPlan,
    manifest: LoadTestManifest,
    settings: DeploymentSettings,
    credentials: RuntimeSecrets,
    postgres_ca: bytes,
    repository: Path,
) -> RuntimePayload:
    """異なるhostへ必要な秘密値だけを配り、private originとsourceを固定する。"""
    context = manifest_context(manifest)
    if (
        context.run_id != plan.run_id
        or context.manifest_sha256 != plan.manifest_sha256
        or context.source_commit != plan.source_commit
        or manifest.schema_revision != plan.schema_revision
        or manifest.public_origin != f"https://{settings.topology.app_private_ip}"
        or sha256(postgres_ca).hexdigest() != settings.postgres_ca_sha256
        or manifest.scheduled_start_at + timedelta(seconds=5_100) >= plan.expires_at
    ):
        raise ValueError("aws_load_test_deployment_binding_invalid")
    root = RUN_BASE / str(plan.run_id)
    tls = create_tls_material(plan, settings.topology.app_private_ip)
    proxy = _json(
        [
            {
                "name": "gs-app-tomidaisai-aws-demo-redis",
                "listen": "172.30.250.2:6379",
                "upstream": f"{settings.topology.redis_private_ip}:6379",
                "enabled": True,
            }
        ]
    )
    runtime = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in (repository / "deploy/aws-demo/runtime.load-test.nonsecret.env.template")
        .read_text()
        .splitlines()
        if line and not line.startswith("#")
    }
    runtime.update(
        {
            "APP_VERSION": plan.source_commit,
            "REALTIME_ALLOWED_ORIGINS": json.dumps([manifest.public_origin], separators=(",", ":")),
            "DATABASE_URL": database_url(
                settings.topology.database_endpoint,
                "quiz_app",
                credentials.application_database_password,
            ),
            "REDIS_URL": (
                "rediss://default:"
                + quote(credentials.redis_application_password.get_secret_value(), safe="")
                + f"@{settings.topology.redis_endpoint}:6379/0"
            ),
            "SESSION_SECRET": credentials.session_secret.get_secret_value(),
            "ROUND_BACKUP_ENCRYPTION_KEY": credentials.backup_encryption_key.get_secret_value(),
        }
    )
    release = {
        "AWS_DEMO_RELEASE_ID": f"{plan.prefix}-candidate",
        "AWS_DEMO_SOURCE_COMMIT": plan.source_commit,
        "AWS_DEMO_API_IMAGE": manifest.artifacts.api_image,
        "AWS_DEMO_GATEWAY_IMAGE": manifest.artifacts.gateway_image,
        "AWS_DEMO_RUNTIME_ENV_FILE": str(root / "runtime/runtime.env"),
        "AWS_DEMO_POSTGRES_CA_FILE": str(root / "ca/postgres.pem"),
        "AWS_DEMO_POSTGRES_CA_SHA256": settings.postgres_ca_sha256,
        "AWS_DEMO_EXECUTION_HOST": settings.topology.app_hostname,
        "AWS_DEMO_DOCKER_CONTEXT": "default",
        "AWS_DEMO_DOCKER_ENDPOINT": "unix:///var/run/docker.sock",
        "AWS_DEMO_RUNTIME_UID": str(RUNTIME_UID),
        "AWS_DEMO_RUNTIME_GID": str(RUNTIME_UID),
        "AWS_DEMO_EVENT_ID": str(manifest.event.id),
        "AWS_DEMO_ROUND_SCOPE": "load-test-ten-round",
        "AWS_DEMO_ROUND_BACKUP_HOST_DIRECTORY": str(root / "backups"),
        "AWS_DEMO_TOXIPROXY_CONFIG_FILE": str(root / "proxy/toxiproxy.json"),
        "AWS_DEMO_TOXIPROXY_CONFIG_SHA256": sha256(proxy).hexdigest(),
        "AWS_DEMO_REDIS_HOSTNAME": settings.topology.redis_endpoint,
        "AWS_DEMO_PROMETHEUS_CONFIG_SHA256": sha256(
            (repository / "infra/prometheus.load-test.yml").read_bytes()
        ).hexdigest(),
        "AWS_DEMO_POSTGRES_EXPORTER_URI": (
            f"{settings.topology.database_endpoint}:5432/quiz?sslmode=verify-full"
            "&sslrootcert=/run/secrets/aws-demo-postgres-ca.pem"
        ),
        "AWS_DEMO_POSTGRES_EXPORTER_USER": "quiz_metrics",
        "AWS_DEMO_POSTGRES_EXPORTER_PASSWORD_FILE": str(root / "exporter-postgres/password"),
        "AWS_DEMO_REDIS_EXPORTER_ADDR": f"rediss://{settings.topology.redis_endpoint}:6379/0",
        "AWS_DEMO_REDIS_EXPORTER_USER": "metrics",
        "AWS_DEMO_REDIS_EXPORTER_PASSWORD_FILE": str(root / "exporter-redis/password.json"),
        "AWS_DEMO_METRICS_ARTIFACT_HOST_DIRECTORY": str(root / "metrics"),
        "AWS_DEMO_LOAD_TEST_MANIFEST_FILE": str(root / "inputs/manifest.json"),
        "AWS_DEMO_SCHEMA_REVISION": plan.schema_revision,
        "AWS_DEMO_GATEWAY_PORT": "18080",
    }
    for key, image in settings.images.model_dump().items():
        release[f"AWS_DEMO_{key.upper()}_IMAGE"] = image
    recovery = {**release, "AWS_DEMO_RELEASE_ID": f"{plan.prefix}-recovery"}
    common = (
        DeploymentFile(path="inputs/manifest.json", body=manifest.model_dump_json().encode()),
        DeploymentFile(path="inputs/application-ca.pem", body=tls.ca_certificate),
    )
    operator = _json(
        {
            "username": run_operator_username(plan.run_id),
            "password": credentials.operator_password.get_secret_value(),
        }
    )
    app = (
        *common,
        DeploymentFile(path="runtime/runtime.env", body=_env(runtime)),
        DeploymentFile(path="release/candidate.env", body=_env(release)),
        DeploymentFile(path="rollback/recovery.env", body=_env(recovery)),
        DeploymentFile(path="ca/postgres.pem", body=postgres_ca),
        DeploymentFile(path="proxy/toxiproxy.json", body=proxy),
        DeploymentFile(
            path="exporter-postgres/password",
            body=credentials.postgres_exporter_password.get_secret_value().encode(),
        ),
        DeploymentFile(
            path="exporter-redis/password.json",
            body=_json(
                {
                    release[
                        "AWS_DEMO_REDIS_EXPORTER_ADDR"
                    ]: credentials.redis_monitor_password.get_secret_value()
                }
            ),
        ),
        DeploymentFile(path="tls/server.pem", body=tls.server_certificate),
        DeploymentFile(path="tls/server-key.pem", body=tls.server_key),
        DeploymentFile(
            path="tls/Caddyfile",
            body=(
                "{\n admin off\n persist_config off\n auto_https off\n}\n"
                + manifest.public_origin
                + " {\n"
                " tls /tls/server.pem /tls/server-key.pem\n reverse_proxy 127.0.0.1:18080\n}\n"
            ).encode(),
        ),
        DeploymentFile(path="inputs/operator.json", body=operator),
        DeploymentFile(
            path="inputs/app-worker.json",
            body=_json(
                {
                    "format": "load-test-app-worker-v1",
                    "run_id": str(plan.run_id),
                    "release_file": str(root / "release/candidate.env"),
                    "rollback_file": str(root / "rollback/recovery.env"),
                }
            ),
        ),
    )
    runner = (
        *common,
        DeploymentFile(path="inputs/operator.json", body=operator),
        DeploymentFile(path="inputs/postgres-ca.pem", body=postgres_ca),
        DeploymentFile(
            path="inputs/verifier-database-url",
            body=database_url(
                settings.topology.database_endpoint,
                "quiz_verify",
                credentials.verifier_database_password,
            ).encode(),
        ),
        DeploymentFile(
            path="inputs/orchestrator.json",
            body=_json(
                {
                    "format": "load-test-orchestrator-v1",
                    "run_id": str(plan.run_id),
                    "postgres_ca_sha256": settings.postgres_ca_sha256,
                }
            ),
        ),
    )
    return RuntimePayload(
        app=app, runner=runner, human_database_sql=human_database_sql(credentials), tls=tls
    )


def archive_files(files: tuple[DeploymentFile, ...]) -> bytes:
    """tarにregular fileだけを入れ、重複・link・任意ownerを排除する。"""
    if len({file.path for file in files}) != len(files):
        raise ValueError("aws_load_test_archive_duplicate_path")
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for file in sorted(files, key=lambda item: item.path):
            body = file.body.get_secret_value()
            entry = tarfile.TarInfo(file.path)
            entry.mode, entry.size, entry.uid, entry.gid = (
                file.mode,
                len(body),
                RUNTIME_UID,
                RUNTIME_UID,
            )
            archive.addfile(entry, io.BytesIO(body))
    if output.tell() > 100 * 1024 * 1024:
        raise ValueError("aws_load_test_deployment_package_too_large")
    return output.getvalue()
