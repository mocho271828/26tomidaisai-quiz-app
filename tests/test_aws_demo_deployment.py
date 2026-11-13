import base64
import hashlib
import importlib
import json
import os
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from quiz_app.load_test.manifest import build_load_test_manifest
from quiz_app.schema_revision import SCHEMA_REVISION

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))
aws_demo_deployment_module = importlib.import_module("validation.aws_demo_deployment")
AwsDemoDeploymentValidationError = aws_demo_deployment_module.AwsDemoDeploymentValidationError
validate_aws_demo_deployment = aws_demo_deployment_module.validate_aws_demo_deployment
validate_aws_demo_rollback = aws_demo_deployment_module.validate_aws_demo_rollback
validate_release_manifest_file = aws_demo_deployment_module.validate_release_manifest_file
validate_round_backup_host_directory = (
    aws_demo_deployment_module.validate_round_backup_host_directory
)

RELEASE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
ROLLBACK_COMMIT = "89abcdef0123456789abcdef0123456789abcdef"
API_DIGEST = "0123456789abcdef" * 4
GATEWAY_DIGEST = "123456789abcdef0" * 4
ROLLBACK_API_DIGEST = "23456789abcdef01" * 4
ROLLBACK_GATEWAY_DIGEST = "3456789abcdef012" * 4
PROMETHEUS_DIGEST = "456789abcdef0123" * 4
POSTGRES_EXPORTER_DIGEST = "56789abcdef01234" * 4
REDIS_EXPORTER_DIGEST = "6789abcdef012345" * 4
NODE_EXPORTER_DIGEST = "789abcdef0123456" * 4
TOXIPROXY_DIGEST = "9378ed52a28bc50edc1350f936f518f31fa95f0d15917d6eb40b8e376d1a214e"
LOAD_TEST_RUN_ID = UUID("90000000-0000-4000-8000-000000000001")
OBSERVABILITY_RELEASE_FIELDS = (
    "AWS_DEMO_PROMETHEUS_IMAGE",
    "AWS_DEMO_POSTGRES_EXPORTER_IMAGE",
    "AWS_DEMO_REDIS_EXPORTER_IMAGE",
    "AWS_DEMO_NODE_EXPORTER_IMAGE",
    "AWS_DEMO_PROMETHEUS_CONFIG_SHA256",
    "AWS_DEMO_POSTGRES_EXPORTER_URI",
    "AWS_DEMO_POSTGRES_EXPORTER_USER",
    "AWS_DEMO_POSTGRES_EXPORTER_PASSWORD_FILE",
    "AWS_DEMO_REDIS_EXPORTER_ADDR",
    "AWS_DEMO_REDIS_EXPORTER_USER",
    "AWS_DEMO_REDIS_EXPORTER_PASSWORD_FILE",
    "AWS_DEMO_METRICS_ARTIFACT_HOST_DIRECTORY",
    "AWS_DEMO_LOAD_TEST_MANIFEST_FILE",
    "AWS_DEMO_TOXIPROXY_IMAGE",
    "AWS_DEMO_TOXIPROXY_CONFIG_FILE",
    "AWS_DEMO_TOXIPROXY_CONFIG_SHA256",
    "AWS_DEMO_REDIS_HOSTNAME",
)
DATABASE_CREDENTIAL_FIXTURE = f"Aa0_{secrets.token_urlsafe(32)}"
REDIS_CREDENTIAL_FIXTURE = f"Aa0_{secrets.token_urlsafe(32)}"
SESSION_KEY_FIXTURE = f"Aa0_{secrets.token_urlsafe(32)}"
LOAD_TEST_BACKUP_KEY_FIXTURE = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
PUBLIC_LOCAL_REVIEW_VALUE = "replace-for-local-review-with-at-least-32-characters"
TEST_CA_BUNDLE = (
    b"-----BEGIN CERTIFICATE-----\nPUBLIC AWS DEMO TEST CA\n-----END CERTIFICATE-----\n"
)


def test_event_identity_is_not_exposed_for_gateway_image_inspection() -> None:
    assert "event-slug" not in aws_demo_deployment_module.SAFE_OUTPUT_FIELDS
    assert "event-id" not in aws_demo_deployment_module.SAFE_OUTPUT_FIELDS


def _runtime_values(commit: str) -> dict[str, str]:
    return {
        "APP_ENV": "aws-demo",
        "APP_VERSION": commit,
        "DATABASE_URL": (
            "postgresql+psycopg://aws_demo_user:"
            f"{DATABASE_CREDENTIAL_FIXTURE}@db.aws.internal:5432/quiz_aws_demo?sslmode=verify-full"
        ),
        "REDIS_URL": (
            f"rediss://aws_demo_user:{REDIS_CREDENTIAL_FIXTURE}@redis.aws.internal:6380/0"
        ),
        "DEMO_OPERATOR_USERNAME": "managed-in-database",
        "DEMO_OPERATOR_PASSWORD": "unused",
        "SESSION_SECRET": SESSION_KEY_FIXTURE,
        "ENABLE_DEMO_AUTH": "true",
        "ENABLE_DEMO_SEED": "false",
        "SESSION_COOKIE_SECURE": "true",
        "OPERATOR_SESSION_TTL_MINUTES": "120",
        "PARTICIPANT_SESSION_TTL_MINUTES": "120",
        "PARTICIPANT_JOIN_RETRY_MINUTES": "5",
        "REALTIME_ALLOWED_ORIGINS": '["https://quiz-demo.aws.internal"]',
        "AWS_DEMO_ROUND_SCOPE": "single-round",
        "API_INSTANCE_ID": "api-single",
        "REDIS_CONNECT_TIMEOUT_SECONDS": "0.25",
        "REDIS_OPERATION_TIMEOUT_SECONDS": "0.25",
        "PGSSLROOTCERT": "/run/secrets/aws-demo-postgres-ca.pem",
        "LOGFIRE_SEND_TO_LOGFIRE": "false",
        "ROUND_BACKUP_STORAGE_MODE": "disabled",
        "ROUND_BACKUP_DIRECTORY": "/var/empty/quiz-round-backups",
        "ROUND_BACKUP_ENCRYPTION_KEY": "disabled",
    }


def _release_values(
    *,
    release_id: str,
    commit: str,
    api_digest: str,
    gateway_digest: str,
    runtime_env_file: Path,
) -> dict[str, str]:
    postgres_ca_file = runtime_env_file.parent / "postgres-ca.pem"
    return {
        "AWS_DEMO_RELEASE_ID": release_id,
        "AWS_DEMO_SOURCE_COMMIT": commit,
        "AWS_DEMO_API_IMAGE": f"registry.internal/quiz/api@sha256:{api_digest}",
        "AWS_DEMO_GATEWAY_IMAGE": (f"registry.internal/quiz/gateway@sha256:{gateway_digest}"),
        "AWS_DEMO_RUNTIME_ENV_FILE": str(runtime_env_file),
        "AWS_DEMO_POSTGRES_CA_FILE": str(postgres_ca_file),
        "AWS_DEMO_POSTGRES_CA_SHA256": hashlib.sha256(postgres_ca_file.read_bytes()).hexdigest(),
        "AWS_DEMO_EXECUTION_HOST": "ip-10-0-1-25.aws.internal",
        "AWS_DEMO_DOCKER_CONTEXT": "aws-demo-local",
        "AWS_DEMO_DOCKER_ENDPOINT": "unix:///var/run/docker.sock",
        "AWS_DEMO_RUNTIME_UID": str(os.geteuid()),
        "AWS_DEMO_RUNTIME_GID": str(os.getegid()),
        "AWS_DEMO_EVENT_ID": "70000000-0000-4000-8000-000000000001",
        "AWS_DEMO_ROUND_SCOPE": "single-round",
        "AWS_DEMO_SCHEMA_REVISION": SCHEMA_REVISION,
        "AWS_DEMO_GATEWAY_PORT": "18080",
    }


def _write_env(path: Path, values: dict[str, str], mode: int = 0o600) -> None:
    path.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n",
        encoding="utf-8",
    )
    path.chmod(mode)


def _deployment_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    config_directory = tmp_path / "deployment-config"
    config_directory.mkdir(mode=0o700)
    config_directory.chmod(0o700)
    postgres_ca_file = config_directory / "postgres-ca.pem"
    postgres_ca_file.write_bytes(TEST_CA_BUNDLE)
    postgres_ca_file.chmod(0o644)

    release_runtime = config_directory / "release-runtime.env"
    rollback_runtime = config_directory / "rollback-runtime.env"
    _write_env(release_runtime, _runtime_values(RELEASE_COMMIT))
    _write_env(rollback_runtime, _runtime_values(ROLLBACK_COMMIT))

    release_file = config_directory / "release.env"
    rollback_file = config_directory / "rollback.env"
    _write_env(
        release_file,
        _release_values(
            release_id="aws-demo-release-002",
            commit=RELEASE_COMMIT,
            api_digest=API_DIGEST,
            gateway_digest=GATEWAY_DIGEST,
            runtime_env_file=release_runtime,
        ),
        mode=0o644,
    )
    _write_env(
        rollback_file,
        _release_values(
            release_id="aws-demo-release-001",
            commit=ROLLBACK_COMMIT,
            api_digest=ROLLBACK_API_DIGEST,
            gateway_digest=ROLLBACK_GATEWAY_DIGEST,
            runtime_env_file=rollback_runtime,
        ),
        mode=0o644,
    )
    return repository_root, release_file, rollback_file


def _load_test_deployment_files(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    repository_root, release_file, rollback_file = _deployment_files(tmp_path)
    config_directory = tmp_path / "deployment-config"
    backup_directory = config_directory / "round-backups"
    backup_directory.mkdir(mode=0o700)
    backup_directory.chmod(0o700)
    artifact_directory = config_directory / "metrics-artifacts"
    artifact_directory.mkdir(mode=0o700)
    artifact_directory.chmod(0o700)

    prometheus_config = repository_root / "infra" / "prometheus.load-test.yml"
    prometheus_config.parent.mkdir()
    prometheus_config.write_bytes(
        (REPOSITORY_ROOT / "infra" / "prometheus.load-test.yml").read_bytes()
    )
    prometheus_config_digest = hashlib.sha256(prometheus_config.read_bytes()).hexdigest()
    postgres_exporter_password = config_directory / "postgres-exporter-password"
    postgres_exporter_password.write_text(f"Aa0_{secrets.token_urlsafe(32)}", encoding="ascii")
    postgres_exporter_password.chmod(0o600)
    redis_exporter_addr = "rediss://redis.aws.internal:6380/0"
    redis_exporter_password = config_directory / "redis-exporter-password.json"
    redis_exporter_password.write_text(
        json.dumps(
            {
                "rediss://metrics_reader@redis.aws.internal:6380/0": (
                    f"Aa0_{secrets.token_urlsafe(32)}"
                )
            }
        ),
        encoding="ascii",
    )
    redis_exporter_password.chmod(0o600)
    load_test_manifest_file = config_directory / "load-test-manifest.json"
    load_test_manifest = build_load_test_manifest(
        run_id=LOAD_TEST_RUN_ID,
        event_id=UUID("70000000-0000-4000-8000-000000000001"),
        event_slug="aws-demo-2026",
        scheduled_start_at=datetime(2026, 9, 8, 10, 0, tzinfo=UTC),
        public_origin="https://quiz-demo.aws.internal",
        source_commit=RELEASE_COMMIT,
        api_image=f"registry.internal/quiz/api@sha256:{API_DIGEST}",
        gateway_image=f"registry.internal/quiz/gateway@sha256:{GATEWAY_DIGEST}",
        generator_image=(f"registry.internal/quiz/generator@sha256:{'89abcdef01234567' * 4}"),
        scoring_policy_version="scoring-v1",
        scoring_base_points=10_000,
        scoring_initial_speed_bonus=500,
        scoring_speed_bucket_ms=100,
        scoring_speed_decrement_per_bucket=10,
        scoring_standard_duration_ms=10_000,
        scoring_is_approved=True,
        display_name_policy_version="display-v1",
        display_notice_version="notice-v1",
        display_max_code_points=20,
        display_reject_patterns_json="[]",
        created_at=datetime(2026, 9, 3, 0, 0, tzinfo=UTC),
    )
    load_test_manifest_file.write_text(
        load_test_manifest.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    load_test_manifest_file.chmod(0o600)
    toxiproxy_config_file = config_directory / "toxiproxy.json"
    toxiproxy_config_file.write_text(
        json.dumps(
            [
                {
                    "name": "gs-app-tomidaisai-aws-demo-redis",
                    "listen": "172.30.250.2:6380",
                    "upstream": "10.0.2.15:6380",
                    "enabled": True,
                }
            ]
        )
        + "\n",
        encoding="ascii",
    )
    toxiproxy_config_file.chmod(0o600)
    toxiproxy_config_digest = hashlib.sha256(toxiproxy_config_file.read_bytes()).hexdigest()

    for release_path, commit in (
        (release_file, RELEASE_COMMIT),
        (rollback_file, ROLLBACK_COMMIT),
    ):
        release_values = _release_values_from_file(release_path)
        release_values["AWS_DEMO_ROUND_SCOPE"] = "load-test-ten-round"
        release_values["AWS_DEMO_ROUND_BACKUP_HOST_DIRECTORY"] = str(backup_directory)
        release_values.update(
            {
                "AWS_DEMO_PROMETHEUS_IMAGE": (
                    f"quay.io/prometheus/prometheus@sha256:{PROMETHEUS_DIGEST}"
                ),
                "AWS_DEMO_POSTGRES_EXPORTER_IMAGE": (
                    "quay.io/prometheuscommunity/postgres-exporter@sha256:"
                    f"{POSTGRES_EXPORTER_DIGEST}"
                ),
                "AWS_DEMO_REDIS_EXPORTER_IMAGE": (
                    f"ghcr.io/oliver006/redis_exporter@sha256:{REDIS_EXPORTER_DIGEST}"
                ),
                "AWS_DEMO_NODE_EXPORTER_IMAGE": (
                    f"quay.io/prometheus/node-exporter@sha256:{NODE_EXPORTER_DIGEST}"
                ),
                "AWS_DEMO_PROMETHEUS_CONFIG_SHA256": prometheus_config_digest,
                "AWS_DEMO_POSTGRES_EXPORTER_URI": (
                    "db.aws.internal:5432/quiz_aws_demo?sslmode=verify-full"
                    "&sslrootcert=/run/secrets/aws-demo-postgres-ca.pem"
                ),
                "AWS_DEMO_POSTGRES_EXPORTER_USER": "metrics_reader",
                "AWS_DEMO_POSTGRES_EXPORTER_PASSWORD_FILE": str(postgres_exporter_password),
                "AWS_DEMO_REDIS_EXPORTER_ADDR": redis_exporter_addr,
                "AWS_DEMO_REDIS_EXPORTER_USER": "metrics_reader",
                "AWS_DEMO_REDIS_EXPORTER_PASSWORD_FILE": str(redis_exporter_password),
                "AWS_DEMO_METRICS_ARTIFACT_HOST_DIRECTORY": str(artifact_directory),
                "AWS_DEMO_LOAD_TEST_MANIFEST_FILE": str(load_test_manifest_file),
                "AWS_DEMO_TOXIPROXY_IMAGE": (
                    f"ghcr.io/shopify/toxiproxy@sha256:{TOXIPROXY_DIGEST}"
                ),
                "AWS_DEMO_TOXIPROXY_CONFIG_FILE": str(toxiproxy_config_file),
                "AWS_DEMO_TOXIPROXY_CONFIG_SHA256": toxiproxy_config_digest,
                "AWS_DEMO_REDIS_HOSTNAME": "redis.aws.internal",
            }
        )
        _write_env(release_path, release_values, mode=0o644)

        runtime_path = Path(release_values["AWS_DEMO_RUNTIME_ENV_FILE"])
        runtime_values = _runtime_values(commit)
        runtime_values.update(
            {
                "AWS_DEMO_ROUND_SCOPE": "load-test-ten-round",
                "API_INSTANCE_ID": "api-primary",
                "ROUND_BACKUP_STORAGE_MODE": "filesystem",
                "ROUND_BACKUP_DIRECTORY": "/var/lib/quiz-round-backups",
                "ROUND_BACKUP_ENCRYPTION_KEY": LOAD_TEST_BACKUP_KEY_FIXTURE,
            }
        )
        _write_env(runtime_path, runtime_values)

    return repository_root, release_file, rollback_file, backup_directory


def test_valid_deployment_requires_distinct_digest_pinned_rollback(
    tmp_path: Path,
) -> None:
    repository_root, release_file, rollback_file = _deployment_files(tmp_path)

    deployment = validate_aws_demo_deployment(
        release_file,
        rollback_file,
        repository_root,
    )

    assert deployment.release.source_commit == RELEASE_COMMIT
    assert deployment.rollback.source_commit == ROLLBACK_COMMIT
    assert deployment.release_settings.app_version == RELEASE_COMMIT


def test_valid_load_test_deployment_requires_shared_backup_identity(
    tmp_path: Path,
) -> None:
    repository_root, release_file, rollback_file, backup_directory = _load_test_deployment_files(
        tmp_path
    )

    deployment = validate_aws_demo_deployment(release_file, rollback_file, repository_root)

    assert deployment.release.round_scope == "load-test-ten-round"
    assert deployment.release.round_backup_host_directory == backup_directory
    assert deployment.release.runtime_uid == os.geteuid()
    assert deployment.release.runtime_gid == os.getegid()
    assert deployment.release_settings.round_backup_storage_mode == "filesystem"
    assert deployment.release.redis_hostname == "redis.aws.internal"
    assert deployment.release.toxiproxy_config_file is not None


def _rehearsal_deployment_files(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    repository_root, release_file, rollback_file = _deployment_files(tmp_path)
    backup_directory = tmp_path / "rehearsal-backups"
    backup_directory.mkdir(mode=0o700)
    for path in (release_file, rollback_file):
        values = _release_values_from_file(path)
        values["AWS_DEMO_ROUND_SCOPE"] = "rehearsal-six-round"
        values["AWS_DEMO_ROUND_BACKUP_HOST_DIRECTORY"] = str(backup_directory)
        _write_env(path, values, mode=0o644)
        runtime_path = Path(values["AWS_DEMO_RUNTIME_ENV_FILE"])
        runtime = _runtime_values(values["AWS_DEMO_SOURCE_COMMIT"])
        runtime.update(
            AWS_DEMO_ROUND_SCOPE="rehearsal-six-round",
            API_INSTANCE_ID="api-rehearsal",
            ROUND_BACKUP_STORAGE_MODE="filesystem",
            ROUND_BACKUP_DIRECTORY="/var/lib/quiz-round-backups",
            ROUND_BACKUP_ENCRYPTION_KEY=LOAD_TEST_BACKUP_KEY_FIXTURE,
        )
        _write_env(runtime_path, runtime)
    return repository_root, release_file, rollback_file, backup_directory


def test_rehearsal_uses_backup_without_load_test_artifacts(tmp_path: Path) -> None:
    repository_root, release_file, rollback_file, backup_directory = _rehearsal_deployment_files(
        tmp_path
    )
    deployment = validate_aws_demo_deployment(release_file, rollback_file, repository_root)

    assert deployment.release.round_scope == "rehearsal-six-round"
    assert deployment.release.round_backup_host_directory == backup_directory
    assert deployment.release.load_test_manifest_file is None
    assert deployment.release.toxiproxy_config_file is None
    assert deployment.release_settings.round_backup_storage_mode == "filesystem"
    rollback = validate_aws_demo_rollback(rollback_file, release_file, repository_root)
    assert rollback.release.round_scope == "rehearsal-six-round"


@pytest.mark.parametrize("field", OBSERVABILITY_RELEASE_FIELDS)
def test_rehearsal_rejects_load_test_configuration(tmp_path: Path, field: str) -> None:
    repository_root, release_file, rollback_file, _ = _rehearsal_deployment_files(tmp_path)
    load_directory = tmp_path / "load"
    load_directory.mkdir()
    _, load_release_file, _, _ = _load_test_deployment_files(load_directory)
    values = _release_values_from_file(release_file)
    values[field] = _release_values_from_file(load_release_file)[field]
    _write_env(release_file, values, mode=0o644)

    with pytest.raises(AwsDemoDeploymentValidationError, match="release_manifest_invalid"):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


def test_rehearsal_requires_backup_directory(tmp_path: Path) -> None:
    repository_root, release_file, rollback_file, _ = _rehearsal_deployment_files(tmp_path)
    values = _release_values_from_file(release_file)
    values.pop("AWS_DEMO_ROUND_BACKUP_HOST_DIRECTORY")
    _write_env(release_file, values, mode=0o644)

    with pytest.raises(AwsDemoDeploymentValidationError, match="release_manifest_invalid"):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


@pytest.mark.parametrize("mode", [0o755, 0o770, 0o777])
def test_rehearsal_rejects_shared_backup_directory(tmp_path: Path, mode: int) -> None:
    repository_root, release_file, rollback_file, backup = _rehearsal_deployment_files(tmp_path)
    backup.chmod(mode)

    with pytest.raises(AwsDemoDeploymentValidationError):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


def test_rehearsal_rejects_symlink_backup_directory(tmp_path: Path) -> None:
    repository_root, release_file, rollback_file, backup = _rehearsal_deployment_files(tmp_path)
    link = tmp_path / "backup-link"
    link.symlink_to(backup, target_is_directory=True)
    for path in (release_file, rollback_file):
        values = _release_values_from_file(path)
        values["AWS_DEMO_ROUND_BACKUP_HOST_DIRECTORY"] = str(link)
        _write_env(path, values, mode=0o644)

    with pytest.raises(AwsDemoDeploymentValidationError):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


def test_rehearsal_requires_distinct_rollback_images(tmp_path: Path) -> None:
    repository_root, release_file, rollback_file, _ = _rehearsal_deployment_files(tmp_path)
    values = _release_values_from_file(rollback_file)
    candidate = _release_values_from_file(release_file)
    for field in ("AWS_DEMO_API_IMAGE", "AWS_DEMO_GATEWAY_IMAGE"):
        values[field] = candidate[field]
    _write_env(rollback_file, values, mode=0o644)

    with pytest.raises(AwsDemoDeploymentValidationError, match="rollback_images_must_be_distinct"):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


def test_rehearsal_rollback_requires_same_backup_key(tmp_path: Path) -> None:
    repository_root, release_file, rollback_file, _ = _rehearsal_deployment_files(tmp_path)
    runtime_path = Path(_release_values_from_file(rollback_file)["AWS_DEMO_RUNTIME_ENV_FILE"])
    values = _release_values_from_file(runtime_path)
    values["ROUND_BACKUP_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(
        secrets.token_bytes(32)
    ).decode("ascii")
    _write_env(runtime_path, values)

    with pytest.raises(AwsDemoDeploymentValidationError) as error:
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)
    assert error.value.code == "rollback_runtime_identity_mismatch"
    assert error.value.fields == ("ROUND_BACKUP_ENCRYPTION_KEY",)


@pytest.mark.parametrize(
    "field",
    [
        "AWS_DEMO_TOXIPROXY_IMAGE",
        "AWS_DEMO_TOXIPROXY_CONFIG_FILE",
        "AWS_DEMO_TOXIPROXY_CONFIG_SHA256",
        "AWS_DEMO_REDIS_HOSTNAME",
    ],
)
def test_load_test_release_requires_complete_fault_topology(
    tmp_path: Path,
    field: str,
) -> None:
    repository_root, release_file, rollback_file, _ = _load_test_deployment_files(tmp_path)
    release_values = _release_values_from_file(release_file)
    release_values.pop(field)
    _write_env(release_file, release_values, mode=0o644)

    with pytest.raises(AwsDemoDeploymentValidationError) as error:
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)

    assert error.value.code == "release_manifest_invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (
            "AWS_DEMO_TOXIPROXY_IMAGE",
            (
                "ghcr.io/shopify/toxiproxy@sha256:"
                "9378ed52a28bc50edc1350f936f518f31fa95f0d15917d6eb40b8e376d1a214e"
            ),
        ),
        ("AWS_DEMO_TOXIPROXY_CONFIG_FILE", "/private/tmp/toxiproxy.json"),
        ("AWS_DEMO_TOXIPROXY_CONFIG_SHA256", "a1" * 32),
        ("AWS_DEMO_REDIS_HOSTNAME", "redis.aws.internal"),
    ],
)
def test_single_round_release_rejects_fault_topology_fields(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    repository_root, release_file, rollback_file = _deployment_files(tmp_path)
    release_values = _release_values_from_file(release_file)
    release_values[field] = value
    _write_env(release_file, release_values, mode=0o644)

    with pytest.raises(AwsDemoDeploymentValidationError) as error:
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)

    assert error.value.code == "release_manifest_invalid"


def test_load_manifest_must_match_candidate_release_and_origin(tmp_path: Path) -> None:
    repository_root, release_file, rollback_file, _ = _load_test_deployment_files(tmp_path)
    release_values = _release_values_from_file(release_file)
    manifest_path = Path(release_values["AWS_DEMO_LOAD_TEST_MANIFEST_FILE"])
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["artifacts"]["source_commit"] = "f" * 40
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    manifest_path.chmod(0o600)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="load_test_manifest_release_identity_mismatch",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)

    payload["artifacts"]["source_commit"] = RELEASE_COMMIT
    payload["public_origin"] = "https://different.aws.internal"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    manifest_path.chmod(0o600)
    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="load_test_manifest_public_origin_mismatch",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


@pytest.mark.parametrize(
    "field",
    [
        "AWS_DEMO_PROMETHEUS_IMAGE",
        "AWS_DEMO_POSTGRES_EXPORTER_IMAGE",
        "AWS_DEMO_REDIS_EXPORTER_IMAGE",
        "AWS_DEMO_NODE_EXPORTER_IMAGE",
        "AWS_DEMO_TOXIPROXY_IMAGE",
    ],
)
def test_monitor_images_require_approved_official_repository(
    tmp_path: Path,
    field: str,
) -> None:
    repository_root, release_file, rollback_file, _ = _load_test_deployment_files(tmp_path)
    release_values = _release_values_from_file(release_file)
    release_values[field] = f"registry.invalid/unapproved@sha256:{PROMETHEUS_DIGEST}"
    _write_env(release_file, release_values, mode=0o644)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="release_manifest_invalid",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


def test_toxiproxy_image_requires_approved_v2_12_digest(tmp_path: Path) -> None:
    repository_root, release_file, rollback_file, _ = _load_test_deployment_files(tmp_path)
    release_values = _release_values_from_file(release_file)
    release_values["AWS_DEMO_TOXIPROXY_IMAGE"] = (
        f"ghcr.io/shopify/toxiproxy@sha256:{'89abcdef01234567' * 4}"
    )
    _write_env(release_file, release_values, mode=0o644)

    with pytest.raises(AwsDemoDeploymentValidationError) as error:
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)

    assert error.value.code == "release_manifest_invalid"
    assert error.value.fields == ("AWS_DEMO_TOXIPROXY_IMAGE",)


def test_fault_topology_config_must_match_runtime_redis_identity(
    tmp_path: Path,
) -> None:
    repository_root, release_file, rollback_file, _ = _load_test_deployment_files(tmp_path)
    for manifest_path in (release_file, rollback_file):
        values = _release_values_from_file(manifest_path)
        values["AWS_DEMO_REDIS_HOSTNAME"] = "different-redis.aws.internal"
        _write_env(manifest_path, values, mode=0o644)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="toxiproxy_redis_hostname_mismatch",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


def test_fault_topology_config_checksum_is_release_bound(tmp_path: Path) -> None:
    repository_root, release_file, rollback_file, _ = _load_test_deployment_files(tmp_path)
    for manifest_path in (release_file, rollback_file):
        values = _release_values_from_file(manifest_path)
        values["AWS_DEMO_TOXIPROXY_CONFIG_SHA256"] = "a1" * 32
        _write_env(manifest_path, values, mode=0o644)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="toxiproxy_config_checksum_mismatch",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


def test_load_test_rollback_must_keep_fault_topology_identity(tmp_path: Path) -> None:
    repository_root, release_file, rollback_file, _ = _load_test_deployment_files(tmp_path)
    rollback_values = _release_values_from_file(rollback_file)
    rollback_values["AWS_DEMO_REDIS_HOSTNAME"] = "redis-alt.aws.internal"
    _write_env(rollback_file, rollback_values, mode=0o644)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="rollback_fault_topology_identity_mismatch",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


def test_exporter_uri_and_password_mapping_are_fail_closed(tmp_path: Path) -> None:
    repository_root, release_file, rollback_file, _ = _load_test_deployment_files(tmp_path)
    for manifest_path in (release_file, rollback_file):
        values = _release_values_from_file(manifest_path)
        values["AWS_DEMO_POSTGRES_EXPORTER_URI"] += "&junk="
        _write_env(manifest_path, values, mode=0o644)
    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="postgres_exporter_uri_invalid",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)

    redis_case = tmp_path / "redis-case"
    redis_case.mkdir()
    repository_root, release_file, rollback_file, _ = _load_test_deployment_files(redis_case)
    values = _release_values_from_file(release_file)
    redis_password_file = Path(values["AWS_DEMO_REDIS_EXPORTER_PASSWORD_FILE"])
    redis_password_file.write_text(
        json.dumps({values["AWS_DEMO_REDIS_EXPORTER_ADDR"]: "Aa0_valid-secret-value"}),
        encoding="ascii",
    )
    redis_password_file.chmod(0o600)
    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="redis_exporter_password_content_invalid",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


@pytest.mark.parametrize(
    ("application_user", "expected_error"),
    [
        ("metrics%5Freader", "redis_exporter_user_not_dedicated"),
        ("invalid%FFuser", "redis_application_user_invalid"),
        ("invalid%user", "redis_application_user_invalid"),
    ],
)
def test_redis_exporter_user_comparison_decodes_application_identity(
    tmp_path: Path,
    application_user: str,
    expected_error: str,
) -> None:
    repository_root, release_file, rollback_file, _ = _load_test_deployment_files(tmp_path)
    for manifest_path in (release_file, rollback_file):
        release_values = _release_values_from_file(manifest_path)
        runtime_path = Path(release_values["AWS_DEMO_RUNTIME_ENV_FILE"])
        runtime_values = _runtime_values(release_values["AWS_DEMO_SOURCE_COMMIT"])
        runtime_values.update(
            {
                "REDIS_URL": (
                    f"rediss://{application_user}:{REDIS_CREDENTIAL_FIXTURE}"
                    "@redis.aws.internal:6380/0"
                ),
                "AWS_DEMO_ROUND_SCOPE": "load-test-ten-round",
                "API_INSTANCE_ID": "api-primary",
                "ROUND_BACKUP_STORAGE_MODE": "filesystem",
                "ROUND_BACKUP_DIRECTORY": "/var/lib/quiz-round-backups",
                "ROUND_BACKUP_ENCRYPTION_KEY": LOAD_TEST_BACKUP_KEY_FIXTURE,
            }
        )
        _write_env(runtime_path, runtime_values)

    with pytest.raises(AwsDemoDeploymentValidationError, match=expected_error):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


def test_metrics_artifact_directory_must_be_empty_at_preflight(tmp_path: Path) -> None:
    repository_root, release_file, rollback_file, _ = _load_test_deployment_files(tmp_path)
    release_values = _release_values_from_file(release_file)
    artifact_directory = Path(release_values["AWS_DEMO_METRICS_ARTIFACT_HOST_DIRECTORY"])
    unrelated = artifact_directory / "secret.txt"
    unrelated.write_text("must not be exposed", encoding="utf-8")
    unrelated.chmod(0o600)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="metrics_artifact_directory_not_empty",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


def test_runtime_environment_must_be_outside_repository(tmp_path: Path) -> None:
    repository_root, release_file, rollback_file = _deployment_files(tmp_path)
    inside_runtime = repository_root / "aws-demo-runtime.env"
    _write_env(inside_runtime, _runtime_values(RELEASE_COMMIT))
    release_values = _release_values_from_file(release_file)
    release_values["AWS_DEMO_RUNTIME_ENV_FILE"] = str(inside_runtime)
    _write_env(release_file, release_values, mode=0o644)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="runtime_environment_inside_repository",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


@pytest.mark.parametrize("mode", [0o666, 0o664, 0o646])
def test_release_manifest_rejects_shared_write_permissions(
    tmp_path: Path,
    mode: int,
) -> None:
    repository_root, release_file, rollback_file = _deployment_files(tmp_path)
    release_file.chmod(mode)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="release_manifest_permissions_writable",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


def test_release_manifest_rejects_shared_write_parent_directory(tmp_path: Path) -> None:
    repository_root, release_file, rollback_file = _deployment_files(tmp_path)
    release_file.parent.chmod(0o775)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="release_manifest_parent_permissions_writable",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


def test_release_manifest_requires_execution_user_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_root, release_file, rollback_file = _deployment_files(tmp_path)
    monkeypatch.setattr(
        "validation.aws_demo_deployment.os.geteuid",
        lambda: release_file.stat().st_uid + 1,
    )

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="release_manifest_owner_mismatch",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


@pytest.mark.parametrize("mode", [0o400, 0o640, 0o700])
def test_runtime_environment_requires_exact_0600_permission(
    tmp_path: Path,
    mode: int,
) -> None:
    repository_root, release_file, rollback_file = _deployment_files(tmp_path)
    release_runtime = Path(_release_values_from_file(release_file)["AWS_DEMO_RUNTIME_ENV_FILE"])
    release_runtime.chmod(mode)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="runtime_environment_permissions_not_0600",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


def test_runtime_environment_rejects_unknown_or_duplicate_field(tmp_path: Path) -> None:
    repository_root, release_file, rollback_file = _deployment_files(tmp_path)
    release_runtime = Path(_release_values_from_file(release_file)["AWS_DEMO_RUNTIME_ENV_FILE"])
    with release_runtime.open("a", encoding="utf-8") as file:
        file.write("APP_ENV=aws-demo\n")

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="environment_file_syntax_invalid",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


@pytest.mark.parametrize(
    "ambiguous_value",
    [
        "${AMBIENT_SESSION_SECRET_WITH_SUFFICIENT_LENGTH}",
        '"quoted-session-secret-with-sufficient-length"',
        "session-secret-with-internal whitespace",
        "session-secret-with-comment#suffix-value",
        "session-secret-with-backslash\\suffix-value",
        "セッション秘密値は十分に長いがASCIIではない",
    ],
)
def test_runtime_environment_rejects_compose_interpreted_values(
    tmp_path: Path,
    ambiguous_value: str,
) -> None:
    repository_root, release_file, rollback_file = _deployment_files(tmp_path)
    release_runtime = Path(_release_values_from_file(release_file)["AWS_DEMO_RUNTIME_ENV_FILE"])
    values = _runtime_values(RELEASE_COMMIT)
    values["SESSION_SECRET"] = ambiguous_value
    _write_env(release_runtime, values)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="environment_file_syntax_invalid",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


def test_runtime_environment_rejects_public_placeholder_without_echoing_secret(
    tmp_path: Path,
) -> None:
    repository_root, release_file, rollback_file = _deployment_files(tmp_path)
    release_runtime = Path(_release_values_from_file(release_file)["AWS_DEMO_RUNTIME_ENV_FILE"])
    values = _runtime_values(RELEASE_COMMIT)
    values["SESSION_SECRET"] = PUBLIC_LOCAL_REVIEW_VALUE
    _write_env(release_runtime, values)

    with pytest.raises(AwsDemoDeploymentValidationError) as error:
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)

    assert error.value.code == "runtime_environment_contains_placeholder"
    assert error.value.fields == ("SESSION_SECRET",)
    assert "replace-for-local-review" not in str(error.value)


def test_release_rejects_tag_only_image(tmp_path: Path) -> None:
    repository_root, release_file, rollback_file = _deployment_files(tmp_path)
    values = _release_values_from_file(release_file)
    values["AWS_DEMO_API_IMAGE"] = "registry.internal/quiz/api:latest"
    _write_env(release_file, values, mode=0o644)

    with pytest.raises(AwsDemoDeploymentValidationError) as error:
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)

    assert error.value.code == "release_manifest_invalid"
    assert error.value.fields == ("AWS_DEMO_API_IMAGE",)


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("AWS_DEMO_EXECUTION_HOST", "localhost"),
        ("AWS_DEMO_EXECUTION_HOST", "demo.example.com"),
        ("AWS_DEMO_DOCKER_ENDPOINT", "ssh://demo@unapproved.internal"),
        ("AWS_DEMO_DOCKER_ENDPOINT", "tcp://10.0.0.25:2376"),
    ],
)
def test_release_rejects_unapproved_execution_target(
    tmp_path: Path,
    field: str,
    unsafe_value: str,
) -> None:
    repository_root, release_file, rollback_file = _deployment_files(tmp_path)
    values = _release_values_from_file(release_file)
    values[field] = unsafe_value
    _write_env(release_file, values, mode=0o644)

    with pytest.raises(AwsDemoDeploymentValidationError) as error:
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)

    assert error.value.code == "release_manifest_invalid"
    assert error.value.fields == (field,)


def test_release_rejects_local_demo_event_id(tmp_path: Path) -> None:
    repository_root, release_file, rollback_file = _deployment_files(tmp_path)
    values = _release_values_from_file(release_file)
    values["AWS_DEMO_EVENT_ID"] = "60000000-0000-4000-8000-000000000001"
    _write_env(release_file, values, mode=0o644)

    with pytest.raises(AwsDemoDeploymentValidationError) as error:
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)

    assert error.value.code == "release_manifest_invalid"
    assert error.value.fields == ("AWS_DEMO_EVENT_ID",)


def test_release_rejects_unapproved_postgres_ca_checksum(tmp_path: Path) -> None:
    repository_root, release_file, rollback_file = _deployment_files(tmp_path)
    values = _release_values_from_file(release_file)
    invalid_digest = "0123456789abcdef" * 4
    values["AWS_DEMO_POSTGRES_CA_SHA256"] = invalid_digest
    _write_env(release_file, values, mode=0o644)
    rollback_values = _release_values_from_file(rollback_file)
    rollback_values["AWS_DEMO_POSTGRES_CA_SHA256"] = invalid_digest
    _write_env(rollback_file, rollback_values, mode=0o644)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="postgres_ca_checksum_mismatch",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


def test_release_commit_must_match_runtime_app_version(tmp_path: Path) -> None:
    repository_root, release_file, rollback_file = _deployment_files(tmp_path)
    release_runtime = Path(_release_values_from_file(release_file)["AWS_DEMO_RUNTIME_ENV_FILE"])
    _write_env(release_runtime, _runtime_values(ROLLBACK_COMMIT))

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="runtime_release_commit_mismatch",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


def test_rollback_must_keep_event_identity(tmp_path: Path) -> None:
    repository_root, release_file, rollback_file = _deployment_files(tmp_path)
    rollback_values = _release_values_from_file(rollback_file)
    rollback_values["AWS_DEMO_EVENT_ID"] = "71000000-0000-4000-8000-000000000001"
    _write_env(rollback_file, rollback_values, mode=0o644)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="rollback_event_identity_mismatch",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


def test_rollback_must_keep_docker_target(tmp_path: Path) -> None:
    repository_root, release_file, rollback_file = _deployment_files(tmp_path)
    rollback_values = _release_values_from_file(rollback_file)
    rollback_values["AWS_DEMO_DOCKER_CONTEXT"] = "different-context"
    _write_env(rollback_file, rollback_values, mode=0o644)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="rollback_docker_target_mismatch",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("AWS_DEMO_ROUND_SCOPE", "single-round"),
        ("AWS_DEMO_ROUND_BACKUP_HOST_DIRECTORY", "/different/backup/path"),
        ("AWS_DEMO_RUNTIME_UID", "12345"),
        ("AWS_DEMO_RUNTIME_GID", "12345"),
    ],
)
def test_load_test_rollback_must_keep_topology_identity(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    repository_root, release_file, rollback_file, _ = _load_test_deployment_files(tmp_path)
    rollback_values = _release_values_from_file(rollback_file)
    if field == "AWS_DEMO_ROUND_SCOPE":
        rollback_values.pop("AWS_DEMO_ROUND_BACKUP_HOST_DIRECTORY")
        for observability_field in OBSERVABILITY_RELEASE_FIELDS:
            rollback_values.pop(observability_field)
    rollback_values[field] = value
    _write_env(rollback_file, rollback_values, mode=0o644)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="rollback_topology_mismatch",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o770, 0o600])
def test_load_test_backup_directory_requires_exact_0700(
    tmp_path: Path,
    mode: int,
) -> None:
    repository_root, release_file, rollback_file, backup_directory = _load_test_deployment_files(
        tmp_path
    )
    backup_directory.chmod(mode)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="round_backup_host_directory_permissions_not_0700",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


def test_load_test_backup_directory_must_be_outside_repository(tmp_path: Path) -> None:
    repository_root, release_file, rollback_file, _ = _load_test_deployment_files(tmp_path)
    inside_directory = repository_root / "round-backups"
    inside_directory.mkdir(mode=0o700)
    inside_directory.chmod(0o700)
    for path in (release_file, rollback_file):
        values = _release_values_from_file(path)
        values["AWS_DEMO_ROUND_BACKUP_HOST_DIRECTORY"] = str(inside_directory)
        _write_env(path, values, mode=0o644)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="round_backup_host_directory_inside_repository",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


def test_load_test_backup_directory_must_not_be_repository_ancestor(
    tmp_path: Path,
) -> None:
    repository_root, release_file, rollback_file, _ = _load_test_deployment_files(tmp_path)
    tmp_path.chmod(0o700)
    for path in (release_file, rollback_file):
        values = _release_values_from_file(path)
        values["AWS_DEMO_ROUND_BACKUP_HOST_DIRECTORY"] = str(tmp_path)
        _write_env(path, values, mode=0o644)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="round_backup_host_directory_inside_repository",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


def test_load_test_backup_directory_must_not_be_symlink(tmp_path: Path) -> None:
    repository_root, release_file, rollback_file, backup_directory = _load_test_deployment_files(
        tmp_path
    )
    symlink = backup_directory.parent / "round-backups-link"
    symlink.symlink_to(backup_directory, target_is_directory=True)
    for path in (release_file, rollback_file):
        values = _release_values_from_file(path)
        values["AWS_DEMO_ROUND_BACKUP_HOST_DIRECTORY"] = str(symlink)
        _write_env(path, values, mode=0o644)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="round_backup_host_directory_symlink_forbidden",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


def test_load_test_backup_directory_must_not_contain_release_control_files(
    tmp_path: Path,
) -> None:
    repository_root, release_file, rollback_file, backup_directory = _load_test_deployment_files(
        tmp_path
    )
    control_directory = backup_directory.parent
    control_directory.chmod(0o700)
    for path in (release_file, rollback_file):
        values = _release_values_from_file(path)
        values["AWS_DEMO_ROUND_BACKUP_HOST_DIRECTORY"] = str(control_directory)
        _write_env(path, values, mode=0o644)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="round_backup_host_directory_contains_control_file",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


@pytest.mark.parametrize("protected_kind", ["runtime", "postgres-ca"])
def test_load_test_backup_directory_must_not_contain_runtime_secret_or_ca(
    tmp_path: Path,
    protected_kind: str,
) -> None:
    repository_root, release_file, rollback_file, backup_directory = _load_test_deployment_files(
        tmp_path
    )
    if protected_kind == "runtime":
        values = _release_values_from_file(release_file)
        original_runtime = Path(values["AWS_DEMO_RUNTIME_ENV_FILE"])
        nested_runtime = backup_directory / "release-runtime.env"
        nested_runtime.write_bytes(original_runtime.read_bytes())
        nested_runtime.chmod(0o600)
        values["AWS_DEMO_RUNTIME_ENV_FILE"] = str(nested_runtime)
        _write_env(release_file, values, mode=0o644)
    else:
        nested_ca = backup_directory / "postgres-ca.pem"
        nested_ca.write_bytes(TEST_CA_BUNDLE)
        nested_ca.chmod(0o644)
        for path in (release_file, rollback_file):
            values = _release_values_from_file(path)
            values["AWS_DEMO_POSTGRES_CA_FILE"] = str(nested_ca)
            values["AWS_DEMO_POSTGRES_CA_SHA256"] = hashlib.sha256(TEST_CA_BUNDLE).hexdigest()
            _write_env(path, values, mode=0o644)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="round_backup_host_directory_contains_control_file",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


def test_release_runtime_identity_must_match_preflight_account(tmp_path: Path) -> None:
    repository_root, release_file, rollback_file = _deployment_files(tmp_path)
    for path in (release_file, rollback_file):
        values = _release_values_from_file(path)
        values["AWS_DEMO_RUNTIME_UID"] = str(os.geteuid() + 1)
        _write_env(path, values, mode=0o644)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="runtime_identity_mismatch",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


def test_load_test_backup_owner_must_match_runtime_and_preflight_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_root, release_file, _, _ = _load_test_deployment_files(tmp_path)
    manifest = validate_release_manifest_file(release_file, repository_root)
    expected_uid = os.geteuid() + 1
    forged_identity = manifest.model_copy(update={"runtime_uid": expected_uid})
    monkeypatch.setattr("validation.aws_demo_deployment.os.geteuid", lambda: expected_uid)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="round_backup_host_directory_owner_mismatch",
    ):
        validate_round_backup_host_directory(
            forged_identity,
            repository_root,
            protected_paths=(),
        )


def test_load_test_rollback_runtime_must_keep_backup_key(tmp_path: Path) -> None:
    repository_root, release_file, rollback_file, _ = _load_test_deployment_files(tmp_path)
    rollback_runtime = Path(_release_values_from_file(rollback_file)["AWS_DEMO_RUNTIME_ENV_FILE"])
    values = _runtime_values(ROLLBACK_COMMIT)
    values.update(
        {
            "AWS_DEMO_ROUND_SCOPE": "load-test-ten-round",
            "API_INSTANCE_ID": "api-primary",
            "ROUND_BACKUP_STORAGE_MODE": "filesystem",
            "ROUND_BACKUP_DIRECTORY": "/var/lib/quiz-round-backups",
            "ROUND_BACKUP_ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(
                "ascii"
            ),
        }
    )
    _write_env(rollback_runtime, values)

    with pytest.raises(AwsDemoDeploymentValidationError) as error:
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)

    assert error.value.code == "rollback_runtime_identity_mismatch"
    assert error.value.fields == ("ROUND_BACKUP_ENCRYPTION_KEY",)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ROUND_BACKUP_STORAGE_MODE", "disabled"),
        ("ROUND_BACKUP_DIRECTORY", "/var/empty/quiz-round-backups"),
        ("ROUND_BACKUP_ENCRYPTION_KEY", "disabled"),
    ],
)
def test_load_test_runtime_rejects_inconsistent_backup_contract(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    repository_root, release_file, rollback_file, _ = _load_test_deployment_files(tmp_path)
    runtime_path = Path(_release_values_from_file(release_file)["AWS_DEMO_RUNTIME_ENV_FILE"])
    runtime_values = _runtime_values(RELEASE_COMMIT)
    runtime_values.update(
        {
            "AWS_DEMO_ROUND_SCOPE": "load-test-ten-round",
            "API_INSTANCE_ID": "api-primary",
            "ROUND_BACKUP_STORAGE_MODE": "filesystem",
            "ROUND_BACKUP_DIRECTORY": "/var/lib/quiz-round-backups",
            "ROUND_BACKUP_ENCRYPTION_KEY": LOAD_TEST_BACKUP_KEY_FIXTURE,
            field: value,
        }
    )
    _write_env(runtime_path, runtime_values)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="runtime_environment_invalid|runtime_round_backup_container_path_mismatch",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


def test_rollback_operation_does_not_depend_on_failed_runtime_or_ca_file(
    tmp_path: Path,
) -> None:
    repository_root, release_file, rollback_file = _deployment_files(tmp_path)
    failed_values = _release_values_from_file(rollback_file)
    failed_values["AWS_DEMO_RUNTIME_ENV_FILE"] = str(tmp_path / "missing-failed-runtime.env")
    failed_values["AWS_DEMO_POSTGRES_CA_FILE"] = str(tmp_path / "missing-failed-ca.pem")
    _write_env(rollback_file, failed_values, mode=0o644)

    rollback = validate_aws_demo_rollback(release_file, rollback_file, repository_root)

    assert rollback.release.release_id == "aws-demo-release-002"
    assert rollback.failed_release.release_id == "aws-demo-release-001"

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="postgres_ca_file_unavailable",
    ):
        validate_aws_demo_deployment(release_file, rollback_file, repository_root)


def test_load_rollback_rejects_failed_runtime_path_inside_backup(
    tmp_path: Path,
) -> None:
    repository_root, release_file, failed_release_file, backup_directory = (
        _load_test_deployment_files(tmp_path)
    )
    failed_values = _release_values_from_file(failed_release_file)
    original_runtime = Path(failed_values["AWS_DEMO_RUNTIME_ENV_FILE"])
    nested_runtime = backup_directory / f"rb-{'f' * 32}.advance_round.fernet"
    nested_runtime.write_bytes(original_runtime.read_bytes())
    nested_runtime.chmod(0o600)
    failed_values["AWS_DEMO_RUNTIME_ENV_FILE"] = str(nested_runtime)
    _write_env(failed_release_file, failed_values, mode=0o644)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="round_backup_host_directory_contains_control_file",
    ):
        validate_aws_demo_rollback(
            release_file,
            failed_release_file,
            repository_root,
        )


def test_load_rollback_does_not_require_failed_runtime_or_ca_to_exist(
    tmp_path: Path,
) -> None:
    repository_root, release_file, failed_release_file, _ = _load_test_deployment_files(tmp_path)
    failed_values = _release_values_from_file(failed_release_file)
    failed_values["AWS_DEMO_RUNTIME_ENV_FILE"] = str(tmp_path / "missing-failed-runtime.env")
    failed_values["AWS_DEMO_POSTGRES_CA_FILE"] = str(tmp_path / "missing-failed-ca.pem")
    _write_env(failed_release_file, failed_values, mode=0o644)

    rollback = validate_aws_demo_rollback(
        release_file,
        failed_release_file,
        repository_root,
    )

    assert rollback.release.release_id == "aws-demo-release-002"
    assert rollback.failed_release.release_id == "aws-demo-release-001"


def _release_values_from_file(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", maxsplit=1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )


def test_new_formal_run_accepts_same_artifact_recovery_release(tmp_path: Path) -> None:
    root, release, recovery, _ = _load_test_deployment_files(tmp_path)
    candidate = _release_values_from_file(release)
    values = _release_values_from_file(recovery)
    for field in ("AWS_DEMO_API_IMAGE", "AWS_DEMO_GATEWAY_IMAGE", "AWS_DEMO_SOURCE_COMMIT"):
        values[field] = candidate[field]
    _write_env(recovery, values, mode=0o644)
    runtime_path = Path(values["AWS_DEMO_RUNTIME_ENV_FILE"])
    runtime = _release_values_from_file(runtime_path)
    runtime["APP_VERSION"] = RELEASE_COMMIT
    _write_env(runtime_path, runtime)
    deployment = validate_aws_demo_deployment(release, recovery, root)
    assert deployment.release.api_image == deployment.rollback.api_image
