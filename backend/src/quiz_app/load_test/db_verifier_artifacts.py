from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

import logfire
from pydantic import BaseModel, ValidationError

from quiz_app.load_test.controller_contracts import canonical_json_bytes
from quiz_app.load_test.db_verifier_contracts import DB_VERIFIER_REQUEST_ADAPTER
from quiz_app.load_test.db_verifier_models import (
    DbVerifierError,
    DbVerifierRunResult,
    FinalVerificationReport,
    LoadedDbVerifierRequest,
    PerRoundVerificationReport,
)
from quiz_app.load_test.verification_contracts import ArtifactIdentity, RoundVerificationReceipt

MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_SECURE_FILE_BYTES = 256 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
_SAFE_ARTIFACT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")


def load_db_verifier_request(path: Path) -> LoadedDbVerifierRequest:
    """controller外部artifactと同じ0600/single-link境界からstrict入力を読む。"""

    body, identity = read_secure_external_file(path)
    try:
        value = DB_VERIFIER_REQUEST_ADAPTER.validate_json(body, strict=True)
    except ValidationError:
        raise DbVerifierError("db_verifier_input_invalid") from None
    if body != canonical_json_bytes(value) + b"\n":
        raise DbVerifierError("db_verifier_input_invalid")
    return LoadedDbVerifierRequest(value=value, artifact=identity)


def seal_bound_report(
    report: PerRoundVerificationReport | FinalVerificationReport,
    output_directory: Path,
) -> DbVerifierRunResult:
    """canonical reportと、PASS時のcontroller-compatible receiptをsealする。"""

    suffix = "final" if report.mode == "final" else f"round-{report.round_number:02d}"
    report_name = f"db-verification-{suffix}-report-{report.verification_id}.json"
    report_artifact = write_canonical_artifact(output_directory, report_name, report)
    receipt_artifact: ArtifactIdentity | None = None
    if report.decision == "PASS":
        receipt = RoundVerificationReceipt(
            format="load-test-round-verification-receipt-v2",
            verification_id=report.verification_id,
            run_id=report.run_id,
            event_id=report.event_id,
            source_commit=report.source_commit,
            schema_revision=report.schema_revision,
            manifest_sha256=report.manifest_sha256,
            scenario_sha256=report.scenario_sha256,
            round_number=report.round_number,
            round_id=report.round_id,
            question_id=report.question_id,
            verification_requested_head_sha256=report.verification_requested_head_sha256,
            generator_evidence=report.generator_evidence,
            report=report_artifact,
            decision="PASS",
            failure_codes=(),
            sealed_at=report.sealed_at,
        )
        receipt_name = f"db-verification-{suffix}-receipt-{report.verification_id}.json"
        receipt_artifact = write_canonical_artifact(output_directory, receipt_name, receipt)
    logfire.info(
        "load-test DB verification sealed",
        mode=report.mode,
        round_number=report.round_number,
        decision=report.decision,
    )
    return DbVerifierRunResult(
        mode=report.mode,
        decision=report.decision,
        report=report_artifact,
        receipt=receipt_artifact,
    )


def runtime_repository_root() -> Path:
    """installed layoutからrepository rootをsentinelで特定する。"""

    module = Path(__file__).resolve(strict=True)
    layouts = (
        (Path("backend/pyproject.toml"), Path("compose.yaml")),
        (Path("pyproject.toml"), Path("alembic.ini")),
    )
    for sentinels in layouts:
        for candidate in module.parents:
            if all((candidate / sentinel).is_file() for sentinel in sentinels):
                return candidate.resolve(strict=True)
    raise DbVerifierError("db_verifier_runtime_root_invalid")


def _require_no_directory_symlink(directory: Path) -> None:
    try:
        for ancestor in reversed((directory, *directory.parents)):
            status = ancestor.lstat()
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
                raise DbVerifierError("db_verifier_artifact_directory_invalid")
    except OSError:
        raise DbVerifierError("db_verifier_artifact_directory_invalid") from None


def _open_external_directory(directory: Path) -> int:
    if not directory.is_absolute() or ".." in directory.parts:
        raise DbVerifierError("db_verifier_artifact_directory_invalid")
    _require_no_directory_symlink(directory)
    try:
        resolved = directory.resolve(strict=True)
        if resolved.is_relative_to(runtime_repository_root()):
            raise DbVerifierError("db_verifier_artifact_must_be_external")
        descriptor = os.open(
            resolved,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        status = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(status.st_mode)
            or stat.S_IMODE(status.st_mode) != 0o700
            or status.st_uid != os.geteuid()
            or status.st_gid != os.getegid()
        ):
            os.close(descriptor)
            raise DbVerifierError("db_verifier_artifact_directory_invalid")
        return descriptor
    except DbVerifierError:
        raise
    except OSError:
        raise DbVerifierError("db_verifier_artifact_directory_invalid") from None


def read_secure_external_file(
    path: Path, *, maximum_bytes: int = MAX_INPUT_BYTES
) -> tuple[bytes, ArtifactIdentity]:
    """TOCTOUとlink攻撃を拒否し、秘密値を出さず外部artifactを読む。"""

    if type(maximum_bytes) is not int or not 1 <= maximum_bytes <= MAX_SECURE_FILE_BYTES:
        raise DbVerifierError("db_verifier_input_size_limit_invalid")
    if (
        not path.is_absolute()
        or ".." in path.parts
        or _SAFE_ARTIFACT_NAME.fullmatch(path.name) is None
    ):
        raise DbVerifierError("db_verifier_input_path_invalid")
    directory_descriptor = _open_external_directory(path.parent)
    descriptor = -1
    try:
        before = os.stat(path.name, dir_fd=directory_descriptor, follow_symlinks=False)
        descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_descriptor,
        )
        opened = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_uid != os.geteuid()
            or opened.st_gid != os.getegid()
            or opened.st_nlink != 1
            or not 1 <= opened.st_size <= maximum_bytes
        ):
            raise DbVerifierError("db_verifier_input_file_invalid")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            body = source.read(maximum_bytes + 1)
        after = os.fstat(descriptor)
        if len(body) != opened.st_size or (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_nlink,
        ) != (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns, opened.st_nlink):
            raise DbVerifierError("db_verifier_input_changed")
        return body, ArtifactIdentity(
            name=path.name,
            sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
        )
    except DbVerifierError:
        raise
    except OSError:
        raise DbVerifierError("db_verifier_input_read_failed") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_descriptor)


def write_canonical_artifact(directory: Path, name: str, value: BaseModel) -> ArtifactIdentity:
    """O_EXCL/0600/fsyncでcanonical JSON artifactを新規sealする。"""

    if _SAFE_ARTIFACT_NAME.fullmatch(name) is None:
        raise DbVerifierError("db_verifier_artifact_name_invalid")
    body = canonical_json_bytes(value) + b"\n"
    if not 1 <= len(body) <= MAX_OUTPUT_BYTES:
        raise DbVerifierError("db_verifier_artifact_size_invalid")
    directory_descriptor = _open_external_directory(directory)
    descriptor = -1
    created = False
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
        created = True
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            os.fchmod(output.fileno(), 0o600)
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
            written = os.fstat(output.fileno())
            if (
                not stat.S_ISREG(written.st_mode)
                or stat.S_IMODE(written.st_mode) != 0o600
                or written.st_uid != os.geteuid()
                or written.st_gid != os.getegid()
                or written.st_nlink != 1
                or written.st_size != len(body)
            ):
                raise DbVerifierError("db_verifier_artifact_file_invalid")
        os.fsync(directory_descriptor)
    except FileExistsError:
        raise DbVerifierError("db_verifier_artifact_collision") from None
    except DbVerifierError:
        if created:
            _unlink_created(directory_descriptor, name)
        raise
    except OSError:
        if created:
            _unlink_created(directory_descriptor, name)
        raise DbVerifierError("db_verifier_artifact_write_failed") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_descriptor)
    return ArtifactIdentity(
        name=name,
        sha256=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
    )


def _unlink_created(directory_descriptor: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)
    except OSError:
        pass
