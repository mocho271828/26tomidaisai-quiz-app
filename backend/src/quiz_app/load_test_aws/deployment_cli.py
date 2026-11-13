"""build→install→人手schema→activateを分離し、準備証拠なし起動を拒否する。"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from types import FrameType
from typing import Literal
from uuid import UUID

import logfire
from botocore.exceptions import BotoCoreError

from quiz_app.load_test.manifest import load_manifest
from quiz_app.load_test.orchestrator_contracts import log_failure, require_private_directory
from quiz_app.load_test_aws.aws import AwsCallError, BotoAwsApi
from quiz_app.load_test_aws.cli import read_file
from quiz_app.load_test_aws.deployment import build_runtime_payload, new_runtime_secrets
from quiz_app.load_test_aws.deployment_api import (
    SecretResponse,
    deployment_preflight,
    discover_topology,
    host_action,
    install_host,
    upload,
)
from quiz_app.load_test_aws.deployment_host import ReadyPayload
from quiz_app.load_test_aws.deployment_models import (
    DeploymentSettings,
    HostDeployment,
    HumanSchemaConfirmation,
    RuntimeDependencies,
)
from quiz_app.load_test_aws.deployment_package import host_package, repository_files
from quiz_app.load_test_aws.lifecycle import output, request_cleanup
from quiz_app.load_test_aws.models import (
    SHA256,
    AwsPreparationEvidence,
    AwsRunPlan,
    SchemaReceipt,
    StrictModel,
)
from quiz_app.load_test_aws.responses import Stack


class DeploymentReceipt(StrictModel):
    format: Literal["load-test-deployment-package-v1"] = "load-test-deployment-package-v1"
    run_id: UUID
    manifest_sha256: SHA256
    source_commit: str
    app_sha256: SHA256
    runner_sha256: SHA256
    ca_sha256: SHA256
    settings: DeploymentSettings


def write_new(directory: Path, name: str, body: bytes) -> None:
    descriptor = os.open(
        directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())


def require_topology(settings: DeploymentSettings, stack: Stack) -> None:
    for field, key in (
        ("app_private_ip", "AppPrivateIp"),
        ("runner_private_ip", "RunnerPrivateIp"),
        ("database_endpoint", "DatabaseEndpoint"),
        ("redis_endpoint", "RedisEndpoint"),
    ):
        if str(getattr(settings.topology, field)) != output(stack, key):
            raise ValueError("aws_load_test_deployment_topology_mismatch")


def build(
    args: argparse.Namespace, plan: AwsRunPlan, stack: Stack, api: BotoAwsApi, repository: Path
) -> None:
    if any(
        value is None
        for value in (
            args.settings,
            args.manifest,
            args.postgres_ca,
            args.node_archive,
            args.uv_archive,
        )
    ):
        raise ValueError("aws_load_test_build_inputs_required")
    dependencies = RuntimeDependencies.model_validate_json(read_file(args.settings))
    settings = DeploymentSettings(
        **dependencies.model_dump(), topology=discover_topology(api, stack)
    )
    require_topology(settings, stack)
    manifest = load_manifest(args.manifest, repository)
    app_secret = api.call(
        "secretsmanager",
        "get_secret_value",
        SecretResponse,
        SecretId=output(stack, "RedisAppSecretArn"),
    )
    monitor_secret = api.call(
        "secretsmanager",
        "get_secret_value",
        SecretResponse,
        SecretId=output(stack, "RedisMonitorSecretArn"),
    )
    payload = build_runtime_payload(
        plan,
        manifest,
        settings,
        new_runtime_secrets(app_secret.value, monitor_secret.value),
        read_file(args.postgres_ca),
        repository,
    )
    source = repository_files(repository, plan.source_commit)
    packages: dict[str, bytes] = {}
    for role in ("app", "runner"):
        config = HostDeployment(
            run_id=plan.run_id,
            account_id=plan.account_id,
            role=role,
            manifest_sha256=plan.manifest_sha256,
            source_commit=plan.source_commit,
            expires_at=plan.expires_at,
            settings=settings,
        )
        packages[role] = host_package(
            config,
            payload,
            source,
            read_file(args.node_archive, maximum_bytes=100 * 1024 * 1024),
            read_file(args.uv_archive, maximum_bytes=100 * 1024 * 1024),
        )
    receipt = DeploymentReceipt(
        run_id=plan.run_id,
        manifest_sha256=plan.manifest_sha256,
        source_commit=plan.source_commit,
        settings=settings,
        app_sha256=sha256(packages["app"]).hexdigest(),
        runner_sha256=sha256(packages["runner"]).hexdigest(),
        ca_sha256=sha256(payload.tls.ca_certificate.get_secret_value()).hexdigest(),
    )
    for name, body in (
        ("app.tar.gz", packages["app"]),
        ("runner.tar.gz", packages["runner"]),
        ("human-database.sql", payload.human_database_sql.get_secret_value()),
        ("application-ca.pem", payload.tls.ca_certificate.get_secret_value()),
        ("deployment.json", receipt.model_dump_json().encode()),
    ):
        write_new(args.output_directory, name, body)


def activate(
    args: argparse.Namespace,
    plan: AwsRunPlan,
    stack: Stack,
    api: BotoAwsApi,
    receipt: DeploymentReceipt,
) -> None:
    if args.human_confirmation is None:
        raise ValueError("aws_load_test_human_confirmation_required")
    confirmation = HumanSchemaConfirmation.model_validate_json(read_file(args.human_confirmation))
    if (
        confirmation.run_id != plan.run_id
        or confirmation.account_id != plan.account_id
        or confirmation.schema_revision != plan.schema_revision
        or not plan.created_at <= confirmation.confirmed_at <= datetime.now(UTC)
    ):
        raise ValueError("aws_load_test_human_confirmation_mismatch")
    body = confirmation.model_dump_json().encode()
    _, digest = upload(api, plan, "app", body, "human.json")
    host_action(api, plan, stack, "app", "activate", digest)
    _, digest = upload(api, plan, "runner", body, "human.json")
    schema = SchemaReceipt.model_validate_json(
        host_action(api, plan, stack, "runner", "verify", digest)
    )
    if (
        schema.run_id != plan.run_id
        or schema.account_id != plan.account_id
        or schema.schema_revision != plan.schema_revision
    ):
        raise ValueError("aws_load_test_verified_schema_mismatch")
    prepared = AwsPreparationEvidence(
        run_id=plan.run_id,
        account_id=plan.account_id,
        source_commit=plan.source_commit,
        manifest_sha256=plan.manifest_sha256,
        app_instance_id=output(stack, "AppInstanceId"),
        runner_instance_id=output(stack, "RunnerInstanceId"),
        app_private_ip=output(stack, "AppPrivateIp"),
        runner_private_ip=output(stack, "RunnerPrivateIp"),
        public_origin=f"https://{output(stack, 'AppPrivateIp')}",
        ca_sha256=receipt.ca_sha256,
        prepared_at=datetime.now(UTC),
        expires_at=plan.expires_at,
    )
    ready = ReadyPayload(preparation=prepared, schema_receipt=schema).model_dump_json().encode()
    for role in ("app", "runner"):
        _, digest = upload(api, plan, role, ready, "ready.json")
        host_action(api, plan, stack, role, "seal", digest)
    write_new(args.output_directory, "preparation.json", prepared.model_dump_json().encode())
    write_new(args.output_directory, "schema.json", schema.model_dump_json().encode())


def publish(
    args: argparse.Namespace, plan: AwsRunPlan, stack: Stack, api: BotoAwsApi, repository: Path
) -> None:
    """作成前に確定したOCI manifest bytesを、同run ECRへだけ登録する。"""
    from quiz_app.load_test_aws.deployment_images import inspect_oci_archive, publish_image

    if args.manifest is None or args.app_archive is None or args.gateway_archive is None:
        raise ValueError("aws_load_test_image_archives_required")
    manifest = load_manifest(args.manifest, repository)
    for kind, path, reference in (
        ("app", args.app_archive, manifest.artifacts.api_image),
        ("gateway", args.gateway_archive, manifest.artifacts.gateway_image),
    ):
        image = inspect_oci_archive(path, plan.source_commit, kind, reference.rsplit("@", 1)[1])
        published = publish_image(api, plan, stack, image, manifest=manifest)
        write_new(
            args.output_directory, f"published-{kind}.json", published.model_dump_json().encode()
        )


def _interrupted(_number: int, _frame: FrameType | None) -> None:
    raise RuntimeError("aws_load_test_deployment_interrupted")


def main() -> int:
    parser = argparse.ArgumentParser(description="Disposable AWS load-test runtime deployment")
    parser.add_argument("action", choices=("build", "publish", "install", "activate"))
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    parser.add_argument("--profile")
    parser.add_argument("--execute", action="store_true")
    for flag in (
        "settings",
        "manifest",
        "postgres-ca",
        "node-archive",
        "uv-archive",
        "human-confirmation",
        "app-archive",
        "gateway-archive",
    ):
        parser.add_argument(f"--{flag}", type=Path)
    args = parser.parse_args()
    logfire.configure(send_to_logfire=False, console=False)
    previous_term = signal.signal(signal.SIGTERM, _interrupted)
    completed = False
    cleanup_plan: AwsRunPlan | None = None
    cleanup_api: BotoAwsApi | None = None
    try:
        repository = Path(__file__).resolve().parents[4]
        require_private_directory(args.output_directory, repository)
        plan = AwsRunPlan.model_validate_json(read_file(args.plan))
        if args.action != "build" and not args.execute:
            raise ValueError("aws_load_test_execute_flag_required")
        api = BotoAwsApi(profile=args.profile)
        if args.execute and args.action in {"publish", "install", "activate"}:
            cleanup_plan, cleanup_api = plan, api
        stack = deployment_preflight(api, plan, now=datetime.now(UTC))
        if args.action == "build":
            build(args, plan, stack, api, repository)
        elif args.action == "publish":
            publish(args, plan, stack, api, repository)
        else:
            receipt = DeploymentReceipt.model_validate_json(
                read_file(args.output_directory / "deployment.json")
            )
            if (
                receipt.run_id != plan.run_id
                or receipt.manifest_sha256 != plan.manifest_sha256
                or receipt.source_commit != plan.source_commit
            ):
                raise ValueError("aws_load_test_deployment_receipt_mismatch")
            require_topology(receipt.settings, stack)
            if args.action == "install":
                for role in ("app", "runner"):
                    body = read_file(
                        args.output_directory / f"{role}.tar.gz", maximum_bytes=100 * 1024 * 1024
                    )
                    if sha256(body).hexdigest() != getattr(receipt, f"{role}_sha256"):
                        raise ValueError("aws_load_test_deployment_package_mismatch")
                    install_host(api, plan, stack, role, body, repository)
            else:
                activate(args, plan, stack, api, receipt)
        completed = True
        print('{"status":"completed"}')
        return 0
    except (
        AwsCallError,
        BotoCoreError,
        OSError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        log_failure("aws_load_test_deployment_failed", error)
        print('{"status":"refused"}')
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        # 正常終了以外（SIGINTを含む）はAWS側の同じ冪等cleanupへ必ず渡す。
        if not completed and cleanup_plan is not None and cleanup_api is not None:
            try:
                request_cleanup(cleanup_api, cleanup_plan.cleanup_input())
            except (AwsCallError, BotoCoreError, OSError, ValueError) as error:
                log_failure("aws_load_test_deployment_cleanup_request_failed", error)


if __name__ == "__main__":
    raise SystemExit(main())
