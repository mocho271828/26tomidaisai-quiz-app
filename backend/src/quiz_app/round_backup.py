import hashlib
import hmac
import os
import re
import stat
import tempfile
from collections import Counter
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from quiz_app.models import (
    Answer,
    Event,
    Question,
    QuestionOption,
    QuizRound,
    RankSnapshot,
    RankSnapshotRow,
    ReadinessReport,
    RoundOptionStat,
    RoundPreparationTarget,
    ScoreLedger,
    ScoringPolicy,
)
from quiz_app.models.enums import (
    RoundResultPublication,
)
from quiz_app.round_backup_contracts import (
    ROUND_BACKUP_ADAPTER,
    AnyRoundBackupArtifact,
    AnyRoundBackupPayload,
    BackupOptionStatRow,
    BackupPreparationTargetRow,
    BackupQuestionOptionRow,
    BackupQuestionRow,
    BackupRankingRow,
    BackupRankSnapshotRow,
    BackupReadinessReportRow,
    BackupScoreLedgerRow,
    RoundBackupReceipt,
    UnifiedBackupAnswerRow,
    UnifiedBackupRoundRow,
    UnifiedBackupScoringPolicy,
    UnifiedRoundBackupArtifact,
    UnifiedRoundBackupPayload,
    canonical_json,
    counts_for_payload,
    plaintext_sha256,
)
from quiz_app.round_backup_contracts import (
    RoundBackupCounts as RoundBackupCounts,
)

BACKUP_FORMAT = "quiz-round-backup-v4"
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
SUFFIX_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,23}$")


class RoundBackupError(Exception):
    """Round backupの作成または検証に失敗した。"""


class RoundBackupConfigurationError(RoundBackupError):
    """暗号鍵または保存先設定が安全な条件を満たさない。"""


class RoundBackupSourceError(RoundBackupError):
    """DB上の対象Roundまたは関連行を一貫して取得できない。"""


class RoundBackupArtifactNotFoundError(RoundBackupError):
    """指定されたbackup artifactが存在しない。"""


class RoundBackupWriteError(RoundBackupError):
    """backup artifactを安全に永続化できない。"""


class RoundBackupIntegrityError(RoundBackupError):
    """artifactの認証、構造、checksumまたは件数が一致しない。"""


class RoundBackupConflictError(RoundBackupError):
    """同じartifact名が異なるRound内容に使用されている。"""


class RoundBackupService:
    """Round関連行を暗号化artifactへ固定し、直後に復号照合する。"""

    def __init__(self, directory: Path, encryption_key: SecretStr | str) -> None:
        # 共通operator dependencyの生成時にはI/Oせず、backup操作だけを保存先障害の影響範囲にする。
        self._directory = directory
        raw_key = (
            encryption_key.get_secret_value()
            if isinstance(encryption_key, SecretStr)
            else encryption_key
        )
        fernet: Fernet | None = None
        try:
            fernet = Fernet(raw_key.encode("ascii"))
        except (TypeError, ValueError, UnicodeEncodeError):
            # 鍵を含み得るcodec例外を上位の監査logへ連鎖させない。
            pass
        if fernet is None:
            raise RoundBackupConfigurationError("round_backup_encryption_key_invalid")
        self._fernet = fernet

    def create_and_verify(
        self,
        session: Session,
        round_: QuizRound,
        *,
        artifact_suffix: str,
        idempotency_key: UUID,
    ) -> RoundBackupReceipt:
        """同じartifactを上書きせず、同一内容の再試行だけをreplayとして返す。"""

        safe_suffix = self._validate_suffix(artifact_suffix)
        self._directory = self._prepare_directory(self._directory)
        artifact_name = self._artifact_name(round_, safe_suffix, idempotency_key)
        artifact_path = self._artifact_path(artifact_name)
        expected = self._build_artifact(
            session,
            round_,
            artifact_suffix=safe_suffix,
            idempotency_key=idempotency_key,
        )

        try:
            return self._verify_existing(artifact_path, expected, is_replay=True)
        except RoundBackupArtifactNotFoundError:
            pass

        encrypted = self._fernet.encrypt(canonical_json(expected))
        if len(encrypted) > MAX_ARTIFACT_BYTES:
            raise RoundBackupSourceError("round_backup_artifact_too_large")
        was_created = self._publish_without_overwrite(artifact_path, encrypted)
        if not was_created:
            return self._verify_existing(artifact_path, expected, is_replay=True)

        try:
            # 複数query間のsource変化も成功扱いにしない。
            current = self._build_artifact(
                session,
                round_,
                artifact_suffix=safe_suffix,
                idempotency_key=idempotency_key,
            )
            if current != expected:
                raise RoundBackupSourceError("round_backup_source_changed_during_write")
            return self._verify_existing(artifact_path, current, is_replay=False)
        except RoundBackupError:
            self._remove_created_artifact(artifact_path)
            raise

    @staticmethod
    def _prepare_directory(directory: Path) -> Path:
        if directory.exists() and directory.is_symlink():
            raise RoundBackupConfigurationError("round_backup_directory_must_not_be_symlink")
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            resolved = directory.resolve(strict=True)
            directory_stat = resolved.stat()
        except OSError as error:
            raise RoundBackupConfigurationError("round_backup_directory_unavailable") from error
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise RoundBackupConfigurationError("round_backup_directory_not_directory")
        return resolved

    @staticmethod
    def _validate_suffix(artifact_suffix: str) -> str:
        if SUFFIX_PATTERN.fullmatch(artifact_suffix) is None:
            raise RoundBackupConfigurationError("round_backup_artifact_suffix_invalid")
        return artifact_suffix

    @staticmethod
    def _artifact_name(
        round_: QuizRound,
        artifact_suffix: str,
        idempotency_key: UUID,
    ) -> str:
        identity = f"{round_.id}:{round_.version}:{idempotency_key}"
        identity_digest = hashlib.sha256(identity.encode("ascii")).hexdigest()[:32]
        return f"rb-{identity_digest}.{artifact_suffix}.fernet"

    def _artifact_path(self, artifact_name: str) -> Path:
        artifact_path = self._directory / artifact_name
        if artifact_path.parent != self._directory:
            raise RoundBackupConfigurationError("round_backup_artifact_path_invalid")
        return artifact_path

    def _build_artifact(
        self,
        session: Session,
        round_: QuizRound,
        *,
        artifact_suffix: str,
        idempotency_key: UUID,
    ) -> AnyRoundBackupArtifact:
        payload: UnifiedRoundBackupPayload | None = None
        try:
            persisted_round = session.get(QuizRound, round_.id)
            if persisted_round is None:
                raise RoundBackupSourceError("round_backup_round_not_found")
            if (
                persisted_round.version != round_.version
                or persisted_round.event_id != round_.event_id
                or persisted_round.question_id != round_.question_id
            ):
                raise RoundBackupSourceError("round_backup_round_identity_changed")
            question = session.get(Question, persisted_round.question_id)
            if question is None or question.event_id != persisted_round.event_id:
                raise RoundBackupSourceError("round_backup_question_not_found_for_event")
            event = session.get(Event, persisted_round.event_id)
            policy = session.get(ScoringPolicy, event.scoring_policy_version) if event else None
            if policy is None:
                raise RoundBackupSourceError("round_backup_scoring_policy_missing")
            payload = self._load_payload(session, persisted_round, question, policy)
        except SQLAlchemyError as error:
            raise RoundBackupSourceError("round_backup_database_read_failed") from error
        except ValidationError:
            # ValidationErrorのinput_valueへ問題文等が含まれるため例外連鎖を切る。
            pass
        if payload is None:
            raise RoundBackupSourceError("round_backup_source_contract_invalid")
        self._validate_source_invariants(payload)

        artifact: AnyRoundBackupArtifact | None = None
        try:
            artifact = UnifiedRoundBackupArtifact(
                format=BACKUP_FORMAT,
                idempotency_key=idempotency_key,
                artifact_suffix=artifact_suffix,
                plaintext_sha256=plaintext_sha256(payload),
                counts=counts_for_payload(payload),
                payload=payload,
            )
        except ValidationError:
            pass
        if artifact is None:
            raise RoundBackupSourceError("round_backup_source_contract_invalid")
        return artifact

    @staticmethod
    def _validate_source_invariants(payload: AnyRoundBackupPayload) -> None:
        """公開結果を復元できない欠落・不一致があればartifact作成前に停止する。"""

        if payload.round.result_publication is not RoundResultPublication.PUBLISHED:
            return

        if sum(option.is_correct for option in payload.question_options) != 1:
            raise RoundBackupSourceError("round_backup_correct_option_count_invalid")

        option_ids = {option.id for option in payload.question_options}
        stat_option_ids = {stat.option_id for stat in payload.option_stats}
        if len(stat_option_ids) != len(payload.option_stats) or stat_option_ids != option_ids:
            raise RoundBackupSourceError("round_backup_option_stats_incomplete")

        answer_counts = Counter(answer.option_id for answer in payload.answers)
        if not answer_counts.keys() <= option_ids:
            raise RoundBackupSourceError("round_backup_answer_option_invalid")

        answer_total = len(payload.answers)
        for option_stat in payload.option_stats:
            expected_count = answer_counts[option_stat.option_id]
            if option_stat.valid_response_count != expected_count:
                raise RoundBackupSourceError("round_backup_option_answer_distribution_mismatch")
            expected_percentage = Decimal("0")
            if answer_total > 0:
                expected_percentage = (
                    Decimal(expected_count) * Decimal(100) / Decimal(answer_total)
                ).quantize(Decimal("0.01"))
            if option_stat.response_percentage != expected_percentage:
                raise RoundBackupSourceError("round_backup_option_percentage_mismatch")

        if len(payload.score_ledgers) != len(payload.answers):
            raise RoundBackupSourceError("round_backup_score_ledger_count_mismatch")
        answer_participants = Counter(answer.participant_id for answer in payload.answers)
        ledger_participants = Counter(
            score_ledger.participant_id for score_ledger in payload.score_ledgers
        )
        if ledger_participants != answer_participants:
            raise RoundBackupSourceError("round_backup_score_ledger_participants_mismatch")
        if not payload.rank_snapshots:
            raise RoundBackupSourceError("round_backup_rank_snapshot_missing")

    @staticmethod
    def _load_payload(
        session: Session,
        round_: QuizRound,
        question: Question,
        policy: ScoringPolicy,
    ) -> UnifiedRoundBackupPayload:
        options = session.scalars(
            select(QuestionOption)
            .where(QuestionOption.question_id == question.id)
            .order_by(QuestionOption.display_order, QuestionOption.id)
        ).all()
        answers = session.scalars(
            select(Answer).where(Answer.round_id == round_.id).order_by(Answer.id)
        ).all()
        score_ledgers = session.scalars(
            select(ScoreLedger).where(ScoreLedger.round_id == round_.id).order_by(ScoreLedger.id)
        ).all()
        readiness_reports = session.scalars(
            select(ReadinessReport)
            .where(ReadinessReport.round_id == round_.id)
            .order_by(ReadinessReport.id)
        ).all()
        preparation_targets = session.scalars(
            select(RoundPreparationTarget)
            .where(RoundPreparationTarget.round_id == round_.id)
            .order_by(RoundPreparationTarget.id)
        ).all()
        option_stats = session.scalars(
            select(RoundOptionStat)
            .where(RoundOptionStat.round_id == round_.id)
            .order_by(RoundOptionStat.option_id, RoundOptionStat.id)
        ).all()
        rank_snapshots = session.scalars(
            select(RankSnapshot)
            .where(RankSnapshot.round_id == round_.id)
            .order_by(RankSnapshot.version, RankSnapshot.id)
        ).all()
        rank_snapshot_rows = session.scalars(
            select(RankSnapshotRow)
            .join(RankSnapshot, RankSnapshot.id == RankSnapshotRow.snapshot_id)
            .where(RankSnapshot.round_id == round_.id)
            .order_by(
                RankSnapshotRow.snapshot_id,
                RankSnapshotRow.overall_rank,
                RankSnapshotRow.public_label,
                RankSnapshotRow.id,
            )
        ).all()

        return UnifiedRoundBackupPayload(
            round=UnifiedBackupRoundRow.model_validate(round_, from_attributes=True),
            question=BackupQuestionRow.model_validate(question, from_attributes=True),
            scoring_policy=UnifiedBackupScoringPolicy.model_validate(policy, from_attributes=True),
            question_options=[
                BackupQuestionOptionRow.model_validate(row, from_attributes=True) for row in options
            ],
            answers=[
                UnifiedBackupAnswerRow.model_validate(row, from_attributes=True) for row in answers
            ],
            score_ledgers=[
                BackupScoreLedgerRow.model_validate(row, from_attributes=True)
                for row in score_ledgers
            ],
            readiness_reports=[
                BackupReadinessReportRow.model_validate(row, from_attributes=True)
                for row in readiness_reports
            ],
            preparation_targets=[
                BackupPreparationTargetRow.model_validate(row, from_attributes=True)
                for row in preparation_targets
            ],
            option_stats=[
                BackupOptionStatRow.model_validate(row, from_attributes=True)
                for row in option_stats
            ],
            rank_snapshots=[
                BackupRankSnapshotRow.model_validate(row, from_attributes=True)
                for row in rank_snapshots
            ],
            rank_snapshot_rows=[
                BackupRankingRow.model_validate(row, from_attributes=True)
                for row in rank_snapshot_rows
            ],
        )

    def _verify_existing(
        self,
        artifact_path: Path,
        expected: AnyRoundBackupArtifact,
        *,
        is_replay: bool,
    ) -> RoundBackupReceipt:
        encrypted = self._read_artifact(artifact_path)
        try:
            plaintext = self._fernet.decrypt(encrypted)
        except InvalidToken as error:
            raise RoundBackupIntegrityError(
                "round_backup_artifact_authentication_failed"
            ) from error
        artifact: AnyRoundBackupArtifact | None = None
        try:
            artifact = ROUND_BACKUP_ADAPTER.validate_json(plaintext)
        except ValidationError:
            # 復号済みpayloadを監査logのexception chainへ出さない。
            pass
        if artifact is None:
            raise RoundBackupIntegrityError("round_backup_artifact_contract_invalid")

        if artifact.format != expected.format:
            raise RoundBackupConflictError("round_backup_format_mismatch")
        if artifact.idempotency_key != expected.idempotency_key:
            raise RoundBackupConflictError("round_backup_idempotency_key_mismatch")
        if artifact.artifact_suffix != expected.artifact_suffix:
            raise RoundBackupConflictError("round_backup_artifact_suffix_mismatch")
        if (
            artifact.payload.round.id != expected.payload.round.id
            or artifact.payload.round.version != expected.payload.round.version
        ):
            raise RoundBackupConflictError("round_backup_round_identity_mismatch")
        if artifact.counts != expected.counts:
            raise RoundBackupConflictError("round_backup_current_row_counts_mismatch")
        if not hmac.compare_digest(
            artifact.plaintext_sha256,
            expected.plaintext_sha256,
        ):
            raise RoundBackupConflictError("round_backup_current_plaintext_checksum_mismatch")
        if artifact.payload != expected.payload:
            raise RoundBackupConflictError("round_backup_current_payload_mismatch")

        return RoundBackupReceipt(
            artifact_name=artifact_path.name,
            plaintext_sha256=artifact.plaintext_sha256,
            counts=artifact.counts,
            round_id=artifact.payload.round.id,
            round_version=artifact.payload.round.version,
            is_replay=is_replay,
        )

    @staticmethod
    def _read_artifact(artifact_path: Path) -> bytes:
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(artifact_path, flags)
        except FileNotFoundError as error:
            raise RoundBackupArtifactNotFoundError("round_backup_artifact_not_found") from error
        except OSError as error:
            raise RoundBackupIntegrityError("round_backup_artifact_open_failed") from error

        try:
            artifact_stat = os.fstat(descriptor)
            if not stat.S_ISREG(artifact_stat.st_mode):
                raise RoundBackupIntegrityError("round_backup_artifact_not_regular_file")
            if stat.S_IMODE(artifact_stat.st_mode) != 0o600:
                raise RoundBackupIntegrityError("round_backup_artifact_mode_invalid")
            if artifact_stat.st_size > MAX_ARTIFACT_BYTES:
                raise RoundBackupIntegrityError("round_backup_artifact_too_large")
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                descriptor = -1
                return stream.read(MAX_ARTIFACT_BYTES + 1)
        except OSError as error:
            raise RoundBackupIntegrityError("round_backup_artifact_read_failed") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _publish_without_overwrite(self, artifact_path: Path, encrypted: bytes) -> bool:
        temporary_path: Path | None = None
        descriptor = -1
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self._directory,
                prefix=f".{artifact_path.name}.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = -1
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary_path, artifact_path, follow_symlinks=False)
            except FileExistsError:
                return False
            self._fsync_directory()
            return True
        except OSError as error:
            raise RoundBackupWriteError("round_backup_artifact_write_failed") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError as error:
                    raise RoundBackupWriteError("round_backup_temporary_cleanup_failed") from error

    def _remove_created_artifact(self, artifact_path: Path) -> None:
        try:
            artifact_path.unlink(missing_ok=True)
            self._fsync_directory()
        except OSError as error:
            raise RoundBackupWriteError("round_backup_failed_artifact_cleanup_failed") from error

    def _fsync_directory(self) -> None:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        try:
            descriptor = os.open(self._directory, flags)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise RoundBackupWriteError("round_backup_directory_fsync_failed") from error
