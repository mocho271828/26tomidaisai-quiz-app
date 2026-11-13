import os
from pathlib import Path

import pytest
from test_aws_demo_deployment import (
    AwsDemoDeploymentValidationError,
    _deployment_files,
    _load_test_deployment_files,
    _rehearsal_deployment_files,
    _release_values_from_file,
    _write_env,
    aws_demo_deployment_module,
    validate_aws_demo_deployment,
)


def test_rehearsal_candidate_alone_does_not_read_old_release_or_runtime(tmp_path: Path) -> None:
    root, release, rollback, backup = _rehearsal_deployment_files(tmp_path)
    Path(_release_values_from_file(rollback)["AWS_DEMO_RUNTIME_ENV_FILE"]).unlink()
    rollback.unlink()

    deployment = validate_aws_demo_deployment(release, None, root)

    assert deployment.rollback is None and deployment.rollback_settings is None
    assert deployment.release.round_scope == "rehearsal-six-round"
    assert deployment.release.round_backup_host_directory == backup
    assert deployment.release_settings.round_backup_storage_mode == "filesystem"
    assert deployment.release_settings.enable_demo_seed is False


@pytest.mark.parametrize("scope", ["single-round", "load-test-ten-round"])
def test_other_scopes_still_require_rollback(tmp_path: Path, scope: str) -> None:
    paths = (
        _deployment_files(tmp_path)
        if scope == "single-round"
        else _load_test_deployment_files(tmp_path)
    )
    with pytest.raises(AwsDemoDeploymentValidationError, match="rollback_manifest_required"):
        validate_aws_demo_deployment(paths[1], None, paths[0])


def test_cli_accepts_candidate_only_for_rehearsal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, release, _, _ = _rehearsal_deployment_files(tmp_path)
    status = aws_demo_deployment_module.main(
        [
            "--release-file",
            str(release),
            "--repository-root",
            str(root),
        ]
    )
    assert status == os.EX_OK
    assert "rollback: none" in capsys.readouterr().out


def test_rollback_operation_still_requires_failed_release(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, release, _, _ = _rehearsal_deployment_files(tmp_path)
    status = aws_demo_deployment_module.main(
        [
            "--release-file",
            str(release),
            "--repository-root",
            str(root),
            "--rollback-operation",
        ]
    )
    assert status == os.EX_CONFIG
    assert "rollback_manifest_required" in capsys.readouterr().err


@pytest.mark.parametrize(
    "field,value",
    [
        ("ENABLE_DEMO_SEED", "true"),
        ("SESSION_COOKIE_SECURE", "false"),
        ("REALTIME_ALLOWED_ORIGINS", '["http://quiz-demo.aws.internal"]'),
        ("APP_VERSION", "1" * 40),
        ("AWS_DEMO_ROUND_SCOPE", "load-test-ten-round"),
        ("ROUND_BACKUP_ENCRYPTION_KEY", "disabled"),
    ],
)
def test_candidate_only_keeps_runtime_safety_checks(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    root, release, _, _ = _rehearsal_deployment_files(tmp_path)
    runtime = Path(_release_values_from_file(release)["AWS_DEMO_RUNTIME_ENV_FILE"])
    values = _release_values_from_file(runtime)
    values[field] = value
    _write_env(runtime, values)
    with pytest.raises(AwsDemoDeploymentValidationError):
        validate_aws_demo_deployment(release, None, root)


@pytest.mark.parametrize(
    "field,value",
    [
        ("AWS_DEMO_API_IMAGE", "registry.internal/quiz/api:latest"),
        ("AWS_DEMO_EVENT_ID", "60000000-0000-4000-8000-000000000001"),
        ("AWS_DEMO_SCHEMA_REVISION", "old-schema"),
        ("AWS_DEMO_RUNTIME_UID", str(os.geteuid() + 1)),
        ("AWS_DEMO_POSTGRES_CA_SHA256", "0" * 64),
    ],
)
def test_candidate_only_keeps_release_identity_and_ca_checks(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    root, release, _, _ = _rehearsal_deployment_files(tmp_path)
    values = _release_values_from_file(release)
    values[field] = value
    _write_env(release, values, mode=0o644)
    with pytest.raises(AwsDemoDeploymentValidationError):
        validate_aws_demo_deployment(release, None, root)


def test_candidate_only_rejects_unsafe_backup_permissions(tmp_path: Path) -> None:
    root, release, _, backup = _rehearsal_deployment_files(tmp_path)
    backup.chmod(0o777)
    with pytest.raises(AwsDemoDeploymentValidationError):
        validate_aws_demo_deployment(release, None, root)


def test_candidate_only_keeps_backup_separate_from_release_and_runtime(tmp_path: Path) -> None:
    root, release, _, _ = _rehearsal_deployment_files(tmp_path)
    values = _release_values_from_file(release)
    values["AWS_DEMO_ROUND_BACKUP_HOST_DIRECTORY"] = str(release.parent)
    _write_env(release, values, mode=0o644)
    with pytest.raises(AwsDemoDeploymentValidationError):
        validate_aws_demo_deployment(release, None, root)
