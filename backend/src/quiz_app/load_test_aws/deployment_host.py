"""SSMからrun専用uidで呼ぶ固定activate・verify・seal。schema DDLは実行しない。"""

from __future__ import annotations

import argparse
import base64
import os
import ssl
import subprocess
import tempfile
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

import boto3
import httpx
import logfire
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from botocore.response import StreamingBody
from pydantic import ConfigDict, Field, SecretStr, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from quiz_app.config import Settings
from quiz_app.contracts import LiveHealthResponse, ReadinessHealthResponse
from quiz_app.dependency_hosts import create_pinned_postgres_engine, validate_remote_dependency_host
from quiz_app.load_test.controller_contracts import manifest_context
from quiz_app.load_test.db_verifier_artifacts import read_secure_external_file
from quiz_app.load_test.db_verifier_database import (
    create_verifier_engine,
    load_verifier_database_settings,
    validate_database_security,
)
from quiz_app.load_test.manifest import load_manifest
from quiz_app.load_test.orchestrator_contracts import (
    OperatorCredentials,
    log_failure,
    require_private_directory,
)
from quiz_app.load_test.preparation import RunOperatorCredential, prepare_fresh_load_test_run
from quiz_app.load_test_aws.aws import ApiModel, BotoAwsApi
from quiz_app.load_test_aws.deployment import REPOSITORY, RUN_BASE, RUNTIME_UID
from quiz_app.load_test_aws.deployment_models import HostDeployment, HumanSchemaConfirmation
from quiz_app.load_test_aws.models import AwsPreparationEvidence, SchemaReceipt, StrictModel


class ReadyPayload(StrictModel):
    preparation: AwsPreparationEvidence
    schema_receipt: SchemaReceipt


class EventCount(StrictModel):
    count: Literal[1]
    expected_count: Literal[1]


class AppRoleSecurity(StrictModel):
    safe_role: Literal[True]
    owned_relations: Literal[0]
    owned_schemas: Literal[0]
    owns_database: Literal[False]


class DownloadResponse(ApiModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, arbitrary_types_allowed=True)
    body: StreamingBody = Field(alias="Body")
    length: int = Field(alias="ContentLength", ge=1, le=65536)


class EcrAuthorization(ApiModel):
    token: SecretStr = Field(alias="authorizationToken")
    endpoint: str = Field(alias="proxyEndpoint")


class EcrAuthorizations(ApiModel):
    entries: tuple[EcrAuthorization, ...] = Field(alias="authorizationData")


def download(config: HostDeployment, digest: str, suffix: str) -> bytes:
    """固定run prefixだけを読み、AWS bodyを下流へ渡す前にhashで照合する。"""
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("aws_load_test_host_artifact_digest_invalid")
    client = boto3.Session(region_name="ap-northeast-1").client(
        "s3",
        config=Config(
            connect_timeout=5,
            read_timeout=15,
            retries={"total_max_attempts": 3},
            proxies={},
            ignore_configured_endpoint_urls=True,
        ),
    )
    # 小さいsealed JSONのみ。StreamingBodyはadapter内で閉じ、生SDK値は返さない。
    response = DownloadResponse.model_validate(
        client.get_object(
            Bucket=f"gs-lt-{config.run_id.hex}-{config.account_id}",
            Key=f"deployment/{config.role}/{digest}.{suffix}",
        )
    )
    stream = response.body
    try:
        body = stream.read(65_537)
    finally:
        stream.close()
    if len(body) > 65_536 or sha256(body).hexdigest() != digest:
        raise ValueError("aws_load_test_host_artifact_mismatch")
    return body


def run_command(argv: tuple[str, ...], *, stdin: bytes | None = None, timeout: int = 300) -> None:
    # Docker pull/loginの出力やdriver例外には秘密値が混入し得るので外へ返さない。
    subprocess.run(  # noqa: S603 -- 固定実行ファイルと検証済みdigest/pathのみ。
        argv,
        input=stdin,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,  # noqa: S603 -- 固定実行ファイルと検証済みdigest/pathのみ。
        check=True,
        timeout=timeout,
        cwd=REPOSITORY,
    )


def pull_images(config: HostDeployment, images: tuple[str, ...]) -> None:
    with tempfile.TemporaryDirectory(
        prefix="gs-loadtest-pull-", dir=RUN_BASE / str(config.run_id)
    ) as temporary:
        docker = ("/usr/bin/docker", "--config", temporary)
        registry = f"{config.account_id}.dkr.ecr.ap-northeast-1.amazonaws.com"
        private = tuple(image for image in images if image.startswith(registry + "/"))
        if private:
            authorization = BotoAwsApi().call(
                "ecr", "get_authorization_token", EcrAuthorizations, registryIds=[config.account_id]
            )
            if (
                len(authorization.entries) != 1
                or authorization.entries[0].endpoint != f"https://{registry}"
            ):
                raise ValueError("aws_load_test_ecr_authorization_invalid")
            decoded = base64.b64decode(
                authorization.entries[0].token.get_secret_value(), validate=True
            )
            user, password = decoded.split(b":", 1)
            if user != b"AWS":
                raise ValueError("aws_load_test_ecr_authorization_invalid")
            run_command(
                (*docker, "login", "--username", "AWS", "--password-stdin", registry),
                stdin=password,
            )
        for image in images:
            run_command((*docker, "pull", "--platform", "linux/amd64", image))


def activate(root: Path, config: HostDeployment) -> None:
    if config.role != "app":
        raise ValueError("aws_load_test_app_host_required")
    manifest = load_manifest(root / "inputs/manifest.json", REPOSITORY)
    body, _ = read_secure_external_file(root / "inputs/operator.json")
    operator = OperatorCredentials.model_validate_json(body)
    settings = Settings(
        _env_file=root / "runtime/runtime.env", aws_demo_expected_event_id=manifest.event.id
    )
    # Settingsはcontainer用固定pathを検証し、host側libpqにだけ実ファイルを指定する。
    os.environ["PGSSLROOTCERT"] = str(root / "ca/postgres.pem")
    host = validate_remote_dependency_host("database", settings.database_url)
    engine = create_pinned_postgres_engine(settings.database_url, host)
    try:
        with engine.connect() as connection:
            row = (
                connection.execute(
                    text("""
                SELECT (NOT rolsuper AND NOT rolcreatedb
                    AND NOT rolcreaterole AND NOT rolreplication
                    AND NOT rolbypassrls) AS safe_role,
                    (SELECT count(*) FROM pg_class WHERE relowner = role.oid AND relnamespace IN
                        (SELECT oid FROM pg_namespace WHERE nspname = 'public')) AS owned_relations,
                    (SELECT count(*) FROM pg_namespace WHERE nspname = 'public'
                        AND nspowner = role.oid) AS owned_schemas,
                    (SELECT datdba = role.oid FROM pg_database
                        WHERE datname = current_database()) AS owns_database
                FROM pg_roles AS role WHERE rolname = current_user
            """)
                )
                .mappings()
                .one()
            )
            AppRoleSecurity.model_validate(dict(row))
    finally:
        engine.dispose()
    prepare_fresh_load_test_run(
        settings, manifest, RunOperatorCredential(password=operator.password)
    )
    for name in ("backups", "metrics"):
        (root / name).mkdir(mode=0o700)
    pull_images(
        config,
        (
            manifest.artifacts.api_image,
            manifest.artifacts.gateway_image,
            *config.settings.images.model_dump().values(),
        ),
    )
    compose = (
        "/usr/bin/docker",
        "--config",
        "/etc/gs-loadtest/docker-cli",
        "compose",
        "--env-file",
        str(root / "release/candidate.env"),
        "-f",
        str(REPOSITORY / "deploy/aws-demo/compose.yaml"),
        "-f",
        str(REPOSITORY / "deploy/aws-demo/compose.load-test.yaml"),
    )
    run_command((*compose, "--profile", "preflight", "run", "--rm", "--no-deps", "preflight"))
    run_command(
        (
            *compose,
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            "150",
            "api",
            "api-secondary",
            "gateway",
            "prometheus",
            "postgres-exporter",
            "redis-exporter",
            "node-exporter",
            "toxiproxy",
        )
    )
    run_command(
        (
            "/usr/bin/docker",
            "--config",
            "/etc/gs-loadtest/docker-cli",
            "run",
            "-d",
            "--name",
            f"gs-lt-{config.run_id.hex}-tls",
            "--network",
            "host",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "NET_BIND_SERVICE",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            "1001:1001",
            "--mount",
            f"type=bind,src={root / 'tls'},dst=/tls,readonly",
            "--entrypoint",
            "caddy",
            manifest.artifacts.gateway_image,
            "run",
            "--config",
            "/tls/Caddyfile",
            "--adapter",
            "caddyfile",
        )
    )


def verify(root: Path, config: HostDeployment) -> SchemaReceipt:
    if config.role != "runner":
        raise ValueError("aws_load_test_runner_host_required")
    manifest = load_manifest(root / "inputs/manifest.json", REPOSITORY)
    pull_images(config, (manifest.artifacts.generator_image,))
    tls = ssl.create_default_context(cafile=root / "inputs/application-ca.pem")
    with httpx.Client(
        base_url=manifest.public_origin,
        verify=tls,
        trust_env=False,
        follow_redirects=False,
        timeout=5,
    ) as client:
        live_response, ready_response = (
            client.get("/api/health/live"),
            client.get("/api/health/ready"),
        )
        live_response.raise_for_status()
        ready_response.raise_for_status()
        live = LiveHealthResponse.model_validate_json(live_response.content)
        ready = ReadinessHealthResponse.model_validate_json(ready_response.content)
        if live.version != config.source_commit or ready.status != "ready" or ready.redis != "ok":
            raise ValueError("aws_load_test_runtime_health_invalid")
    settings = load_verifier_database_settings(
        root / "inputs/verifier-database-url",
        root / "inputs/postgres-ca.pem",
        config.settings.postgres_ca_sha256,
        REPOSITORY,
    )
    engine = create_verifier_engine(settings)
    try:
        with (
            engine.connect() as connection,
            connection.execution_options(postgresql_readonly=True).begin(),
        ):
            validate_database_security(
                connection,
                expected_role_name=settings.role_name,
                expected_database_name=settings.database_name,
                expected_schema_revision=manifest.schema_revision,
            )
            row = (
                connection.execute(
                    text(
                        "SELECT count(*) AS count, "
                        "count(*) FILTER (WHERE id = :event_id) AS expected_count "
                        "FROM public.events"
                    ),
                    {"event_id": manifest.event.id},
                )
                .mappings()
                .one()
            )
            EventCount.model_validate(dict(row))
    finally:
        engine.dispose()
    return SchemaReceipt(
        run_id=config.run_id,
        account_id=config.account_id,
        schema_revision=manifest.schema_revision,
        verified_at=datetime.now(UTC),
        schema_applied_by_human=True,
        application_role_is_nonowner=True,
        verifier_role_is_read_only=True,
        single_event_count=1,
    )


def seal(root: Path, config: HostDeployment, payload: ReadyPayload) -> None:
    prepared = payload.preparation
    ca, _ = read_secure_external_file(root / "inputs/application-ca.pem")
    if (
        prepared.run_id != config.run_id
        or prepared.account_id != config.account_id
        or prepared.source_commit != config.source_commit
        or prepared.manifest_sha256 != config.manifest_sha256
        or prepared.ca_sha256 != sha256(ca).hexdigest()
        or prepared.expires_at != config.expires_at
        or prepared.app_private_ip != config.settings.topology.app_private_ip
        or prepared.runner_private_ip != config.settings.topology.runner_private_ip
        or prepared.prepared_at > datetime.now(UTC)
        or prepared.expires_at <= datetime.now(UTC)
        or payload.schema_receipt.schema_revision
        != load_manifest(root / "inputs/manifest.json", REPOSITORY).schema_revision
        or payload.schema_receipt.run_id != config.run_id
        or payload.schema_receipt.account_id != config.account_id
    ):
        raise ValueError("aws_load_test_host_seal_binding_invalid")
    for name, model in (("preparation.json", prepared), ("schema.json", payload.schema_receipt)):
        descriptor = os.open(
            root / "inputs" / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(model.model_dump_json().encode())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-directory", type=Path, required=True)
    parser.add_argument("--action", choices=("activate", "verify", "seal"), required=True)
    parser.add_argument("--artifact-sha256", required=True)
    args = parser.parse_args()
    logfire.configure(send_to_logfire=False, console=False)
    try:
        root = args.run_directory
        require_private_directory(root, REPOSITORY)
        body, _ = read_secure_external_file(root / "inputs/deployment.json")
        config = HostDeployment.model_validate_json(body)
        manifest = load_manifest(root / "inputs/manifest.json", REPOSITORY)
        if (
            root != RUN_BASE / str(config.run_id)
            or os.geteuid() != RUNTIME_UID
            or manifest_context(manifest).manifest_sha256 != config.manifest_sha256
            or manifest.artifacts.source_commit != config.source_commit
            or datetime.now(UTC) >= config.expires_at
        ):
            raise ValueError("aws_load_test_host_context_invalid")
        if args.action == "seal":
            seal(
                root,
                config,
                ReadyPayload.model_validate_json(
                    download(config, args.artifact_sha256, "ready.json")
                ),
            )
            print('{"status":"sealed"}')
        else:
            confirmation = HumanSchemaConfirmation.model_validate_json(
                download(config, args.artifact_sha256, "human.json")
            )
            if (
                confirmation.run_id != config.run_id
                or confirmation.account_id != config.account_id
                or confirmation.schema_revision != manifest.schema_revision
                or not manifest.created_at <= confirmation.confirmed_at <= datetime.now(UTC)
            ):
                raise ValueError("aws_load_test_human_confirmation_invalid")
            if args.action == "activate":
                activate(root, config)
                print('{"status":"activated"}')
            else:
                print(verify(root, config).model_dump_json())
    except (
        ValueError,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
        SQLAlchemyError,
        httpx.HTTPError,
        BotoCoreError,
        ClientError,
        ValidationError,
    ) as error:
        log_failure("aws_load_test_host_deployment_failed", error)
        print('{"status":"refused"}')
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
