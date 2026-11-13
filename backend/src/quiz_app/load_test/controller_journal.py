from __future__ import annotations

import fcntl
import os
import re
import stat
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from types import TracebackType
from uuid import UUID

from pydantic import ValidationError

from quiz_app.load_test.controller_contracts import (
    ControllerCheckpoint,
    ControllerJournalRecord,
    ControllerRefusal,
    JournalPayload,
    canonical_json_bytes,
)
from quiz_app.load_test.verification_contracts import ArtifactIdentity

MAX_JOURNAL_BYTES = 2_000_000
MAX_JOURNAL_RECORDS = 1_000
MAX_CHECKPOINT_BYTES = 1_000_000
JOURNAL_FORMAT = "load-test-controller-journal-v2"


CHECKPOINT_NAME_PATTERN = re.compile(r"^controller-round-[0-9]{2}-[0-9a-f-]{36}\.json$")


def require_secure_artifact_directory(
    path: Path,
    repository_root: Path,
    *,
    allowed_names: set[str] | None = None,
    allow_checkpoints: bool = False,
) -> int:
    """外部0700 directoryを検証し、callerがcloseするanchored descriptorを返す。"""

    try:
        before = path.lstat()
        resolved_repository = repository_root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
    except OSError as error:
        raise ControllerRefusal("controller_artifact_directory_invalid") from error
    if (
        not path.is_absolute()
        or ".." in path.parts
        or resolved_path != path
        or resolved_path.is_relative_to(resolved_repository)
        or resolved_repository.is_relative_to(resolved_path)
    ):
        raise ControllerRefusal("controller_artifact_directory_invalid")
    for ancestor in (path, *path.parents):
        try:
            ancestor_status = ancestor.lstat()
        except OSError as error:
            raise ControllerRefusal("controller_artifact_directory_invalid") from error
        is_sticky_directory = stat.S_ISDIR(ancestor_status.st_mode) and bool(
            ancestor_status.st_mode & stat.S_ISVTX
        )
        if (
            ancestor.is_symlink()
            or ancestor_status.st_uid not in {0, os.geteuid()}
            or (ancestor_status.st_mode & 0o022 and not is_sticky_directory)
        ):
            raise ControllerRefusal("controller_artifact_directory_invalid")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ControllerRefusal("controller_artifact_directory_invalid") from error
    status = os.fstat(descriptor)
    if (
        (before.st_dev, before.st_ino) != (status.st_dev, status.st_ino)
        or stat.S_ISLNK(before.st_mode)
        or not stat.S_ISDIR(status.st_mode)
        or stat.S_IMODE(status.st_mode) != 0o700
        or status.st_uid != os.geteuid()
        or status.st_gid != os.getegid()
    ):
        os.close(descriptor)
        raise ControllerRefusal("controller_artifact_directory_invalid")
    names = set(os.listdir(descriptor))
    permitted = allowed_names or set()
    checkpoints_are_valid = allow_checkpoints and all(
        CHECKPOINT_NAME_PATTERN.fullmatch(name) is not None
        and _require_secure_file_status(
            os.stat(name, dir_fd=descriptor, follow_symlinks=False),
            MAX_CHECKPOINT_BYTES,
        )
        for name in names
    )
    if names - permitted and not checkpoints_are_valid:
        os.close(descriptor)
        raise ControllerRefusal("controller_artifact_directory_inventory_invalid")
    return descriptor


def _require_secure_file_status(status: os.stat_result, max_bytes: int) -> bool:
    return (
        stat.S_ISREG(status.st_mode)
        and stat.S_IMODE(status.st_mode) == 0o600
        and status.st_uid == os.geteuid()
        and status.st_gid == os.getegid()
        and status.st_nlink == 1
        and 0 < status.st_size <= max_bytes
    )


def _read_secure_checkpoint(
    path: Path,
    repository_root: Path,
) -> tuple[bytes, ArtifactIdentity]:
    """既存checkpointだけをsymlink/hardlink/TOCTOU耐性付きで読む。"""

    directory_descriptor = require_secure_artifact_directory(
        path.parent,
        repository_root,
        allow_checkpoints=True,
    )
    descriptor = -1
    try:
        try:
            before = os.stat(path.name, dir_fd=directory_descriptor, follow_symlinks=False)
            descriptor = os.open(
                path.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_descriptor,
            )
        except OSError as error:
            raise ControllerRefusal("controller_artifact_invalid") from error
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (
            after.st_dev,
            after.st_ino,
        ) or not _require_secure_file_status(after, MAX_CHECKPOINT_BYTES):
            raise ControllerRefusal("controller_artifact_invalid")
        body = os.read(descriptor, MAX_CHECKPOINT_BYTES + 1)
        if len(body) != after.st_size:
            raise ControllerRefusal("controller_artifact_invalid")
        return body, ArtifactIdentity(
            name=path.name,
            sha256=sha256(body).hexdigest(),
            size_bytes=len(body),
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_descriptor)


def _write_new_file(directory_descriptor: int, name: str, body: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_descriptor)
    try:
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            os.fchmod(output.fileno(), 0o600)
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        try:
            os.unlink(name, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def seal_checkpoint(
    directory: Path,
    checkpoint: ControllerCheckpoint,
    repository_root: Path,
) -> ArtifactIdentity:
    """checkpointを非上書きでsealし、同一内容だけcrash recoveryとして受理する。"""

    directory_descriptor = require_secure_artifact_directory(
        directory,
        repository_root,
        allow_checkpoints=True,
    )
    name = f"controller-round-{checkpoint.round_number:02d}-{checkpoint.round_id}.json"
    body = canonical_json_bytes(checkpoint) + b"\n"
    try:
        try:
            _write_new_file(directory_descriptor, name, body)
            os.fsync(directory_descriptor)
        except FileExistsError:
            existing, identity = _read_secure_checkpoint(directory / name, repository_root)
            if existing != body:
                raise ControllerRefusal("controller_checkpoint_collision") from None
            return identity
        return ArtifactIdentity(name=name, sha256=sha256(body).hexdigest(), size_bytes=len(body))
    except OSError as error:
        raise ControllerRefusal("controller_checkpoint_write_failed") from error
    finally:
        os.close(directory_descriptor)


def load_checkpoint(
    directory: Path,
    artifact: ArtifactIdentity,
    repository_root: Path,
) -> ControllerCheckpoint:
    """journalが参照するcheckpointの実体・digest・canonical bodyを再検証する。"""

    if CHECKPOINT_NAME_PATTERN.fullmatch(artifact.name) is None:
        raise ControllerRefusal("controller_checkpoint_invalid")
    body, actual = _read_secure_checkpoint(directory / artifact.name, repository_root)
    if actual != artifact:
        raise ControllerRefusal("controller_checkpoint_invalid")
    try:
        checkpoint = ControllerCheckpoint.model_validate_json(body)
    except ValidationError as error:
        raise ControllerRefusal("controller_checkpoint_invalid") from error
    if canonical_json_bytes(checkpoint) + b"\n" != body:
        raise ControllerRefusal("controller_checkpoint_invalid")
    return checkpoint


class ControllerJournal:
    """process lifetime lockを保持するappend-only hash-chain journal。"""

    def __init__(self, path: Path, repository_root: Path) -> None:
        self.path = path
        self._repository_root = repository_root
        self.records: list[ControllerJournalRecord] = []
        self._descriptor = -1
        self._directory_descriptor = -1

    @property
    def head_sha256(self) -> str | None:
        return self.records[-1].entry_sha256 if self.records else None

    def __enter__(self) -> ControllerJournal:
        self._directory_descriptor = require_secure_artifact_directory(
            self.path.parent,
            self._repository_root,
            allowed_names={self.path.name},
        )
        flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        created = False
        try:
            self._descriptor = os.open(
                self.path.name,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=self._directory_descriptor,
            )
            created = True
        except FileExistsError:
            try:
                self._descriptor = os.open(
                    self.path.name,
                    flags,
                    dir_fd=self._directory_descriptor,
                )
            except OSError as error:
                self._close()
                raise ControllerRefusal("controller_journal_invalid") from error
        try:
            fcntl.flock(self._descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self._close()
            raise ControllerRefusal("controller_journal_locked") from error
        try:
            status = os.fstat(self._descriptor)
            if created:
                os.fchmod(self._descriptor, 0o600)
                os.fsync(self._descriptor)
                os.fsync(self._directory_descriptor)
            if (
                not stat.S_ISREG(status.st_mode)
                or stat.S_IMODE(os.fstat(self._descriptor).st_mode) != 0o600
                or status.st_uid != os.geteuid()
                or status.st_gid != os.getegid()
                or status.st_nlink != 1
                or status.st_size > MAX_JOURNAL_BYTES
            ):
                raise ControllerRefusal("controller_journal_invalid")
            self.records = self._read_and_validate()
            return self
        except (OSError, ValidationError, ControllerRefusal) as error:
            self._close()
            if isinstance(error, ControllerRefusal):
                raise
            raise ControllerRefusal("controller_journal_invalid") from error

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self._close()

    def _close(self) -> None:
        if self._descriptor >= 0:
            os.close(self._descriptor)
            self._descriptor = -1
        if self._directory_descriptor >= 0:
            os.close(self._directory_descriptor)
            self._directory_descriptor = -1

    def _read_and_validate(self) -> list[ControllerJournalRecord]:
        os.lseek(self._descriptor, 0, os.SEEK_SET)
        body = os.read(self._descriptor, MAX_JOURNAL_BYTES + 1)
        if len(body) > MAX_JOURNAL_BYTES or (body and not body.endswith(b"\n")):
            raise ControllerRefusal("controller_journal_invalid")
        lines = body.splitlines()
        if len(lines) > MAX_JOURNAL_RECORDS:
            raise ControllerRefusal("controller_journal_invalid")
        records: list[ControllerJournalRecord] = []
        previous: str | None = None
        run_id: UUID | None = None
        event_id: UUID | None = None
        for sequence, line in enumerate(lines, start=1):
            record = ControllerJournalRecord.model_validate_json(line)
            unsigned = record.model_dump(mode="json", exclude={"entry_sha256"})
            expected_hash = sha256(canonical_json_bytes(unsigned)).hexdigest()
            if (
                record.sequence != sequence
                or record.previous_sha256 != previous
                or record.entry_sha256 != expected_hash
                or (run_id is not None and record.run_id != run_id)
                or (event_id is not None and record.event_id != event_id)
            ):
                raise ControllerRefusal("controller_journal_chain_invalid")
            run_id, event_id, previous = record.run_id, record.event_id, record.entry_sha256
            records.append(record)
        os.lseek(self._descriptor, 0, os.SEEK_END)
        return records

    def append(
        self,
        *,
        run_id: UUID,
        event_id: UUID,
        payload: JournalPayload,
        recorded_at: datetime | None = None,
    ) -> ControllerJournalRecord:
        if self._descriptor < 0:
            raise ControllerRefusal("controller_journal_not_open")
        unsigned: dict[str, object] = {
            "format": JOURNAL_FORMAT,
            "sequence": len(self.records) + 1,
            "previous_sha256": self.head_sha256,
            "recorded_at": recorded_at or datetime.now(UTC),
            "run_id": run_id,
            "event_id": event_id,
            "payload": payload,
        }
        entry_sha256 = sha256(canonical_json_bytes(unsigned)).hexdigest()
        record = ControllerJournalRecord.model_validate({**unsigned, "entry_sha256": entry_sha256})
        serialized = canonical_json_bytes(record) + b"\n"
        if os.fstat(self._descriptor).st_size + len(serialized) > MAX_JOURNAL_BYTES:
            raise ControllerRefusal("controller_journal_full")
        written = os.write(self._descriptor, serialized)
        if written != len(serialized):
            raise ControllerRefusal("controller_journal_write_failed")
        os.fsync(self._descriptor)
        self.records.append(record)
        return record
