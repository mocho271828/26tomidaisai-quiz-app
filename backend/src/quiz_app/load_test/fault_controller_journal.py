"""障害注入専用のappend-only hash-chain journal。"""

from __future__ import annotations

import fcntl
import hmac
import os
import stat
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from types import TracebackType

from pydantic import ValidationError

from quiz_app.load_test.controller_contracts import ControllerRefusal, canonical_json_bytes
from quiz_app.load_test.controller_journal import require_secure_artifact_directory
from quiz_app.load_test.fault_controller_contracts import (
    FaultControllerIdentity,
    FaultControllerJournalRecord,
    FaultInitializedPayload,
    FaultJournalPayload,
)

FAULT_JOURNAL_FORMAT = "load-test-fault-controller-journal-v1"
MAX_FAULT_JOURNAL_BYTES = 1_000_000
MAX_FAULT_JOURNAL_RECORDS = 256


def fault_controller_identity_sha256(identity: FaultControllerIdentity) -> str:
    """journal全recordへ固定する、秘密を含まない実行identityのdigest。"""

    return sha256(canonical_json_bytes(identity)).hexdigest()


def _is_secure_journal_file(status: os.stat_result) -> bool:
    return (
        stat.S_ISREG(status.st_mode)
        and stat.S_IMODE(status.st_mode) == 0o600
        and status.st_uid == os.geteuid()
        and status.st_gid == os.getegid()
        and status.st_nlink == 1
        and status.st_size <= MAX_FAULT_JOURNAL_BYTES
    )


class FaultControllerJournal:
    """検証済みidentityとprocess lifetime lockを保持するfault journal。"""

    def __init__(
        self,
        path: Path,
        repository_root: Path,
        *,
        identity: FaultControllerIdentity,
    ) -> None:
        self.path = path
        self.identity = FaultControllerIdentity.model_validate(identity)
        self.identity_sha256 = fault_controller_identity_sha256(self.identity)
        self._repository_root = repository_root
        self._records: list[FaultControllerJournalRecord] = []
        self._descriptor = -1
        self._directory_descriptor = -1
        self._validated_size = 0

    @property
    def records(self) -> tuple[FaultControllerJournalRecord, ...]:
        return tuple(self._records)

    @property
    def head_sha256(self) -> str | None:
        return self._records[-1].entry_sha256 if self._records else None

    def __enter__(self) -> FaultControllerJournal:
        try:
            self._directory_descriptor = require_secure_artifact_directory(
                self.path.parent,
                self._repository_root,
                allowed_names={self.path.name},
            )
        except ControllerRefusal as error:
            raise ControllerRefusal("fault_controller_journal_directory_invalid") from error
        flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        created = False
        try:
            try:
                self._descriptor = os.open(
                    self.path.name,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=self._directory_descriptor,
                )
                created = True
            except FileExistsError:
                self._descriptor = os.open(
                    self.path.name,
                    flags,
                    dir_fd=self._directory_descriptor,
                )
            try:
                fcntl.flock(self._descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise ControllerRefusal("fault_controller_journal_locked") from error
            if created:
                os.fchmod(self._descriptor, 0o600)
                os.fsync(self._descriptor)
                os.fsync(self._directory_descriptor)
            self._require_current_file(expected_size=None)
            self._records = self._read_and_validate()
            return self
        except (OSError, ValidationError) as error:
            self._close()
            raise ControllerRefusal("fault_controller_journal_invalid") from error
        except ControllerRefusal:
            self._close()
            raise

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

    def _require_current_file(self, *, expected_size: int | None) -> os.stat_result:
        if self._descriptor < 0 or self._directory_descriptor < 0:
            raise ControllerRefusal("fault_controller_journal_not_open")
        try:
            descriptor_status = os.fstat(self._descriptor)
            path_status = os.stat(
                self.path.name,
                dir_fd=self._directory_descriptor,
                follow_symlinks=False,
            )
        except OSError as error:
            raise ControllerRefusal("fault_controller_journal_invalid") from error
        if (
            not _is_secure_journal_file(descriptor_status)
            or not _is_secure_journal_file(path_status)
            or (descriptor_status.st_dev, descriptor_status.st_ino)
            != (path_status.st_dev, path_status.st_ino)
            or (expected_size is not None and descriptor_status.st_size != expected_size)
        ):
            raise ControllerRefusal("fault_controller_journal_invalid")
        return descriptor_status

    def _read_and_validate(self) -> list[FaultControllerJournalRecord]:
        status = self._require_current_file(expected_size=None)
        os.lseek(self._descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = status.st_size
        while remaining:
            chunk = os.read(self._descriptor, min(64 * 1024, remaining))
            if not chunk:
                raise ControllerRefusal("fault_controller_journal_invalid")
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        if len(body) != status.st_size or (body and not body.endswith(b"\n")):
            raise ControllerRefusal("fault_controller_journal_invalid")
        lines = body.splitlines()
        if len(lines) > MAX_FAULT_JOURNAL_RECORDS:
            raise ControllerRefusal("fault_controller_journal_invalid")
        records: list[FaultControllerJournalRecord] = []
        previous_sha256: str | None = None
        for sequence, line in enumerate(lines, start=1):
            record = FaultControllerJournalRecord.model_validate_json(line)
            if canonical_json_bytes(record) != line:
                raise ControllerRefusal("fault_controller_journal_invalid")
            unsigned = record.model_dump(mode="json", exclude={"entry_sha256"})
            expected_hash = sha256(canonical_json_bytes(unsigned)).hexdigest()
            if (
                record.sequence != sequence
                or record.previous_sha256 != previous_sha256
                or not hmac.compare_digest(record.entry_sha256, expected_hash)
                or record.run_id != self.identity.context.run_id
                or record.event_id != self.identity.context.event_id
                or not hmac.compare_digest(record.identity_sha256, self.identity_sha256)
            ):
                raise ControllerRefusal("fault_controller_journal_chain_invalid")
            if isinstance(record.payload, FaultInitializedPayload) and not hmac.compare_digest(
                fault_controller_identity_sha256(record.payload.identity),
                self.identity_sha256,
            ):
                raise ControllerRefusal("fault_controller_journal_identity_invalid")
            records.append(record)
            previous_sha256 = record.entry_sha256
        if records and not isinstance(records[0].payload, FaultInitializedPayload):
            raise ControllerRefusal("fault_controller_journal_identity_invalid")
        self._validated_size = len(body)
        self._require_current_file(expected_size=self._validated_size)
        os.lseek(self._descriptor, 0, os.SEEK_END)
        return records

    def append(
        self,
        payload: FaultJournalPayload,
        *,
        identity: FaultControllerIdentity | None = None,
        recorded_at: datetime | None = None,
    ) -> FaultControllerJournalRecord:
        """一recordを永続化し、fsync完了後だけmemory上へ公開する。"""

        if self._descriptor < 0:
            raise ControllerRefusal("fault_controller_journal_not_open")
        if identity is not None and not hmac.compare_digest(
            fault_controller_identity_sha256(identity),
            self.identity_sha256,
        ):
            raise ControllerRefusal("fault_controller_journal_identity_invalid")
        if len(self._records) >= MAX_FAULT_JOURNAL_RECORDS:
            raise ControllerRefusal("fault_controller_journal_full")
        if not self._records:
            if not isinstance(payload, FaultInitializedPayload):
                raise ControllerRefusal("fault_controller_journal_identity_required")
            if not hmac.compare_digest(
                fault_controller_identity_sha256(payload.identity),
                self.identity_sha256,
            ):
                raise ControllerRefusal("fault_controller_journal_identity_invalid")
        elif isinstance(payload, FaultInitializedPayload):
            raise ControllerRefusal("fault_controller_journal_identity_duplicate")
        self._require_current_file(expected_size=self._validated_size)
        timestamp = recorded_at or datetime.now(UTC)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ControllerRefusal("fault_controller_journal_time_invalid")
        timestamp = timestamp.astimezone(UTC)
        if self._records and timestamp < self._records[-1].recorded_at:
            raise ControllerRefusal("fault_controller_journal_time_reversed")
        unsigned: dict[str, object] = {
            "format": FAULT_JOURNAL_FORMAT,
            "sequence": len(self._records) + 1,
            "previous_sha256": self.head_sha256,
            "recorded_at": timestamp,
            "run_id": self.identity.context.run_id,
            "event_id": self.identity.context.event_id,
            "identity_sha256": self.identity_sha256,
            "payload": payload,
        }
        entry_sha256 = sha256(canonical_json_bytes(unsigned)).hexdigest()
        try:
            record = FaultControllerJournalRecord.model_validate(
                {**unsigned, "entry_sha256": entry_sha256}
            )
            serialized = canonical_json_bytes(record) + b"\n"
            expected_size = self._validated_size + len(serialized)
            if expected_size > MAX_FAULT_JOURNAL_BYTES:
                raise ControllerRefusal("fault_controller_journal_full")
            written = 0
            while written < len(serialized):
                count = os.write(self._descriptor, serialized[written:])
                if count <= 0:
                    raise ControllerRefusal("fault_controller_journal_write_failed")
                written += count
            os.fsync(self._descriptor)
            self._require_current_file(expected_size=expected_size)
        except (OSError, ValidationError) as error:
            raise ControllerRefusal("fault_controller_journal_write_failed") from error
        self._validated_size = expected_size
        self._records.append(record)
        return record
