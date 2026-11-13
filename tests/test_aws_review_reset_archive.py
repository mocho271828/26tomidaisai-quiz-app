from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr, ValidationError
from sqlalchemy import MetaData, Table
from test_round_backup import EVENT_ID, SENSITIVE_PROMPT, SeededRound
from test_round_backup import seeded_round as seeded_round

from quiz_app.aws_demo_paths import AwsDemoDeploymentValidationError
from validation.aws_review import reset_archive
from validation.aws_review.host_contracts import HostRefusedError
from validation.aws_review.reset_archive import ResetArchive

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def archive(seeded_round: SeededRound, monkeypatch: pytest.MonkeyPatch) -> ResetArchive:
    query = Mock()
    query.mappings.return_value.one.return_value = {"last_value": 400}
    # PostgreSQL固有のsequence照会だけを置換し、application行は実SQLiteで読む。
    monkeypatch.setattr(seeded_round.session, "execute", lambda *_args, **_kwargs: query)
    return reset_archive.capture_reset_archive(
        seeded_round.session,
        reset_id=uuid4(),
        source_commit="a" * 40,
        event_id=EVENT_ID,
        event_version=1,
    )


@pytest.fixture
def archive_location() -> Iterator[tuple[Path, Path]]:
    with TemporaryDirectory(prefix=".reset-archive-test-", dir=ROOT) as temporary:
        parent = Path(temporary)
        repository = parent / "source"
        repository.mkdir()
        yield parent / "event-reset", repository


def _write(archive: ResetArchive, location: tuple[Path, Path], key: str) -> str:
    directory, repository = location
    return reset_archive.write_reset_archive(
        archive,
        directory=directory,
        repository_root=repository,
        protected_paths=(),
        encryption_key=SecretStr(key),
    )


def test_archive_captures_rows_and_sequence_without_plaintext_files(
    archive: ResetArchive, seeded_round: SeededRound, archive_location: tuple[Path, Path]
) -> None:
    digest = _write(archive, archive_location, seeded_round.encryption_key)
    directory, _ = archive_location
    path = directory / "archive.fernet"
    ciphertext = path.read_bytes()
    assert SENSITIVE_PROMPT.encode() not in ciphertext
    recovered = ResetArchive.model_validate_json(
        Fernet(seeded_round.encryption_key.encode()).decrypt(ciphertext)
    )
    assert recovered.model_dump(mode="json") == archive.model_dump(mode="json")
    assert recovered.events[0].id == EVENT_ID
    assert recovered.events[0].state is archive.events[0].state
    assert recovered.questions[0].prompt == SENSITIVE_PROMPT
    assert len(recovered.answers) == 1
    assert len(recovered.score_ledgers) == 1
    assert recovered.participant_id_sequence_last_value == 400
    assert len(digest) == 64
    assert directory.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_nlink == 1
    assert tuple(directory.iterdir()) == (path,)


def test_archive_json_rows_are_validated_instead_of_using_table_constructor(
    archive: ResetArchive,
) -> None:
    payload = archive.model_dump(mode="json")
    payload["events"][0]["id"] = "not-a-uuid"
    with pytest.raises(ValidationError):
        ResetArchive.model_validate(payload)


def test_existing_archive_directory_is_never_replaced(
    archive: ResetArchive, seeded_round: SeededRound, archive_location: tuple[Path, Path]
) -> None:
    _write(archive, archive_location, seeded_round.encryption_key)
    path = archive_location[0] / "archive.fernet"
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        _write(archive, archive_location, seeded_round.encryption_key)
    assert path.read_bytes() == original


@pytest.mark.parametrize("tamper", ["invalid", "wrong-key", "valid-but-changed"])
def test_readback_corruption_prevents_success(
    archive: ResetArchive,
    seeded_round: SeededRound,
    archive_location: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    if tamper == "invalid":
        ciphertext = b"invalid"
    else:
        cipher = Fernet(
            Fernet.generate_key() if tamper == "wrong-key" else seeded_round.encryption_key.encode()
        )
        ciphertext = cipher.encrypt(
            archive.model_copy(update={"event_version": 2}).model_dump_json().encode()
        )
    monkeypatch.setattr(
        reset_archive, "read_validated_external_file", lambda *_args, **_kwargs: ciphertext
    )
    with pytest.raises((InvalidToken, HostRefusedError)):
        _write(archive, archive_location, seeded_round.encryption_key)


def test_archive_size_limit_refuses_before_file_write(
    archive: ResetArchive,
    seeded_round: SeededRound,
    archive_location: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reset_archive, "MAX_ARTIFACT_BYTES", 1)
    with pytest.raises(HostRefusedError, match="too_large"):
        _write(archive, archive_location, seeded_round.encryption_key)
    assert not (archive_location[0] / "archive.fernet").exists()


def test_untracked_table_contract_refuses_before_database_read(
    seeded_round: SeededRound,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = MetaData()
    Table("untracked_application_table", metadata)
    monkeypatch.setattr(reset_archive.SQLModel, "metadata", metadata)
    with pytest.raises(HostRefusedError, match="table_contract_mismatch"):
        reset_archive.capture_reset_archive(
            seeded_round.session,
            reset_id=uuid4(),
            source_commit="a" * 40,
            event_id=EVENT_ID,
            event_version=1,
        )


def test_symlinked_archive_parent_refuses_before_secret_write(
    archive: ResetArchive,
    seeded_round: SeededRound,
    archive_location: tuple[Path, Path],
) -> None:
    directory, repository = archive_location
    target = directory.parent / "target"
    target.mkdir(mode=0o700)
    alias = directory.parent / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(AwsDemoDeploymentValidationError, match="symlink"):
        _write(archive, (alias / "event-reset", repository), seeded_round.encryption_key)
    assert not tuple(target.rglob("*.fernet"))
