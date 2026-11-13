import json
import os
import stat
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest

from quiz_app.load_test import controller_journal as journal_module
from quiz_app.load_test.controller_contracts import (
    ControllerCheckpoint,
    ControllerContext,
    ControllerRefusal,
    InitializedPayload,
    canonical_json_bytes,
)
from quiz_app.load_test.controller_journal import (
    ControllerJournal,
    load_checkpoint,
    seal_checkpoint,
)
from quiz_app.load_test.verification_contracts import ArtifactIdentity

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RUN_ID = UUID("10000000-0000-4000-8000-000000000001")
EVENT_ID = UUID("10000000-0000-4000-8000-000000000002")
ROUND_ID = UUID("10000000-0000-4000-8000-000000000003")
QUESTION_ID = UUID("10000000-0000-4000-8000-000000000004")
ACCOUNT_ID = UUID("10000000-0000-4000-8000-000000000005")
IDEMPOTENCY_KEY = UUID("10000000-0000-4000-8000-000000000006")
VERIFICATION_ID = UUID("10000000-0000-4000-8000-000000000007")
NEXT_ROUND_ID = UUID("10000000-0000-4000-8000-000000000008")
RECORDED_AT = datetime(2026, 9, 3, 1, 2, 3, tzinfo=UTC)
SOURCE_COMMIT = "a" * 40
MANIFEST_SHA256 = "b" * 64
SCENARIO_SHA256 = "c" * 64
JOURNAL_HEAD_SHA256 = "d" * 64
VERIFICATION_HEAD_SHA256 = "e" * 64
RESPONSE_SHA256 = "f" * 64


def _secure_directory(base: Path, name: str = "controller-artifacts") -> Path:
    directory = base / name
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return directory


def _artifact(
    name: str, digest_character: str, size_bytes: int = 41
) -> ArtifactIdentity:
    return ArtifactIdentity(
        name=name,
        sha256=digest_character * 64,
        size_bytes=size_bytes,
    )


def _context() -> ControllerContext:
    return ControllerContext(
        run_id=RUN_ID,
        event_id=EVENT_ID,
        event_slug="load-test-2026-09-03",
        public_origin="https://quiz.example.test",
        source_commit=SOURCE_COMMIT,
        schema_revision="schema-v1",
        scheduled_start_at=RECORDED_AT,
        manifest_sha256=MANIFEST_SHA256,
        scenario_sha256=SCENARIO_SHA256,
    )


def _initialized_payload() -> InitializedPayload:
    return InitializedPayload(
        kind="initialized",
        context=_context(),
        operator_account_id=ACCOUNT_ID,
        server_time=RECORDED_AT,
        event_version=1,
        round_version=1,
    )


def _checkpoint(**updates: object) -> ControllerCheckpoint:
    checkpoint = ControllerCheckpoint(
        format="load-test-controller-checkpoint-v2",
        run_id=RUN_ID,
        event_id=EVENT_ID,
        source_commit=SOURCE_COMMIT,
        schema_revision="schema-v1",
        manifest_sha256=MANIFEST_SHA256,
        scenario_sha256=SCENARIO_SHA256,
        round_number=1,
        round_id=ROUND_ID,
        question_id=QUESTION_ID,
        journal_head_sha256=JOURNAL_HEAD_SHA256,
        verification_requested_head_sha256=VERIFICATION_HEAD_SHA256,
        verification_request_artifact=_artifact("verification-request.json", "5"),
        receipt_artifact=_artifact("receipt.json", "1"),
        verification_id=VERIFICATION_ID,
        report=_artifact("verification-report.json", "2"),
        generator_evidence=_artifact("generator-round-01.json", "3"),
        completed_operation="advance_round",
        idempotency_key=IDEMPOTENCY_KEY,
        response_sha256=RESPONSE_SHA256,
        next_round_id=NEXT_ROUND_ID,
        sealed_at=RECORDED_AT,
    )
    return checkpoint.model_copy(update=updates)


def _append_record(path: Path) -> None:
    with ControllerJournal(path, REPOSITORY_ROOT) as journal:
        journal.append(
            run_id=RUN_ID,
            event_id=EVENT_ID,
            payload=_initialized_payload(),
            recorded_at=RECORDED_AT,
        )


def _append_two_records(path: Path) -> None:
    with ControllerJournal(path, REPOSITORY_ROOT) as journal:
        for second in range(2):
            journal.append(
                run_id=RUN_ID,
                event_id=EVENT_ID,
                payload=_initialized_payload(),
                recorded_at=RECORDED_AT.replace(second=second),
            )


def _journal_json_records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _write_journal_json_records(path: Path, records: list[dict[str, object]]) -> None:
    body = b"\n".join(canonical_json_bytes(record) for record in records) + b"\n"
    path.write_bytes(body)
    path.chmod(0o600)


def _rehash_record(record: dict[str, object]) -> None:
    unsigned = {key: value for key, value in record.items() if key != "entry_sha256"}
    record["entry_sha256"] = sha256(canonical_json_bytes(unsigned)).hexdigest()


def _checkpoint_path(directory: Path, checkpoint: ControllerCheckpoint) -> Path:
    return (
        directory
        / f"controller-round-{checkpoint.round_number:02d}-{checkpoint.round_id}.json"
    )


def _write_checkpoint_body(
    directory: Path,
    checkpoint: ControllerCheckpoint,
    body: bytes,
) -> ArtifactIdentity:
    path = _checkpoint_path(directory, checkpoint)
    path.write_bytes(body)
    path.chmod(0o600)
    return ArtifactIdentity(
        name=path.name,
        sha256=sha256(body).hexdigest(),
        size_bytes=len(body),
    )


def test_external_journal_is_created_0600_and_hash_chain_survives_reopen(
    tmp_path: Path,
) -> None:
    directory = _secure_directory(tmp_path)
    path = directory / "controller.journal"

    with ControllerJournal(path, REPOSITORY_ROOT) as journal:
        first = journal.append(
            run_id=RUN_ID,
            event_id=EVENT_ID,
            payload=_initialized_payload(),
            recorded_at=RECORDED_AT,
        )
        second = journal.append(
            run_id=RUN_ID,
            event_id=EVENT_ID,
            payload=_initialized_payload(),
            recorded_at=RECORDED_AT.replace(second=4),
        )
        assert second.previous_sha256 == first.entry_sha256
        assert journal.head_sha256 == second.entry_sha256

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with ControllerJournal(path, REPOSITORY_ROOT) as reopened:
        assert reopened.records == [first, second]


@pytest.mark.parametrize("mode", [0o750, 0o777])
def test_journal_rejects_directory_not_exactly_0700(tmp_path: Path, mode: int) -> None:
    directory = _secure_directory(tmp_path)
    directory.chmod(mode)

    with (
        pytest.raises(
            ControllerRefusal, match="^controller_artifact_directory_invalid$"
        ),
        ControllerJournal(directory / "controller.journal", REPOSITORY_ROOT),
    ):
        pass


def test_journal_rejects_existing_file_not_0600(tmp_path: Path) -> None:
    directory = _secure_directory(tmp_path)
    path = directory / "controller.journal"
    path.touch(mode=0o600)
    path.chmod(0o640)

    with (
        pytest.raises(ControllerRefusal, match="^controller_journal_invalid$"),
        ControllerJournal(path, REPOSITORY_ROOT),
    ):
        pass


def test_journal_rejects_lexical_ancestor_symlink(tmp_path: Path) -> None:
    target = _secure_directory(tmp_path, "real-artifacts")
    linked_parent = tmp_path / "linked-artifacts"
    linked_parent.symlink_to(target, target_is_directory=True)

    with (
        pytest.raises(
            ControllerRefusal, match="^controller_artifact_directory_invalid$"
        ),
        ControllerJournal(linked_parent / "controller.journal", REPOSITORY_ROOT),
    ):
        pass


def test_journal_rejects_final_symlink(tmp_path: Path) -> None:
    directory = _secure_directory(tmp_path)
    target = tmp_path / "journal-target"
    target.touch(mode=0o600)
    target.chmod(0o600)
    (directory / "controller.journal").symlink_to(target)

    with (
        pytest.raises(ControllerRefusal, match="^controller_journal_invalid$"),
        ControllerJournal(directory / "controller.journal", REPOSITORY_ROOT),
    ):
        pass


def test_journal_rejects_hardlink(tmp_path: Path) -> None:
    directory = _secure_directory(tmp_path)
    target = tmp_path / "journal-target"
    target.touch(mode=0o600)
    target.chmod(0o600)
    os.link(target, directory / "controller.journal")

    with (
        pytest.raises(ControllerRefusal, match="^controller_journal_invalid$"),
        ControllerJournal(directory / "controller.journal", REPOSITORY_ROOT),
    ):
        pass


def test_journal_rejects_artifact_directory_inside_repository(tmp_path: Path) -> None:
    repository = _secure_directory(tmp_path, "repository")
    directory = _secure_directory(repository, "artifacts")

    with (
        pytest.raises(
            ControllerRefusal, match="^controller_artifact_directory_invalid$"
        ),
        ControllerJournal(directory / "controller.journal", repository),
    ):
        pass


def test_journal_rejects_artifact_directory_that_contains_repository(
    tmp_path: Path,
) -> None:
    directory = _secure_directory(tmp_path)
    repository = _secure_directory(directory, "repository")

    with (
        pytest.raises(
            ControllerRefusal, match="^controller_artifact_directory_invalid$"
        ),
        ControllerJournal(directory / "controller.journal", repository),
    ):
        pass


def test_journal_rejects_unexpected_directory_inventory(tmp_path: Path) -> None:
    directory = _secure_directory(tmp_path)
    unexpected = directory / "operator-notes.txt"
    unexpected.write_text("unexpected")
    unexpected.chmod(0o600)

    with (
        pytest.raises(
            ControllerRefusal,
            match="^controller_artifact_directory_inventory_invalid$",
        ),
        ControllerJournal(directory / "controller.journal", REPOSITORY_ROOT),
    ):
        pass


def test_journal_rejects_partial_final_line(tmp_path: Path) -> None:
    directory = _secure_directory(tmp_path)
    path = directory / "controller.journal"
    _append_record(path)
    path.write_bytes(path.read_bytes().removesuffix(b"\n"))

    with (
        pytest.raises(ControllerRefusal, match="^controller_journal_invalid$"),
        ControllerJournal(path, REPOSITORY_ROOT),
    ):
        pass


def test_journal_rejects_sequence_tamper_even_with_recomputed_entry_hash(
    tmp_path: Path,
) -> None:
    directory = _secure_directory(tmp_path)
    path = directory / "controller.journal"
    _append_two_records(path)
    records = _journal_json_records(path)
    records[1]["sequence"] = 3
    _rehash_record(records[1])
    _write_journal_json_records(path, records)

    with (
        pytest.raises(ControllerRefusal, match="^controller_journal_chain_invalid$"),
        ControllerJournal(path, REPOSITORY_ROOT),
    ):
        pass


def test_journal_rejects_entry_hash_tamper(tmp_path: Path) -> None:
    directory = _secure_directory(tmp_path)
    path = directory / "controller.journal"
    _append_two_records(path)
    records = _journal_json_records(path)
    records[1]["entry_sha256"] = "0" * 64
    _write_journal_json_records(path, records)

    with (
        pytest.raises(ControllerRefusal, match="^controller_journal_chain_invalid$"),
        ControllerJournal(path, REPOSITORY_ROOT),
    ):
        pass


def test_journal_rejects_previous_hash_tamper_even_with_recomputed_entry_hash(
    tmp_path: Path,
) -> None:
    directory = _secure_directory(tmp_path)
    path = directory / "controller.journal"
    _append_two_records(path)
    records = _journal_json_records(path)
    records[1]["previous_sha256"] = "0" * 64
    _rehash_record(records[1])
    _write_journal_json_records(path, records)

    with (
        pytest.raises(ControllerRefusal, match="^controller_journal_chain_invalid$"),
        ControllerJournal(path, REPOSITORY_ROOT),
    ):
        pass


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("run_id", "20000000-0000-4000-8000-000000000001"),
        ("event_id", "20000000-0000-4000-8000-000000000002"),
    ],
)
def test_journal_rejects_context_tamper_even_with_recomputed_entry_hash(
    tmp_path: Path,
    field: str,
    replacement: str,
) -> None:
    directory = _secure_directory(tmp_path)
    path = directory / "controller.journal"
    _append_two_records(path)
    records = _journal_json_records(path)
    records[1][field] = replacement
    _rehash_record(records[1])
    _write_journal_json_records(path, records)

    with (
        pytest.raises(ControllerRefusal, match="^controller_journal_chain_invalid$"),
        ControllerJournal(path, REPOSITORY_ROOT),
    ):
        pass


def test_journal_process_lifetime_lock_refuses_second_writer(tmp_path: Path) -> None:
    directory = _secure_directory(tmp_path)
    path = directory / "controller.journal"

    with (
        ControllerJournal(path, REPOSITORY_ROOT),
        pytest.raises(ControllerRefusal, match="^controller_journal_locked$"),
        ControllerJournal(path, REPOSITORY_ROOT),
    ):
        pass

    with ControllerJournal(path, REPOSITORY_ROOT):
        pass


def test_journal_append_fsyncs_descriptor_before_record_becomes_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _secure_directory(tmp_path)
    path = directory / "controller.journal"
    real_fsync = os.fsync
    synced_descriptors: list[int] = []

    def recording_fsync(descriptor: int) -> None:
        synced_descriptors.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(journal_module.os, "fsync", recording_fsync)
    with ControllerJournal(path, REPOSITORY_ROOT) as journal:
        synced_descriptors.clear()
        descriptor = journal._descriptor
        record = journal.append(
            run_id=RUN_ID,
            event_id=EVENT_ID,
            payload=_initialized_payload(),
            recorded_at=RECORDED_AT,
        )
        assert synced_descriptors == [descriptor]
        assert journal.records == [record]


def test_seal_checkpoint_uses_exclusive_nofollow_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _secure_directory(tmp_path)
    checkpoint = _checkpoint()
    real_open = os.open
    checkpoint_flags: list[int] = []

    def recording_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if str(path).startswith("controller-round-"):
            checkpoint_flags.append(flags)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(journal_module.os, "open", recording_open)
    seal_checkpoint(directory, checkpoint, REPOSITORY_ROOT)

    create_flags = checkpoint_flags[0]
    assert create_flags & os.O_CREAT
    assert create_flags & os.O_EXCL
    assert create_flags & getattr(os, "O_NOFOLLOW", 0)


def test_seal_checkpoint_existing_identical_body_is_idempotent(tmp_path: Path) -> None:
    directory = _secure_directory(tmp_path)
    checkpoint = _checkpoint()

    first = seal_checkpoint(directory, checkpoint, REPOSITORY_ROOT)
    second = seal_checkpoint(directory, checkpoint, REPOSITORY_ROOT)

    assert second == first
    assert stat.S_IMODE((directory / first.name).stat().st_mode) == 0o600


def test_seal_checkpoint_existing_different_body_refuses_collision(
    tmp_path: Path,
) -> None:
    directory = _secure_directory(tmp_path)
    checkpoint = _checkpoint()
    seal_checkpoint(directory, checkpoint, REPOSITORY_ROOT)

    changed = checkpoint.model_copy(update={"response_sha256": "0" * 64})
    with pytest.raises(ControllerRefusal, match="^controller_checkpoint_collision$"):
        seal_checkpoint(directory, changed, REPOSITORY_ROOT)


def test_checkpoint_reader_rejects_hardlink(tmp_path: Path) -> None:
    directory = _secure_directory(tmp_path)
    checkpoint = _checkpoint()
    body = canonical_json_bytes(checkpoint) + b"\n"
    source = tmp_path / "checkpoint-source.json"
    source.write_bytes(body)
    source.chmod(0o600)
    path = _checkpoint_path(directory, checkpoint)
    os.link(source, path)
    artifact = ArtifactIdentity(
        name=path.name,
        sha256=sha256(body).hexdigest(),
        size_bytes=len(body),
    )

    with pytest.raises(
        ControllerRefusal,
        match="^controller_artifact_directory_inventory_invalid$",
    ):
        load_checkpoint(directory, artifact, REPOSITORY_ROOT)


def test_load_checkpoint_missing_file_is_fail_closed(tmp_path: Path) -> None:
    directory = _secure_directory(tmp_path)
    artifact = ArtifactIdentity(
        name=f"controller-round-01-{ROUND_ID}.json",
        sha256="0" * 64,
        size_bytes=1,
    )

    with pytest.raises(ControllerRefusal, match="^controller_artifact_invalid$"):
        load_checkpoint(directory, artifact, REPOSITORY_ROOT)


def test_load_checkpoint_validates_identity_and_returns_full_model(
    tmp_path: Path,
) -> None:
    directory = _secure_directory(tmp_path)
    checkpoint = _checkpoint()
    artifact = seal_checkpoint(directory, checkpoint, REPOSITORY_ROOT)

    loaded = load_checkpoint(directory, artifact, REPOSITORY_ROOT)

    assert loaded == checkpoint
    assert loaded.generator_evidence == checkpoint.generator_evidence


@pytest.mark.parametrize(
    "identity_update",
    [
        {"sha256": "0" * 64},
        {"size_bytes": 1},
    ],
)
def test_load_checkpoint_rejects_identity_mismatch(
    tmp_path: Path,
    identity_update: dict[str, object],
) -> None:
    directory = _secure_directory(tmp_path)
    artifact = seal_checkpoint(directory, _checkpoint(), REPOSITORY_ROOT)
    mismatched = artifact.model_copy(update=identity_update)

    with pytest.raises(ControllerRefusal, match="^controller_checkpoint_invalid$"):
        load_checkpoint(directory, mismatched, REPOSITORY_ROOT)


def test_load_checkpoint_rejects_identity_name_for_missing_file(tmp_path: Path) -> None:
    directory = _secure_directory(tmp_path)
    artifact = seal_checkpoint(directory, _checkpoint(), REPOSITORY_ROOT)
    wrong_name = artifact.model_copy(
        update={"name": f"controller-round-02-{ROUND_ID}.json"}
    )

    with pytest.raises(ControllerRefusal, match="^controller_artifact_invalid$"):
        load_checkpoint(directory, wrong_name, REPOSITORY_ROOT)


def test_load_checkpoint_rejects_noncanonical_body_with_matching_digest(
    tmp_path: Path,
) -> None:
    directory = _secure_directory(tmp_path)
    checkpoint = _checkpoint()
    body = (
        json.dumps(
            checkpoint.model_dump(mode="json"),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        ).encode()
        + b"\n"
    )
    artifact = _write_checkpoint_body(directory, checkpoint, body)

    with pytest.raises(ControllerRefusal, match="^controller_checkpoint_invalid$"):
        load_checkpoint(directory, artifact, REPOSITORY_ROOT)


def test_load_checkpoint_rejects_extra_model_field_with_matching_digest(
    tmp_path: Path,
) -> None:
    directory = _secure_directory(tmp_path)
    checkpoint = _checkpoint()
    payload = checkpoint.model_dump(mode="json")
    payload["session_token"] = "must-not-be-accepted"  # noqa: S105 - 余分な秘密項目の拒否確認用
    body = canonical_json_bytes(payload) + b"\n"
    artifact = _write_checkpoint_body(directory, checkpoint, body)

    with pytest.raises(ControllerRefusal, match="^controller_checkpoint_invalid$"):
        load_checkpoint(directory, artifact, REPOSITORY_ROOT)


def test_retired_journal_format_is_rejected_even_with_valid_hash(
    tmp_path: Path,
) -> None:
    path = _secure_directory(tmp_path) / "controller.journal"
    _append_record(path)
    records = _journal_json_records(path)
    records[0]["format"] = "load-test-controller-journal-v1"
    _rehash_record(records[0])
    _write_journal_json_records(path, records)

    with pytest.raises(ControllerRefusal, match="controller_journal"):
        with ControllerJournal(path, REPOSITORY_ROOT):
            pytest.fail("旧journalを受理してはいけません")
