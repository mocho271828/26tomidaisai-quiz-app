"""AWS demo外部pathとbackup bindのfail-closed検証。"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

ROUND_BACKUP_ARTIFACT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^rb-[0-9a-f]{32}\.(?:advance_round|complete_event)\.fernet$"
)


class AwsDemoDeploymentValidationError(ValueError):
    """秘密値を含めずにAWS demo配置設定の拒否理由を表す。"""

    def __init__(self, code: str, fields: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.fields = fields


@dataclass(frozen=True, slots=True)
class ExternalFilePolicy:
    """外部fileのpath・owner・mode検証に使う固定error契約。"""

    path_not_absolute: str
    symlink_forbidden: str
    unavailable: str
    not_regular: str
    hardlink_forbidden: str
    owner_mismatch: str
    inside_repository: str
    permissions_invalid: str
    parent_permissions_writable: str
    parent_owner_mismatch: str
    exact_mode: int | None = None
    forbidden_mode_bits: int = 0


RUNTIME_ENVIRONMENT_FILE_POLICY: Final[ExternalFilePolicy] = ExternalFilePolicy(
    path_not_absolute="runtime_environment_path_not_absolute",
    symlink_forbidden="runtime_environment_symlink_forbidden",
    unavailable="runtime_environment_file_unavailable",
    not_regular="runtime_environment_not_regular_file",
    hardlink_forbidden="runtime_environment_hardlink_forbidden",
    owner_mismatch="runtime_environment_owner_mismatch",
    inside_repository="runtime_environment_inside_repository",
    permissions_invalid="runtime_environment_permissions_not_0600",
    parent_permissions_writable="runtime_environment_parent_permissions_writable",
    parent_owner_mismatch="runtime_environment_parent_owner_mismatch",
    exact_mode=0o600,
)

RELEASE_MANIFEST_FILE_POLICY: Final[ExternalFilePolicy] = ExternalFilePolicy(
    path_not_absolute="release_manifest_path_not_absolute",
    symlink_forbidden="release_manifest_symlink_forbidden",
    unavailable="release_manifest_unavailable",
    not_regular="release_manifest_not_regular_file",
    hardlink_forbidden="release_manifest_hardlink_forbidden",
    owner_mismatch="release_manifest_owner_mismatch",
    inside_repository="release_manifest_inside_repository",
    permissions_invalid="release_manifest_permissions_writable",
    parent_permissions_writable="release_manifest_parent_permissions_writable",
    parent_owner_mismatch="release_manifest_parent_owner_mismatch",
    forbidden_mode_bits=0o022,
)

POSTGRES_CA_FILE_POLICY: Final[ExternalFilePolicy] = ExternalFilePolicy(
    path_not_absolute="postgres_ca_path_not_absolute",
    symlink_forbidden="postgres_ca_symlink_forbidden",
    unavailable="postgres_ca_file_unavailable",
    not_regular="postgres_ca_not_regular_file",
    hardlink_forbidden="postgres_ca_hardlink_forbidden",
    owner_mismatch="postgres_ca_owner_mismatch",
    inside_repository="postgres_ca_inside_repository",
    permissions_invalid="postgres_ca_permissions_writable",
    parent_permissions_writable="postgres_ca_parent_permissions_writable",
    parent_owner_mismatch="postgres_ca_parent_owner_mismatch",
    forbidden_mode_bits=0o022,
)

POSTGRES_EXPORTER_PASSWORD_FILE_POLICY: Final[ExternalFilePolicy] = ExternalFilePolicy(
    path_not_absolute="postgres_exporter_password_path_not_absolute",
    symlink_forbidden="postgres_exporter_password_symlink_forbidden",
    unavailable="postgres_exporter_password_unavailable",
    not_regular="postgres_exporter_password_not_regular_file",
    hardlink_forbidden="postgres_exporter_password_hardlink_forbidden",
    owner_mismatch="postgres_exporter_password_owner_mismatch",
    inside_repository="postgres_exporter_password_inside_repository",
    permissions_invalid="postgres_exporter_password_permissions_not_0600",
    parent_permissions_writable="postgres_exporter_password_parent_permissions_writable",
    parent_owner_mismatch="postgres_exporter_password_parent_owner_mismatch",
    exact_mode=0o600,
)

REDIS_EXPORTER_PASSWORD_FILE_POLICY: Final[ExternalFilePolicy] = ExternalFilePolicy(
    path_not_absolute="redis_exporter_password_path_not_absolute",
    symlink_forbidden="redis_exporter_password_symlink_forbidden",
    unavailable="redis_exporter_password_unavailable",
    not_regular="redis_exporter_password_not_regular_file",
    hardlink_forbidden="redis_exporter_password_hardlink_forbidden",
    owner_mismatch="redis_exporter_password_owner_mismatch",
    inside_repository="redis_exporter_password_inside_repository",
    permissions_invalid="redis_exporter_password_permissions_not_0600",
    parent_permissions_writable="redis_exporter_password_parent_permissions_writable",
    parent_owner_mismatch="redis_exporter_password_parent_owner_mismatch",
    exact_mode=0o600,
)

LOAD_TEST_MANIFEST_FILE_POLICY: Final[ExternalFilePolicy] = ExternalFilePolicy(
    path_not_absolute="load_test_manifest_path_not_absolute",
    symlink_forbidden="load_test_manifest_symlink_forbidden",
    unavailable="load_test_manifest_unavailable",
    not_regular="load_test_manifest_not_regular_file",
    hardlink_forbidden="load_test_manifest_hardlink_forbidden",
    owner_mismatch="load_test_manifest_owner_mismatch",
    inside_repository="load_test_manifest_inside_repository",
    permissions_invalid="load_test_manifest_permissions_not_0600",
    parent_permissions_writable="load_test_manifest_parent_permissions_writable",
    parent_owner_mismatch="load_test_manifest_parent_owner_mismatch",
    exact_mode=0o600,
)

TOXIPROXY_CONFIG_FILE_POLICY: Final[ExternalFilePolicy] = ExternalFilePolicy(
    path_not_absolute="toxiproxy_config_path_not_absolute",
    symlink_forbidden="toxiproxy_config_symlink_forbidden",
    unavailable="toxiproxy_config_unavailable",
    not_regular="toxiproxy_config_not_regular_file",
    hardlink_forbidden="toxiproxy_config_hardlink_forbidden",
    owner_mismatch="toxiproxy_config_owner_mismatch",
    inside_repository="toxiproxy_config_inside_repository",
    permissions_invalid="toxiproxy_config_permissions_not_0600",
    parent_permissions_writable="toxiproxy_config_parent_permissions_writable",
    parent_owner_mismatch="toxiproxy_config_parent_owner_mismatch",
    exact_mode=0o600,
)


@dataclass(frozen=True, slots=True)
class ValidatedExternalPath:
    resolved: Path
    status: os.stat_result
    ancestors: tuple[tuple[Path, os.stat_result], ...]


def _inspect_lexical_path(
    path: Path,
    *,
    path_not_absolute_code: str,
    symlink_code: str,
    unavailable_code: str,
) -> ValidatedExternalPath:
    """resolve前の全componentを検査しsymlink差替え境界を狭める。"""

    if not path.is_absolute() or ".." in path.parts:
        raise AwsDemoDeploymentValidationError(path_not_absolute_code)

    current = Path(path.anchor)
    final_status: os.stat_result | None = None
    ancestors: list[tuple[Path, os.stat_result]] = []
    try:
        root_status = current.lstat()
        ancestors.append((current, root_status))
        for index, component in enumerate(path.parts[1:], start=1):
            current /= component
            component_status = current.lstat()
            if stat.S_ISLNK(component_status.st_mode):
                raise AwsDemoDeploymentValidationError(symlink_code)
            if index < len(path.parts) - 1 and not stat.S_ISDIR(component_status.st_mode):
                raise AwsDemoDeploymentValidationError(unavailable_code)
            if index < len(path.parts) - 1:
                ancestors.append((current, component_status))
            final_status = component_status
        resolved = path.resolve(strict=True)
    except AwsDemoDeploymentValidationError:
        raise
    except OSError as error:
        raise AwsDemoDeploymentValidationError(unavailable_code) from error

    if final_status is None:
        raise AwsDemoDeploymentValidationError(unavailable_code)
    return ValidatedExternalPath(
        resolved=resolved,
        status=final_status,
        ancestors=tuple(ancestors),
    )


def _validate_external_parent_chain(
    validated: ValidatedExternalPath,
    *,
    permissions_error_code: str,
    owner_error_code: str,
) -> None:
    """全祖先の共有書込みと直親のowner差替え可能性を拒否する。"""

    for _, ancestor_status in validated.ancestors:
        if (
            not stat.S_ISDIR(ancestor_status.st_mode)
            or stat.S_IMODE(ancestor_status.st_mode) & 0o022
        ):
            raise AwsDemoDeploymentValidationError(permissions_error_code)
        if ancestor_status.st_uid not in {0, os.geteuid()}:
            raise AwsDemoDeploymentValidationError(owner_error_code)
    if not validated.ancestors:
        raise AwsDemoDeploymentValidationError(permissions_error_code)
    if validated.ancestors[-1][1].st_uid != os.geteuid():
        raise AwsDemoDeploymentValidationError(owner_error_code)


def validate_external_regular_file(
    path: Path,
    repository_root: Path,
    policy: ExternalFilePolicy,
) -> ValidatedExternalPath:
    """repository外のowner管理されたregular fileを固定policyで検証する。"""

    validated = _inspect_lexical_path(
        path,
        path_not_absolute_code=policy.path_not_absolute,
        symlink_code=policy.symlink_forbidden,
        unavailable_code=policy.unavailable,
    )
    if not stat.S_ISREG(validated.status.st_mode):
        raise AwsDemoDeploymentValidationError(policy.not_regular)
    if validated.status.st_nlink != 1:
        raise AwsDemoDeploymentValidationError(policy.hardlink_forbidden)
    if validated.status.st_uid != os.geteuid():
        raise AwsDemoDeploymentValidationError(policy.owner_mismatch)
    try:
        resolved_repository_root = repository_root.resolve(strict=True)
    except OSError as error:
        raise AwsDemoDeploymentValidationError(policy.unavailable) from error
    if validated.resolved.is_relative_to(resolved_repository_root):
        raise AwsDemoDeploymentValidationError(policy.inside_repository)

    mode = stat.S_IMODE(validated.status.st_mode)
    if policy.exact_mode is not None and mode != policy.exact_mode:
        raise AwsDemoDeploymentValidationError(policy.permissions_invalid)
    if policy.forbidden_mode_bits and mode & policy.forbidden_mode_bits:
        raise AwsDemoDeploymentValidationError(policy.permissions_invalid)
    _validate_external_parent_chain(
        validated,
        permissions_error_code=policy.parent_permissions_writable,
        owner_error_code=policy.parent_owner_mismatch,
    )
    return validated


def read_validated_external_file(
    validated: ValidatedExternalPath,
    *,
    maximum_bytes: int,
    unavailable_code: str,
) -> bytes:
    """検証済みinodeをO_NOFOLLOWで開き、有限長かつ不変な内容だけを返す。"""

    if maximum_bytes < 1:
        raise ValueError("maximum_bytesは1以上にしてください")

    descriptor = -1
    try:
        descriptor = os.open(
            validated.resolved,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        expected = validated.status
        if (
            (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
                stat.S_IMODE(opened.st_mode),
                opened.st_nlink,
                opened.st_uid,
            )
            != (
                expected.st_dev,
                expected.st_ino,
                expected.st_size,
                expected.st_mtime_ns,
                expected.st_ctime_ns,
                stat.S_IMODE(expected.st_mode),
                expected.st_nlink,
                expected.st_uid,
            )
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != os.geteuid()
            or not 1 <= opened.st_size <= maximum_bytes
        ):
            raise AwsDemoDeploymentValidationError(unavailable_code)

        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        final_status = os.fstat(descriptor)
        if (
            len(body) != opened.st_size
            or len(body) > maximum_bytes
            or (
                final_status.st_dev,
                final_status.st_ino,
                final_status.st_size,
                final_status.st_mtime_ns,
                final_status.st_ctime_ns,
                stat.S_IMODE(final_status.st_mode),
                final_status.st_nlink,
                final_status.st_uid,
            )
            != (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
                stat.S_IMODE(opened.st_mode),
                opened.st_nlink,
                opened.st_uid,
            )
        ):
            raise AwsDemoDeploymentValidationError(unavailable_code)
        return body
    except AwsDemoDeploymentValidationError:
        raise
    except OSError as error:
        raise AwsDemoDeploymentValidationError(unavailable_code) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def validate_external_artifact_directory(
    directory: Path,
    repository_root: Path,
    *,
    runtime_uid: int,
    runtime_gid: int,
    protected_paths: tuple[Path, ...],
) -> Path:
    """metrics export専用RW bindをrepositoryと制御fileから隔離する。"""

    validated = _inspect_lexical_path(
        directory,
        path_not_absolute_code="metrics_artifact_directory_path_not_absolute",
        symlink_code="metrics_artifact_directory_symlink_forbidden",
        unavailable_code="metrics_artifact_directory_unavailable",
    )
    if not stat.S_ISDIR(validated.status.st_mode):
        raise AwsDemoDeploymentValidationError("metrics_artifact_directory_not_directory")
    try:
        resolved_repository = repository_root.resolve(strict=True)
        resolved_protected = tuple(path.resolve(strict=True) for path in protected_paths)
    except OSError as error:
        raise AwsDemoDeploymentValidationError("protected_deployment_file_unavailable") from error
    if (
        validated.resolved == resolved_repository
        or validated.resolved.is_relative_to(resolved_repository)
        or resolved_repository.is_relative_to(validated.resolved)
    ):
        raise AwsDemoDeploymentValidationError("metrics_artifact_directory_intersects_repository")
    if any(
        path == validated.resolved or path.is_relative_to(validated.resolved)
        for path in resolved_protected
    ):
        raise AwsDemoDeploymentValidationError("metrics_artifact_directory_contains_control_file")
    if stat.S_IMODE(validated.status.st_mode) != 0o700:
        raise AwsDemoDeploymentValidationError("metrics_artifact_directory_permissions_not_0700")
    if (
        validated.status.st_uid != runtime_uid
        or validated.status.st_gid != runtime_gid
        or validated.status.st_uid != os.geteuid()
        or validated.status.st_gid != os.getegid()
    ):
        raise AwsDemoDeploymentValidationError("metrics_artifact_directory_owner_mismatch")
    try:
        if any(directory.iterdir()):
            raise AwsDemoDeploymentValidationError("metrics_artifact_directory_not_empty")
    except OSError as error:
        raise AwsDemoDeploymentValidationError("metrics_artifact_directory_not_empty") from error
    _validate_external_parent_chain(
        validated,
        permissions_error_code="metrics_artifact_directory_parent_permissions_writable",
        owner_error_code="metrics_artifact_directory_parent_owner_mismatch",
    )
    return validated.resolved


def validate_round_backup_directory(
    directory: Path,
    repository_root: Path,
    *,
    runtime_uid: int,
    runtime_gid: int,
    protected_paths: tuple[Path, ...],
    optional_protected_paths: tuple[Path, ...] = (),
) -> Path:
    """RW bind元と既存artifactだけを厳格なload-test契約で検証する。"""

    validated = _inspect_lexical_path(
        directory,
        path_not_absolute_code="round_backup_host_directory_path_not_absolute",
        symlink_code="round_backup_host_directory_symlink_forbidden",
        unavailable_code="round_backup_host_directory_unavailable",
    )
    if not stat.S_ISDIR(validated.status.st_mode):
        raise AwsDemoDeploymentValidationError("round_backup_host_directory_not_directory")
    try:
        resolved_repository_root = repository_root.resolve(strict=True)
        resolved_protected_paths = tuple(path.resolve(strict=True) for path in protected_paths)
    except OSError as error:
        raise AwsDemoDeploymentValidationError("protected_deployment_file_unavailable") from error
    if (
        validated.resolved.is_relative_to(resolved_repository_root)
        or resolved_repository_root == validated.resolved
        or resolved_repository_root.is_relative_to(validated.resolved)
    ):
        raise AwsDemoDeploymentValidationError("round_backup_host_directory_inside_repository")
    if any(
        path == validated.resolved or path.is_relative_to(validated.resolved)
        for path in resolved_protected_paths
    ):
        raise AwsDemoDeploymentValidationError("round_backup_host_directory_contains_control_file")
    _reject_optional_protected_path_containment(
        validated.resolved,
        optional_protected_paths,
    )
    if stat.S_IMODE(validated.status.st_mode) != 0o700:
        raise AwsDemoDeploymentValidationError("round_backup_host_directory_permissions_not_0700")
    if (
        validated.status.st_uid != runtime_uid
        or validated.status.st_gid != runtime_gid
        or validated.status.st_uid != os.geteuid()
        or validated.status.st_gid != os.getegid()
    ):
        raise AwsDemoDeploymentValidationError("round_backup_host_directory_owner_mismatch")

    _validate_external_parent_chain(
        validated,
        permissions_error_code="round_backup_host_directory_parent_permissions_writable",
        owner_error_code="round_backup_host_directory_parent_owner_mismatch",
    )
    validate_round_backup_directory_contents(validated.resolved, runtime_uid, runtime_gid)
    return validated.resolved


def _reject_optional_protected_path_containment(
    backup_directory: Path,
    protected_paths: tuple[Path, ...],
) -> None:
    """壊れたrollback側fileを開かずmanifestのpath containmentだけを拒否する。"""

    for protected_path in protected_paths:
        if not protected_path.is_absolute():
            raise AwsDemoDeploymentValidationError("protected_deployment_path_not_absolute")
        lexical_path = Path(os.path.normpath(protected_path))
        if lexical_path == backup_directory or lexical_path.is_relative_to(backup_directory):
            raise AwsDemoDeploymentValidationError(
                "round_backup_host_directory_contains_control_file"
            )
        try:
            resolved_path = protected_path.resolve(strict=True)
        except OSError:
            continue
        if resolved_path == backup_directory or resolved_path.is_relative_to(backup_directory):
            raise AwsDemoDeploymentValidationError(
                "round_backup_host_directory_contains_control_file"
            )


def validate_round_backup_directory_contents(
    directory: Path,
    runtime_uid: int,
    runtime_gid: int,
) -> None:
    """preflight前から存在するartifact以外をRW mountへ持ち込ませない。"""

    try:
        entries = tuple(directory.iterdir())
        for entry in entries:
            entry_status = entry.lstat()
            if stat.S_ISLNK(entry_status.st_mode):
                raise AwsDemoDeploymentValidationError("round_backup_artifact_symlink_forbidden")
            if not stat.S_ISREG(entry_status.st_mode):
                raise AwsDemoDeploymentValidationError("round_backup_artifact_not_regular_file")
            if entry_status.st_nlink != 1:
                raise AwsDemoDeploymentValidationError("round_backup_artifact_hardlink_forbidden")
            if ROUND_BACKUP_ARTIFACT_PATTERN.fullmatch(entry.name) is None:
                raise AwsDemoDeploymentValidationError("round_backup_artifact_name_invalid")
            if stat.S_IMODE(entry_status.st_mode) != 0o600:
                raise AwsDemoDeploymentValidationError("round_backup_artifact_permissions_not_0600")
            if (
                entry_status.st_uid != runtime_uid
                or entry_status.st_gid != runtime_gid
                or entry_status.st_uid != os.geteuid()
                or entry_status.st_gid != os.getegid()
            ):
                raise AwsDemoDeploymentValidationError("round_backup_artifact_owner_mismatch")
    except AwsDemoDeploymentValidationError:
        raise
    except OSError as error:
        raise AwsDemoDeploymentValidationError(
            "round_backup_host_directory_contents_unavailable"
        ) from error
