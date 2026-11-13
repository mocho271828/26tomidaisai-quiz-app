"""Event初期化直前の全application行を、Round backupとは別に暗号化退避する。"""

import hashlib
import hmac
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypeVar, get_args
from uuid import UUID

from cryptography.fernet import Fernet
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationInfo, field_validator
from sqlalchemy import select, text
from sqlmodel import Session, SQLModel

from quiz_app import models
from quiz_app.aws_demo_paths import (
    RUNTIME_ENVIRONMENT_FILE_POLICY,
    read_validated_external_file,
    validate_external_artifact_directory,
    validate_external_regular_file,
)
from quiz_app.round_backup import MAX_ARTIFACT_BYTES
from quiz_app.schema_revision import SCHEMA_REVISION
from validation.aws_review.host_contracts import HostRefusedError

Row = TypeVar("Row", bound=SQLModel)


class ResetArchive(BaseModel):
    """復元用CLIを持たない保全記録。認証hashを含むため平文では永続化しない。"""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    format: Literal["quiz-event-reset-archive-v1"] = "quiz-event-reset-archive-v1"
    schema_revision: str = SCHEMA_REVISION
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    reset_id: UUID
    event_id: UUID
    event_version: int = Field(ge=1)
    archived_at: datetime
    events: tuple[models.Event, ...]
    event_quiz_configurations: tuple[models.EventQuizConfiguration, ...]
    scoring_policies: tuple[models.ScoringPolicy, ...]
    display_name_policies: tuple[models.DisplayNamePolicy, ...]
    admin_accounts: tuple[models.AdminAccount, ...]
    admin_sessions: tuple[models.AdminSession, ...]
    participants: tuple[models.Participant, ...]
    participant_sessions: tuple[models.ParticipantSession, ...]
    display_name_consents: tuple[models.DisplayNameConsent, ...]
    participant_id_pools: tuple[models.ParticipantIdPool, ...]
    participant_id_slots: tuple[models.ParticipantIdSlot, ...]
    questions: tuple[models.Question, ...]
    question_options: tuple[models.QuestionOption, ...]
    quiz_rounds: tuple[models.QuizRound, ...]
    readiness_reports: tuple[models.ReadinessReport, ...]
    round_preparation_targets: tuple[models.RoundPreparationTarget, ...]
    answers: tuple[models.Answer, ...]
    score_ledgers: tuple[models.ScoreLedger, ...]
    round_option_stats: tuple[models.RoundOptionStat, ...]
    rank_snapshots: tuple[models.RankSnapshot, ...]
    rank_snapshot_rows: tuple[models.RankSnapshotRow, ...]
    operation_audits: tuple[models.OperationAudit, ...]
    idempotency_records: tuple[models.IdempotencyRecord, ...]
    participant_id_sequence_last_value: int | None = Field(ge=1)

    @field_validator("*", mode="before")
    @classmethod
    def validate_table_rows(cls, value: object, info: ValidationInfo) -> object:
        # SQLModelのtable型は通常のJSON内包時に検証を省略する。明示検証でUUID/日時等を
        # 復元し、保全の対象列をDB modelと一致させたまま境界の型検証を保証する。
        if info.field_name is None:
            return value
        arguments = get_args(cls.model_fields[info.field_name].annotation)
        if (
            not arguments
            or not isinstance(arguments[0], type)
            or not issubclass(arguments[0], SQLModel)
        ):
            return value
        if not isinstance(value, (list, tuple)):
            raise ValueError("review_reset_archive_rows_invalid")
        return tuple(arguments[0].model_validate(row) for row in value)


class SequenceState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    last_value: int | None = Field(ge=1)


def _rows(session: Session, model: type[Row]) -> tuple[Row, ...]:
    return tuple(model.model_validate(row) for row in session.scalars(select(model)))


def capture_reset_archive(
    session: Session, *, reset_id: UUID, source_commit: str, event_id: UUID, event_version: int
) -> ResetArchive:
    """削除transaction内で呼び、既知の全tableと採番位置を読み取る。"""
    # 新しいtable追加時に、保全対象を更新せず削除だけ進めることを防ぐ。
    archived_tables = set(ResetArchive.model_fields) & set(SQLModel.metadata.tables)
    if archived_tables != set(SQLModel.metadata.tables):
        raise HostRefusedError("review_reset_archive_table_contract_mismatch")
    sequence = SequenceState.model_validate(
        dict(
            session.execute(
                text(
                    "SELECT last_value FROM pg_catalog.pg_sequences "
                    "WHERE schemaname = 'public' AND sequencename = :name"
                ),
                {"name": f"participant_id_{event_id.hex}"},
            )
            .mappings()
            .one()
        )
    )
    return ResetArchive(
        source_commit=source_commit,
        reset_id=reset_id,
        event_id=event_id,
        event_version=event_version,
        archived_at=datetime.now(UTC),
        events=_rows(session, models.Event),
        event_quiz_configurations=_rows(session, models.EventQuizConfiguration),
        scoring_policies=_rows(session, models.ScoringPolicy),
        display_name_policies=_rows(session, models.DisplayNamePolicy),
        admin_accounts=_rows(session, models.AdminAccount),
        admin_sessions=_rows(session, models.AdminSession),
        participants=_rows(session, models.Participant),
        participant_sessions=_rows(session, models.ParticipantSession),
        display_name_consents=_rows(session, models.DisplayNameConsent),
        participant_id_pools=_rows(session, models.ParticipantIdPool),
        participant_id_slots=_rows(session, models.ParticipantIdSlot),
        questions=_rows(session, models.Question),
        question_options=_rows(session, models.QuestionOption),
        quiz_rounds=_rows(session, models.QuizRound),
        readiness_reports=_rows(session, models.ReadinessReport),
        round_preparation_targets=_rows(session, models.RoundPreparationTarget),
        answers=_rows(session, models.Answer),
        score_ledgers=_rows(session, models.ScoreLedger),
        round_option_stats=_rows(session, models.RoundOptionStat),
        rank_snapshots=_rows(session, models.RankSnapshot),
        rank_snapshot_rows=_rows(session, models.RankSnapshotRow),
        operation_audits=_rows(session, models.OperationAudit),
        idempotency_records=_rows(session, models.IdempotencyRecord),
        participant_id_sequence_last_value=sequence.last_value,
    )


def write_reset_archive(
    archive: ResetArchive,
    *,
    directory: Path,
    repository_root: Path,
    protected_paths: tuple[Path, ...],
    encryption_key: SecretStr,
) -> str:
    """新規0700 directory内へ0600で保存し、再読込・復号・全内容照合後だけ返す。"""
    # Round backupのbind対象外へ置き、過去の保全記録を再実行で上書きしない。
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    validate_external_artifact_directory(
        directory,
        repository_root,
        runtime_uid=os.geteuid(),
        runtime_gid=os.getegid(),
        protected_paths=protected_paths,
    )
    plaintext = archive.model_dump_json().encode("utf-8")
    if len(plaintext) > MAX_ARTIFACT_BYTES // 2:
        raise HostRefusedError("review_reset_archive_too_large")
    cipher = Fernet(encryption_key.get_secret_value().encode("ascii"))
    encrypted = cipher.encrypt(plaintext)
    path = directory / "archive.fernet"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encrypted)
        stream.flush()
        os.fsync(stream.fileno())
    for durable_directory in (directory, directory.parent):
        descriptor = os.open(durable_directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    validated = validate_external_regular_file(
        path, repository_root, RUNTIME_ENVIRONMENT_FILE_POLICY
    )
    saved = read_validated_external_file(
        validated,
        maximum_bytes=MAX_ARTIFACT_BYTES,
        unavailable_code="review_reset_archive_unavailable",
    )
    restored = cipher.decrypt(saved)
    if not hmac.compare_digest(
        hashlib.sha256(restored).digest(), hashlib.sha256(plaintext).digest()
    ):
        raise HostRefusedError("review_reset_archive_checksum_mismatch")
    if ResetArchive.model_validate_json(restored).model_dump(mode="json") != archive.model_dump(
        mode="json"
    ):
        raise HostRefusedError("review_reset_archive_content_mismatch")
    return hashlib.sha256(plaintext).hexdigest()
