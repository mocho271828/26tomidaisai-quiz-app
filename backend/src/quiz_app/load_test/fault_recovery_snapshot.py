"""障害controller専用の最小Compose定義を外部directoryへ不変化する。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from quiz_app.load_test.controller_contracts import canonical_json_bytes
from quiz_app.load_test.fault_host_config import (
    FaultControllerHostError,
    FaultHostConfiguration,
)
from quiz_app.load_test.fault_topology import (
    TOXIPROXY_CONFIG_SHA256_ENV,
    TOXIPROXY_EXPECTED_LISTEN_ENV,
    TOXIPROXY_EXPECTED_NAME_ENV,
    TOXIPROXY_EXPECTED_UPSTREAM_ENV,
)

COMPOSE_PROJECT: Final = "gs-app-tomidaisai-aws-demo"
FAULT_CONTROL_SERVICE: Final = "fault-control-client"
API_SECONDARY_SERVICE: Final = "api-secondary"
FAULT_CONTROL_NETWORK: Final = "fault-control"
FAULT_CONTROL_NETWORK_NAME: Final = f"{COMPOSE_PROJECT}_{FAULT_CONTROL_NETWORK}"
MAXIMUM_SNAPSHOT_BYTES: Final = 16 * 1024

_SAFE_PATH: Final = re.compile(r"^[A-Za-z0-9_./:@+-]+$")
_SNAPSHOT_NAME: Final = re.compile(r"^fault-recovery-[0-9a-f]{32}-[0-9a-f]{64}\.compose\.json$")


@dataclass(frozen=True, slots=True)
class FaultRecoveryComposeSnapshot:
    """content-addressed Compose fileの検証済みidentity。"""

    path: Path
    sha256: str
    size_bytes: int


def _snapshot_body(configuration: FaultHostConfiguration) -> bytes:
    proxy = configuration.toxiproxy_proxy
    document: dict[str, object] = {
        "name": COMPOSE_PROJECT,
        "networks": {
            FAULT_CONTROL_NETWORK: {
                "external": True,
                "name": FAULT_CONTROL_NETWORK_NAME,
            }
        },
        "services": {
            API_SECONDARY_SERVICE: {
                "image": configuration.api_image,
                "pull_policy": "never",
            },
            FAULT_CONTROL_SERVICE: {
                "cap_drop": ["ALL"],
                "command": ["probe"],
                "entrypoint": [
                    "python",
                    "-m",
                    "quiz_app.load_test.fault_control_probe",
                ],
                "environment": {
                    TOXIPROXY_CONFIG_SHA256_ENV: (configuration.toxiproxy_configuration_sha256),
                    TOXIPROXY_EXPECTED_LISTEN_ENV: proxy.listen,
                    TOXIPROXY_EXPECTED_NAME_ENV: proxy.name,
                    TOXIPROXY_EXPECTED_UPSTREAM_ENV: proxy.upstream,
                },
                "image": configuration.api_image,
                "networks": [FAULT_CONTROL_NETWORK],
                "profiles": ["fault-control"],
                "pull_policy": "never",
                "read_only": True,
                "restart": "no",
                "security_opt": ["no-new-privileges:true"],
                "user": f"{os.geteuid()}:{os.getegid()}",
            },
        },
    }
    body = canonical_json_bytes(document) + b"\n"
    if not 1 <= len(body) <= MAXIMUM_SNAPSHOT_BYTES:
        raise FaultControllerHostError("fault_host_recovery_snapshot_invalid")
    return body


def _open_snapshot_directory(directory: Path, repository_root: Path, name: str) -> int:
    if (
        not directory.is_absolute()
        or ".." in directory.parts
        or _SAFE_PATH.fullmatch(str(directory)) is None
    ):
        raise FaultControllerHostError("fault_host_recovery_snapshot_directory_invalid")
    descriptor = -1
    try:
        before = directory.lstat()
        resolved = directory.resolve(strict=True)
        repository = repository_root.resolve(strict=True)
        for ancestor in (directory, *directory.parents):
            ancestor_status = ancestor.lstat()
            is_sticky = stat.S_ISDIR(ancestor_status.st_mode) and bool(
                ancestor_status.st_mode & stat.S_ISVTX
            )
            if (
                stat.S_ISLNK(ancestor_status.st_mode)
                or not stat.S_ISDIR(ancestor_status.st_mode)
                or ancestor_status.st_uid not in {0, os.geteuid()}
                or (ancestor_status.st_mode & 0o022 and not is_sticky)
            ):
                raise FaultControllerHostError("fault_host_recovery_snapshot_directory_invalid")
        if (
            resolved != directory
            or resolved.is_relative_to(repository)
            or repository.is_relative_to(resolved)
        ):
            raise FaultControllerHostError("fault_host_recovery_snapshot_directory_invalid")
        descriptor = os.open(
            resolved,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or not stat.S_ISDIR(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o700
            or opened.st_uid != os.geteuid()
            or opened.st_gid != os.getegid()
            or set(os.listdir(descriptor)) - {name}
        ):
            raise FaultControllerHostError("fault_host_recovery_snapshot_directory_invalid")
        result = descriptor
        descriptor = -1
        return result
    except FaultControllerHostError:
        raise
    except OSError as error:
        raise FaultControllerHostError("fault_host_recovery_snapshot_directory_invalid") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _require_snapshot_status(status: os.stat_result, expected_size: int) -> None:
    if (
        not stat.S_ISREG(status.st_mode)
        or stat.S_IMODE(status.st_mode) != 0o600
        or status.st_uid != os.geteuid()
        or status.st_gid != os.getegid()
        or status.st_nlink != 1
        or status.st_size != expected_size
    ):
        raise FaultControllerHostError("fault_host_recovery_snapshot_invalid")


def _read_existing(directory_descriptor: int, name: str, expected: bytes) -> None:
    descriptor = -1
    try:
        before = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_descriptor,
        )
        opened = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise FaultControllerHostError("fault_host_recovery_snapshot_invalid")
        _require_snapshot_status(opened, len(expected))
        chunks: list[bytes] = []
        total = 0
        while total <= MAXIMUM_SNAPSHOT_BYTES:
            chunk = os.read(
                descriptor,
                min(4096, MAXIMUM_SNAPSHOT_BYTES + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        body = b"".join(chunks)
        after = os.fstat(descriptor)
        if body != expected or (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_nlink,
        ) != (
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
            opened.st_nlink,
        ):
            raise FaultControllerHostError("fault_host_recovery_snapshot_invalid")
    except FaultControllerHostError:
        raise
    except OSError as error:
        raise FaultControllerHostError("fault_host_recovery_snapshot_invalid") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _snapshot_identity(
    configuration: FaultHostConfiguration,
) -> tuple[bytes, str, str]:
    body = _snapshot_body(configuration)
    digest = hashlib.sha256(body).hexdigest()
    name = f"fault-recovery-{configuration.manifest.run_id.hex}-{digest}.compose.json"
    if _SNAPSHOT_NAME.fullmatch(name) is None:
        raise FaultControllerHostError("fault_host_recovery_snapshot_invalid")
    return body, digest, name


def validate_fault_recovery_compose_snapshot(
    configuration: FaultHostConfiguration,
    snapshot: FaultRecoveryComposeSnapshot,
) -> FaultRecoveryComposeSnapshot:
    """手作りidentityを信頼せず、期待contentとanchored fileを再照合する。"""

    if (
        not isinstance(snapshot, FaultRecoveryComposeSnapshot)
        or not isinstance(snapshot.path, Path)
        or not isinstance(snapshot.sha256, str)
        or type(snapshot.size_bytes) is not int
    ):
        raise FaultControllerHostError("fault_host_recovery_snapshot_invalid")
    body, digest, name = _snapshot_identity(configuration)
    expected_path = snapshot.path.parent / name
    if (
        snapshot.path != expected_path
        or snapshot.sha256 != digest
        or snapshot.size_bytes != len(body)
    ):
        raise FaultControllerHostError("fault_host_recovery_snapshot_invalid")
    directory_descriptor = _open_snapshot_directory(
        snapshot.path.parent,
        configuration.repository_root,
        name,
    )
    try:
        _read_existing(directory_descriptor, name, body)
    finally:
        os.close(directory_descriptor)
    return FaultRecoveryComposeSnapshot(
        path=snapshot.path,
        sha256=digest,
        size_bytes=len(body),
    )


def seal_fault_recovery_compose_snapshot(
    configuration: FaultHostConfiguration,
    directory: Path,
) -> FaultRecoveryComposeSnapshot:
    """最小ComposeをO_EXCLでsealし、同一bytesだけをresumeとして受理する。"""

    body, digest, name = _snapshot_identity(configuration)
    directory_descriptor = _open_snapshot_directory(
        directory,
        configuration.repository_root,
        name,
    )
    descriptor = -1
    created = False
    try:
        try:
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory_descriptor,
            )
            created = True
            os.fchmod(descriptor, 0o600)
            written = 0
            while written < len(body):
                count = os.write(descriptor, body[written:])
                if count <= 0:
                    raise OSError
                written += count
            os.fsync(descriptor)
            _require_snapshot_status(os.fstat(descriptor), len(body))
            os.close(descriptor)
            descriptor = -1
            os.fsync(directory_descriptor)
        except FileExistsError:
            _read_existing(directory_descriptor, name, body)
        snapshot = FaultRecoveryComposeSnapshot(
            path=directory / name,
            sha256=digest,
            size_bytes=len(body),
        )
        return validate_fault_recovery_compose_snapshot(configuration, snapshot)
    except FaultControllerHostError:
        if created:
            _unlink_created(directory_descriptor, name)
        raise
    except OSError as error:
        if created:
            _unlink_created(directory_descriptor, name)
        raise FaultControllerHostError("fault_host_recovery_snapshot_write_failed") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_descriptor)


def _unlink_created(directory_descriptor: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)
    except OSError:
        pass


def decode_fault_recovery_compose_snapshot(body: bytes) -> dict[str, object]:
    """testと監査がsnapshotのcanonical documentを確認するための限定seam。"""

    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FaultControllerHostError("fault_host_recovery_snapshot_invalid") from error
    if not isinstance(value, dict) or canonical_json_bytes(value) + b"\n" != body:
        raise FaultControllerHostError("fault_host_recovery_snapshot_invalid")
    return value
