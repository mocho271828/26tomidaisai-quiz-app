import os
from pathlib import Path

import pytest

from quiz_app.aws_demo_paths import (
    POSTGRES_CA_FILE_POLICY,
    RELEASE_MANIFEST_FILE_POLICY,
    RUNTIME_ENVIRONMENT_FILE_POLICY,
    AwsDemoDeploymentValidationError,
    ExternalFilePolicy,
    validate_external_regular_file,
    validate_round_backup_directory,
    validate_round_backup_directory_contents,
)


@pytest.mark.parametrize(
    ("policy", "mode"),
    [
        (RELEASE_MANIFEST_FILE_POLICY, 0o644),
        (RUNTIME_ENVIRONMENT_FILE_POLICY, 0o600),
        (POSTGRES_CA_FILE_POLICY, 0o644),
    ],
)
def test_external_file_rejects_intermediate_symlink(
    tmp_path: Path,
    policy: ExternalFilePolicy,
    mode: int,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    real_parent = tmp_path / "external-real"
    real_parent.mkdir()
    external_file = real_parent / "configuration.env"
    external_file.write_text("public fixture\n", encoding="utf-8")
    external_file.chmod(mode)
    linked_parent = tmp_path / "external-link"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match=policy.symlink_forbidden,
    ):
        validate_external_regular_file(
            linked_parent / external_file.name,
            repository_root,
            policy,
        )


def test_external_file_rejects_writable_grandparent(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    writable_grandparent = tmp_path / "shared"
    writable_grandparent.mkdir(mode=0o777)
    writable_grandparent.chmod(0o777)
    secure_parent = writable_grandparent / "current"
    secure_parent.mkdir(mode=0o700)
    secure_parent.chmod(0o700)
    runtime_file = secure_parent / "runtime.env"
    runtime_file.write_text("public fixture\n", encoding="utf-8")
    runtime_file.chmod(0o600)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="runtime_environment_parent_permissions_writable",
    ):
        validate_external_regular_file(
            runtime_file,
            repository_root,
            RUNTIME_ENVIRONMENT_FILE_POLICY,
        )


def test_external_file_rejects_third_party_owned_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    third_party_ancestor = tmp_path / "third-party"
    third_party_ancestor.mkdir()
    secure_parent = third_party_ancestor / "current"
    secure_parent.mkdir(mode=0o700)
    secure_parent.chmod(0o700)
    runtime_file = secure_parent / "runtime.env"
    runtime_file.write_text("public fixture\n", encoding="utf-8")
    runtime_file.chmod(0o600)
    original_lstat = Path.lstat

    def lstat_with_third_party_owner(path: Path) -> os.stat_result:
        result = original_lstat(path)
        if path != third_party_ancestor:
            return result
        fields = list(result)
        fields[4] = os.geteuid() + 1
        return os.stat_result(fields)

    monkeypatch.setattr(Path, "lstat", lstat_with_third_party_owner)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="runtime_environment_parent_owner_mismatch",
    ):
        validate_external_regular_file(
            runtime_file,
            repository_root,
            RUNTIME_ENVIRONMENT_FILE_POLICY,
        )


@pytest.mark.parametrize(
    ("policy", "mode"),
    [
        (RELEASE_MANIFEST_FILE_POLICY, 0o644),
        (RUNTIME_ENVIRONMENT_FILE_POLICY, 0o600),
        (POSTGRES_CA_FILE_POLICY, 0o644),
    ],
)
def test_external_file_rejects_hardlink_alias(
    tmp_path: Path,
    policy: ExternalFilePolicy,
    mode: int,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    source = repository_root / "protected-source"
    source.write_text("public fixture\n", encoding="utf-8")
    source.chmod(mode)
    external_parent = tmp_path / "external"
    external_parent.mkdir()
    alias = external_parent / "configuration.env"
    os.link(source, alias)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match=policy.hardlink_forbidden,
    ):
        validate_external_regular_file(alias, repository_root, policy)


def test_backup_directory_rejects_intermediate_symlink(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    real_parent = tmp_path / "external-real"
    real_parent.mkdir()
    backup_directory = real_parent / "round-backups"
    backup_directory.mkdir(mode=0o700)
    backup_directory.chmod(0o700)
    linked_parent = tmp_path / "external-link"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="round_backup_host_directory_symlink_forbidden",
    ):
        validate_round_backup_directory(
            linked_parent / backup_directory.name,
            repository_root,
            runtime_uid=os.geteuid(),
            runtime_gid=os.getegid(),
            protected_paths=(),
        )


@pytest.fixture
def backup_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "round-backups"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return directory


@pytest.mark.parametrize("operation", ["advance_round", "complete_event"])
def test_backup_directory_accepts_only_valid_existing_artifacts(
    backup_directory: Path,
    operation: str,
) -> None:
    artifact = backup_directory / f"rb-{'a' * 32}.{operation}.fernet"
    artifact.write_bytes(b"encrypted artifact")
    artifact.chmod(0o600)

    validate_round_backup_directory_contents(
        backup_directory,
        os.geteuid(),
        os.getegid(),
    )


def test_backup_directory_rejects_unrelated_secret_file(backup_directory: Path) -> None:
    unrelated_secret = backup_directory / "session-secret.env"
    unrelated_secret.write_text("must-not-be-mounted\n", encoding="utf-8")
    unrelated_secret.chmod(0o600)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="round_backup_artifact_name_invalid",
    ):
        validate_round_backup_directory_contents(
            backup_directory,
            os.geteuid(),
            os.getegid(),
        )


def test_backup_directory_rejects_subdirectory(backup_directory: Path) -> None:
    (backup_directory / "nested").mkdir()

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="round_backup_artifact_not_regular_file",
    ):
        validate_round_backup_directory_contents(
            backup_directory,
            os.geteuid(),
            os.getegid(),
        )


def test_backup_directory_rejects_symlinked_artifact(
    backup_directory: Path,
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.fernet"
    target.write_bytes(b"encrypted artifact")
    target.chmod(0o600)
    artifact = backup_directory / f"rb-{'b' * 32}.advance_round.fernet"
    artifact.symlink_to(target)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="round_backup_artifact_symlink_forbidden",
    ):
        validate_round_backup_directory_contents(
            backup_directory,
            os.geteuid(),
            os.getegid(),
        )


def test_backup_directory_rejects_artifact_with_wrong_mode(backup_directory: Path) -> None:
    artifact = backup_directory / f"rb-{'c' * 32}.complete_event.fernet"
    artifact.write_bytes(b"encrypted artifact")
    artifact.chmod(0o640)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="round_backup_artifact_permissions_not_0600",
    ):
        validate_round_backup_directory_contents(
            backup_directory,
            os.geteuid(),
            os.getegid(),
        )


def test_backup_directory_rejects_hardlinked_artifact(
    backup_directory: Path,
    tmp_path: Path,
) -> None:
    protected_file = tmp_path / "runtime.env"
    protected_file.write_text("must-not-be-mounted\n", encoding="utf-8")
    protected_file.chmod(0o600)
    artifact = backup_directory / f"rb-{'e' * 32}.advance_round.fernet"
    os.link(protected_file, artifact)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="round_backup_artifact_hardlink_forbidden",
    ):
        validate_round_backup_directory_contents(
            backup_directory,
            os.geteuid(),
            os.getegid(),
        )


def test_backup_directory_rejects_artifact_with_wrong_owner(backup_directory: Path) -> None:
    artifact = backup_directory / f"rb-{'d' * 32}.advance_round.fernet"
    artifact.write_bytes(b"encrypted artifact")
    artifact.chmod(0o600)

    with pytest.raises(
        AwsDemoDeploymentValidationError,
        match="round_backup_artifact_owner_mismatch",
    ):
        validate_round_backup_directory_contents(
            backup_directory,
            os.geteuid() + 1,
            os.getegid(),
        )
