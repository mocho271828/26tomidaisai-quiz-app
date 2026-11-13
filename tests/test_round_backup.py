import stat
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel

from quiz_app.models import (
    Answer,
    Event,
    Participant,
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
    RoundState,
    ScoreExclusionReason,
    SpeedCohort,
)
from quiz_app.round_backup import (
    RoundBackupConfigurationError,
    RoundBackupConflictError,
    RoundBackupIntegrityError,
    RoundBackupReceipt,
    RoundBackupService,
    RoundBackupSourceError,
)
from quiz_app.round_backup_contracts import (
    ROUND_BACKUP_ADAPTER,
    UnifiedRoundBackupArtifact,
    canonical_json,
    plaintext_sha256,
)

NOW = datetime(2026, 8, 21, 10, 30, tzinfo=UTC)
EVENT_ID = UUID(int=1)
QUESTION_ID = UUID(int=2)
ROUND_ID = UUID(int=3)
PARTICIPANT_ID = UUID(int=4)
OPTION_A_ID = UUID(int=5)
OPTION_B_ID = UUID(int=6)
IDEMPOTENCY_KEY = UUID(int=7)
BACKUP_SUFFIX = "advance_round"
SENSITIVE_PROMPT = "ROUND-BACKUP-ONLY-PROMPT-7de22f4a"
SENSITIVE_OPTION = "ROUND-BACKUP-ONLY-OPTION-a104fc8b"


@dataclass(frozen=True)
class SeededRound:
    session: Session
    round: QuizRound
    options: tuple[QuestionOption, QuestionOption]
    stats: tuple[RoundOptionStat, RoundOptionStat]
    score_ledger: ScoreLedger
    rank_snapshot: RankSnapshot
    rank_row: RankSnapshotRow
    encryption_key: str


@pytest.fixture
def seeded_round() -> Iterator[SeededRound]:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    # PostgreSQL専用の部分indexはSQLiteでは無条件uniqueになるためunit testでは除く。
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX uq_question_options_one_correct")

    encryption_key = Fernet.generate_key().decode("ascii")
    policy = ScoringPolicy(
        version="score-v1",
        base_points=100,
        initial_speed_bonus=20,
        speed_bucket_ms=1000,
        speed_decrement_per_bucket=1,
        standard_duration_ms=10000,
        is_approved=True,
        created_at=NOW,
    )
    event = Event(
        id=EVENT_ID,
        slug="backup-test",
        title="backup test",
        scoring_policy_version=policy.version,
        display_name_policy_version="test",
        created_at=NOW,
        updated_at=NOW,
    )
    question = Question(
        id=QUESTION_ID,
        event_id=EVENT_ID,
        version=1,
        prompt=SENSITIVE_PROMPT,
        explanation="復元対象の解説",
        display_order=1,
        created_at=NOW,
    )
    option_a = QuestionOption(
        id=OPTION_A_ID,
        question_id=QUESTION_ID,
        label="A",
        content="誤答の選択肢",
        display_order=1,
        is_correct=False,
    )
    option_b = QuestionOption(
        id=OPTION_B_ID,
        question_id=QUESTION_ID,
        label="B",
        content=SENSITIVE_OPTION,
        display_order=2,
        is_correct=True,
    )
    round_ = QuizRound(
        id=ROUND_ID,
        event_id=EVENT_ID,
        question_id=QUESTION_ID,
        version=7,
        state=RoundState.ROUND_BACKUP_PENDING,
        result_publication=RoundResultPublication.PUBLISHED,
        open_at=NOW - timedelta(minutes=3),
        standard_close_at=NOW - timedelta(minutes=3) + timedelta(seconds=10),
        revealed_at=NOW,
        created_at=NOW,
    )
    answer = Answer(
        id=UUID(int=8),
        participant_id=PARTICIPANT_ID,
        round_id=ROUND_ID,
        option_id=OPTION_B_ID,
        client_answer_id=UUID(int=9),
        received_at=NOW - timedelta(minutes=3) + timedelta(milliseconds=1_250),
    )
    score_ledger = ScoreLedger(
        id=UUID(int=10),
        round_id=ROUND_ID,
        participant_id=PARTICIPANT_ID,
        scoring_policy_version="score-v1",
        is_correct=True,
        base_points=100,
        speed_points=20,
        total_points=120,
        elapsed_ms=1_250,
        speed_bucket=1,
        speed_rank=1,
        speed_cohort_size=1,
        speed_cohort=SpeedCohort.CORRECT,
        exclusion_reason=ScoreExclusionReason.NONE,
    )
    readiness = ReadinessReport(
        id=UUID(int=11),
        participant_id=PARTICIPANT_ID,
        round_id=ROUND_ID,
        round_version=7,
        is_ready=True,
        reported_at=NOW,
        last_seen_at=NOW,
    )
    preparation_target = RoundPreparationTarget(
        id=UUID(int=12),
        round_id=ROUND_ID,
        participant_id=PARTICIPANT_ID,
        captured_at=NOW,
    )
    stat_a = RoundOptionStat(
        id=UUID(int=13),
        round_id=ROUND_ID,
        option_id=OPTION_A_ID,
        valid_response_count=0,
        response_percentage=0,
    )
    stat_b = RoundOptionStat(
        id=UUID(int=14),
        round_id=ROUND_ID,
        option_id=OPTION_B_ID,
        valid_response_count=1,
        response_percentage=100,
    )
    rank_snapshot = RankSnapshot(
        id=UUID(int=15),
        event_id=EVENT_ID,
        round_id=ROUND_ID,
        version=1,
        generated_at=NOW,
    )
    rank_row = RankSnapshotRow(
        id=UUID(int=16),
        snapshot_id=rank_snapshot.id,
        public_label="AAA",
        overall_rank=1,
        total_points=120,
        correct_count=1,
    )

    with Session(engine, expire_on_commit=False) as session:
        session.add_all(
            [
                policy,
                event,
                question,
                option_a,
                option_b,
                round_,
                answer,
                score_ledger,
                readiness,
                preparation_target,
                stat_a,
                stat_b,
                rank_snapshot,
                rank_row,
            ]
        )
        session.commit()
        yield SeededRound(
            session=session,
            round=round_,
            options=(option_a, option_b),
            stats=(stat_a, stat_b),
            score_ledger=score_ledger,
            rank_snapshot=rank_snapshot,
            rank_row=rank_row,
            encryption_key=encryption_key,
        )
    engine.dispose()


def _service(directory: Path, encryption_key: str) -> RoundBackupService:
    return RoundBackupService(directory, SecretStr(encryption_key))


def _create_backup(
    service: RoundBackupService,
    seeded: SeededRound,
) -> RoundBackupReceipt:
    return service.create_and_verify(
        seeded.session,
        seeded.round,
        artifact_suffix=BACKUP_SUFFIX,
        idempotency_key=IDEMPOTENCY_KEY,
    )


def test_backup_encrypts_all_round_rows_and_verifies_plaintext(
    tmp_path: Path,
    seeded_round: SeededRound,
) -> None:
    service = _service(tmp_path, seeded_round.encryption_key)

    receipt = _create_backup(service, seeded_round)
    artifact_path = tmp_path / receipt.artifact_name
    encrypted = artifact_path.read_bytes()
    plaintext = Fernet(seeded_round.encryption_key).decrypt(encrypted)
    artifact = UnifiedRoundBackupArtifact.model_validate_json(plaintext)

    assert receipt.is_replay is False
    assert receipt.round_id == ROUND_ID
    assert receipt.round_version == 7
    assert receipt.plaintext_sha256 == artifact.plaintext_sha256
    assert artifact.format == "quiz-round-backup-v4"
    assert receipt.counts.model_dump() == {
        "rounds": 1,
        "questions": 1,
        "question_options": 2,
        "answers": 1,
        "score_ledgers": 1,
        "readiness_reports": 1,
        "preparation_targets": 1,
        "option_stats": 2,
        "rank_snapshots": 1,
        "rank_snapshot_rows": 1,
    }
    assert artifact.payload.round.id == ROUND_ID
    assert artifact.payload.question.prompt == SENSITIVE_PROMPT
    assert [option.content for option in artifact.payload.question_options] == [
        "誤答の選択肢",
        SENSITIVE_OPTION,
    ]
    assert artifact.payload.question_options[1].is_correct is True
    assert artifact.payload.rank_snapshot_rows[0].public_label == "AAA"
    assert SENSITIVE_PROMPT.encode() not in encrypted
    assert SENSITIVE_OPTION.encode() not in encrypted
    assert b"PUBLIC-001" not in encrypted
    assert stat.S_IMODE(artifact_path.stat().st_mode) == 0o600
    assert "artifact_path" not in receipt.model_dump()


def test_same_artifact_retry_does_not_overwrite_file(
    tmp_path: Path,
    seeded_round: SeededRound,
) -> None:
    service = _service(tmp_path, seeded_round.encryption_key)

    first = _create_backup(service, seeded_round)
    artifact_path = tmp_path / first.artifact_name
    original_encrypted = artifact_path.read_bytes()
    second = _create_backup(service, seeded_round)

    assert second.is_replay is True
    assert second.artifact_name == first.artifact_name
    assert second.plaintext_sha256 == first.plaintext_sha256
    assert artifact_path.read_bytes() == original_encrypted
    assert list(tmp_path.iterdir()) == [artifact_path]


def test_tampered_ciphertext_is_rejected_without_overwrite(
    tmp_path: Path,
    seeded_round: SeededRound,
) -> None:
    service = _service(tmp_path, seeded_round.encryption_key)
    receipt = _create_backup(service, seeded_round)
    artifact_path = tmp_path / receipt.artifact_name
    encrypted = artifact_path.read_bytes()
    tampered = bytes([encrypted[0] ^ 1]) + encrypted[1:]
    artifact_path.write_bytes(tampered)

    with pytest.raises(
        RoundBackupIntegrityError,
        match="round_backup_artifact_authentication_failed",
    ):
        _create_backup(service, seeded_round)

    assert artifact_path.read_bytes() == tampered


def test_authenticated_artifact_with_wrong_counts_is_rejected(
    tmp_path: Path,
    seeded_round: SeededRound,
) -> None:
    service = _service(tmp_path, seeded_round.encryption_key)
    receipt = _create_backup(service, seeded_round)
    artifact_path = tmp_path / receipt.artifact_name
    fernet = Fernet(seeded_round.encryption_key)
    artifact = UnifiedRoundBackupArtifact.model_validate_json(
        fernet.decrypt(artifact_path.read_bytes())
    )
    wrong_counts = artifact.counts.model_copy(update={"answers": 2})
    malformed_artifact = artifact.model_copy(update={"counts": wrong_counts})
    artifact_path.write_bytes(fernet.encrypt(malformed_artifact.model_dump_json().encode("utf-8")))

    with pytest.raises(
        RoundBackupIntegrityError,
        match="round_backup_artifact_contract_invalid",
    ) as captured:
        _create_backup(service, seeded_round)

    # 上位の監査logへ復号済みpayloadを含むValidationErrorを連鎖させない。
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert SENSITIVE_PROMPT not in str(captured.value)
    assert SENSITIVE_OPTION not in str(captured.value)


def test_artifact_for_another_round_is_rejected(
    tmp_path: Path,
    seeded_round: SeededRound,
) -> None:
    service = _service(tmp_path, seeded_round.encryption_key)
    first = _create_backup(service, seeded_round)
    other_event_id = UUID(int=17)
    other_question = Question(
        id=UUID(int=18),
        event_id=other_event_id,
        version=1,
        prompt="別Roundの問題",
        display_order=1,
        created_at=NOW,
    )
    other_option = QuestionOption(
        id=UUID(int=19),
        question_id=other_question.id,
        label="A",
        content="別Roundの選択肢",
        display_order=1,
        is_correct=True,
    )
    other_round = QuizRound(
        id=UUID(int=20),
        event_id=other_event_id,
        question_id=other_question.id,
        version=1,
        state=RoundState.PREPARING_ASSETS,
        result_publication=RoundResultPublication.WITHHELD,
        created_at=NOW,
    )
    other_event = Event(
        id=other_event_id,
        slug="other-backup-test",
        title="other backup test",
        scoring_policy_version="score-v1",
        display_name_policy_version="test",
        created_at=NOW,
        updated_at=NOW,
    )
    seeded_round.session.add_all([other_event, other_question, other_option, other_round])
    seeded_round.session.commit()
    second = service.create_and_verify(
        seeded_round.session,
        other_round,
        artifact_suffix=BACKUP_SUFFIX,
        idempotency_key=IDEMPOTENCY_KEY,
    )
    first_path = tmp_path / first.artifact_name
    second_path = tmp_path / second.artifact_name
    second_path.write_bytes(first_path.read_bytes())

    with pytest.raises(
        RoundBackupConflictError,
        match="round_backup_round_identity_mismatch",
    ):
        service.create_and_verify(
            seeded_round.session,
            other_round,
            artifact_suffix=BACKUP_SUFFIX,
            idempotency_key=IDEMPOTENCY_KEY,
        )


def test_wrong_encryption_key_cannot_verify_existing_artifact(
    tmp_path: Path,
    seeded_round: SeededRound,
) -> None:
    _create_backup(_service(tmp_path, seeded_round.encryption_key), seeded_round)
    wrong_key_service = _service(tmp_path, Fernet.generate_key().decode("ascii"))

    with pytest.raises(
        RoundBackupIntegrityError,
        match="round_backup_artifact_authentication_failed",
    ):
        _create_backup(wrong_key_service, seeded_round)


@pytest.mark.parametrize(
    ("invalid_source", "expected_error"),
    [
        ("correct_option", "round_backup_correct_option_count_invalid"),
        ("option_stats", "round_backup_option_stats_incomplete"),
        ("option_distribution", "round_backup_option_answer_distribution_mismatch"),
        ("option_percentage", "round_backup_option_percentage_mismatch"),
        ("score_count", "round_backup_score_ledger_count_mismatch"),
        ("score_participant", "round_backup_source_contract_invalid"),
        ("rank_snapshot", "round_backup_rank_snapshot_missing"),
    ],
)
def test_published_round_source_invariants_are_required_before_write(
    tmp_path: Path,
    seeded_round: SeededRound,
    invalid_source: str,
    expected_error: str,
) -> None:
    if invalid_source == "correct_option":
        seeded_round.options[1].is_correct = False
    elif invalid_source == "option_stats":
        seeded_round.session.delete(seeded_round.stats[0])
    elif invalid_source == "option_distribution":
        seeded_round.stats[0].valid_response_count = 1
        seeded_round.stats[1].valid_response_count = 0
    elif invalid_source == "option_percentage":
        seeded_round.stats[1].response_percentage = 50
    elif invalid_source == "score_count":
        seeded_round.session.delete(seeded_round.score_ledger)
    elif invalid_source == "score_participant":
        seeded_round.score_ledger.participant_id = UUID(int=99)
    elif invalid_source == "rank_snapshot":
        seeded_round.session.delete(seeded_round.rank_row)
        seeded_round.session.delete(seeded_round.rank_snapshot)
    seeded_round.session.commit()
    service = _service(tmp_path, seeded_round.encryption_key)

    with pytest.raises(RoundBackupSourceError, match=expected_error):
        _create_backup(service, seeded_round)

    assert list(tmp_path.iterdir()) == []


def test_non_published_round_does_not_require_published_result_rows(
    tmp_path: Path,
    seeded_round: SeededRound,
) -> None:
    seeded_round.round.result_publication = RoundResultPublication.WITHHELD
    seeded_round.session.delete(seeded_round.stats[0])
    seeded_round.session.delete(seeded_round.stats[1])
    seeded_round.session.delete(seeded_round.score_ledger)
    seeded_round.session.delete(seeded_round.rank_row)
    seeded_round.session.delete(seeded_round.rank_snapshot)
    seeded_round.session.commit()

    receipt = _create_backup(_service(tmp_path, seeded_round.encryption_key), seeded_round)

    assert receipt.counts.answers == 1
    assert receipt.counts.score_ledgers == 0
    assert receipt.counts.option_stats == 0
    assert receipt.counts.rank_snapshots == 0


def test_artifact_suffix_rejects_path_traversal(
    tmp_path: Path,
    seeded_round: SeededRound,
) -> None:
    service = _service(tmp_path, seeded_round.encryption_key)

    with pytest.raises(
        RoundBackupConfigurationError,
        match="round_backup_artifact_suffix_invalid",
    ):
        service.create_and_verify(
            seeded_round.session,
            seeded_round.round,
            artifact_suffix="../outside",
            idempotency_key=IDEMPOTENCY_KEY,
        )

    assert list(tmp_path.iterdir()) == []


def test_backup_directory_is_prepared_only_when_backup_runs(
    tmp_path: Path,
    seeded_round: SeededRound,
) -> None:
    configured_directory = tmp_path / "round-backups"
    service = _service(configured_directory, seeded_round.encryption_key)
    real_directory = tmp_path / "redirected"
    real_directory.mkdir()
    configured_directory.symlink_to(real_directory, target_is_directory=True)

    with pytest.raises(
        RoundBackupConfigurationError,
        match="round_backup_directory_must_not_be_symlink",
    ):
        _create_backup(service, seeded_round)

    assert list(real_directory.iterdir()) == []


def _use_client_timing(seeded: SeededRound, *, client_ahead_tolerance_ms: int = 250) -> None:
    policy = ScoringPolicy(
        version="score-client-v1",
        timing_basis="client_monotonic_v1",
        arrival_grace_ms=2000,
        clock_tolerance_ms=500 if client_ahead_tolerance_ms == 2500 else 250,
        client_ahead_tolerance_ms=client_ahead_tolerance_ms,
        base_points=400,
        initial_speed_bonus=100,
        speed_bucket_ms=100,
        speed_decrement_per_bucket=1,
        standard_duration_ms=10000,
        is_approved=True,
        created_at=NOW,
    )
    event = seeded.session.get(Event, EVENT_ID)
    assert event is not None
    event.scoring_policy_version = policy.version
    participant = Participant(
        id=PARTICIPANT_ID, event_id=EVENT_ID, public_label="AAA", joined_at=NOW
    )
    # SQLiteで再取得するAnswerのnaive datetimeに合わせ、単体試験内の時刻を統一する。
    opened = NOW.replace(tzinfo=None)
    seeded.round.open_at = opened
    seeded.round.standard_close_at = opened + timedelta(seconds=10)
    seeded.round.revealed_at = opened + timedelta(seconds=17)
    answer = seeded.session.get(Answer, UUID(int=8))
    assert answer is not None
    answer.client_elapsed_ms = 2000
    answer.received_at = opened + timedelta(milliseconds=3500)
    seeded.score_ledger.scoring_policy_version = policy.version
    seeded.score_ledger.elapsed_ms = 2000
    seeded.session.add_all([policy, event, participant, answer, seeded.round, seeded.score_ledger])
    seeded.session.commit()


def test_client_backup_preserves_official_time_and_policy_snapshot(
    tmp_path: Path, seeded_round: SeededRound
) -> None:
    _use_client_timing(seeded_round)
    service = _service(tmp_path, seeded_round.encryption_key)
    receipt = _create_backup(service, seeded_round)
    fernet = Fernet(seeded_round.encryption_key)
    artifact = ROUND_BACKUP_ADAPTER.validate_json(
        fernet.decrypt((tmp_path / receipt.artifact_name).read_bytes())
    )

    assert isinstance(artifact, UnifiedRoundBackupArtifact)
    assert artifact.payload.answers[0].client_elapsed_ms == 2000
    assert artifact.payload.score_ledgers[0].elapsed_ms == 2000
    assert artifact.payload.scoring_policy.version == "score-client-v1"
    assert artifact.payload.scoring_policy.timing_basis == "client_monotonic_v1"
    assert artifact.payload.scoring_policy.arrival_grace_ms == 2000
    assert artifact.payload.scoring_policy.clock_tolerance_ms == 250
    assert artifact.payload.scoring_policy.client_ahead_tolerance_ms == 250
    assert canonical_json(ROUND_BACKUP_ADAPTER.validate_json(canonical_json(artifact))) == (
        canonical_json(artifact)
    )
    assert _create_backup(service, seeded_round).is_replay is True


def test_client_backup_preserves_current_timing_policy(
    tmp_path: Path, seeded_round: SeededRound
) -> None:
    _use_client_timing(seeded_round, client_ahead_tolerance_ms=2500)
    policy = seeded_round.session.get(ScoringPolicy, "score-client-v1")
    answer = seeded_round.session.get(Answer, UUID(int=8))
    assert policy is not None and answer is not None
    assert seeded_round.round.open_at is not None
    policy.arrival_grace_ms = 4000
    policy.clock_tolerance_ms = 10000
    answer.client_elapsed_ms = 0
    answer.received_at = seeded_round.round.open_at + timedelta(milliseconds=13_999)
    seeded_round.score_ledger.elapsed_ms = 0
    seeded_round.session.add_all([policy, answer, seeded_round.score_ledger])
    seeded_round.session.commit()

    receipt = _create_backup(_service(tmp_path, seeded_round.encryption_key), seeded_round)
    artifact = ROUND_BACKUP_ADAPTER.validate_json(
        Fernet(seeded_round.encryption_key).decrypt((tmp_path / receipt.artifact_name).read_bytes())
    )

    assert isinstance(artifact, UnifiedRoundBackupArtifact)
    assert artifact.payload.scoring_policy.arrival_grace_ms == 4000
    assert artifact.payload.scoring_policy.clock_tolerance_ms == 10000
    assert artifact.payload.scoring_policy.client_ahead_tolerance_ms == 2500
    assert artifact.payload.answers[0].client_elapsed_ms == 0


@pytest.mark.parametrize("invalid_source", ["missing_time", "ledger_time", "policy", "late"])
def test_client_backup_rejects_time_or_policy_mismatch_before_write(
    tmp_path: Path, seeded_round: SeededRound, invalid_source: str
) -> None:
    _use_client_timing(seeded_round)
    answer = seeded_round.session.get(Answer, UUID(int=8))
    assert answer is not None
    if invalid_source == "missing_time":
        answer.client_elapsed_ms = None
    elif invalid_source == "ledger_time":
        seeded_round.score_ledger.elapsed_ms = 3500
    elif invalid_source == "policy":
        seeded_round.score_ledger.scoring_policy_version = "score-v1"
    else:
        answer.received_at = answer.received_at + timedelta(seconds=10)
    seeded_round.session.add(answer)
    seeded_round.session.commit()

    with pytest.raises(RoundBackupSourceError, match="round_backup_source_contract_invalid"):
        _create_backup(_service(tmp_path, seeded_round.encryption_key), seeded_round)
    assert list(tmp_path.iterdir()) == []


def test_client_backup_rejects_valid_checksum_with_mismatched_ledger_time(
    tmp_path: Path, seeded_round: SeededRound
) -> None:
    _use_client_timing(seeded_round)
    service = _service(tmp_path, seeded_round.encryption_key)
    receipt = _create_backup(service, seeded_round)
    path = tmp_path / receipt.artifact_name
    fernet = Fernet(seeded_round.encryption_key)
    artifact = UnifiedRoundBackupArtifact.model_validate_json(fernet.decrypt(path.read_bytes()))
    altered_ledger = artifact.payload.score_ledgers[0].model_copy(update={"elapsed_ms": 3500})
    payload = artifact.payload.model_copy(update={"score_ledgers": [altered_ledger]})
    altered_artifact = artifact.model_copy(
        update={"payload": payload, "plaintext_sha256": plaintext_sha256(payload)}
    )
    path.write_bytes(fernet.encrypt(canonical_json(altered_artifact)))

    with pytest.raises(RoundBackupIntegrityError, match="round_backup_artifact_contract_invalid"):
        _create_backup(service, seeded_round)


@pytest.mark.parametrize("elapsed_ms", [None, 1249, 1251])
def test_server_backup_rejects_mismatched_official_time_before_write(
    tmp_path: Path, seeded_round: SeededRound, elapsed_ms: int | None
) -> None:
    seeded_round.score_ledger.elapsed_ms = elapsed_ms
    seeded_round.session.commit()

    with pytest.raises(RoundBackupSourceError, match="round_backup_source_contract_invalid"):
        _create_backup(_service(tmp_path, seeded_round.encryption_key), seeded_round)
    assert list(tmp_path.iterdir()) == []


def test_server_backup_rejects_valid_checksum_with_mismatched_ledger_time(
    tmp_path: Path, seeded_round: SeededRound
) -> None:
    service = _service(tmp_path, seeded_round.encryption_key)
    receipt = _create_backup(service, seeded_round)
    path = tmp_path / receipt.artifact_name
    fernet = Fernet(seeded_round.encryption_key)
    artifact = UnifiedRoundBackupArtifact.model_validate_json(fernet.decrypt(path.read_bytes()))
    altered_ledger = artifact.payload.score_ledgers[0].model_copy(update={"elapsed_ms": 1251})
    payload = artifact.payload.model_copy(update={"score_ledgers": [altered_ledger]})
    altered_artifact = artifact.model_copy(
        update={"payload": payload, "plaintext_sha256": plaintext_sha256(payload)}
    )
    path.write_bytes(fernet.encrypt(canonical_json(altered_artifact)))

    with pytest.raises(RoundBackupIntegrityError, match="round_backup_artifact_contract_invalid"):
        _create_backup(service, seeded_round)


def test_server_backup_uses_integer_milliseconds(tmp_path: Path, seeded_round: SeededRound) -> None:
    answer = seeded_round.session.get(Answer, UUID(int=8))
    assert answer is not None
    answer.received_at += timedelta(microseconds=999)
    seeded_round.session.add(answer)
    seeded_round.session.commit()
    receipt = _create_backup(_service(tmp_path, seeded_round.encryption_key), seeded_round)
    artifact = ROUND_BACKUP_ADAPTER.validate_json(
        Fernet(seeded_round.encryption_key).decrypt((tmp_path / receipt.artifact_name).read_bytes())
    )
    assert artifact.payload.score_ledgers[0].elapsed_ms == 1250


@pytest.mark.parametrize(
    ("tolerance_ms", "difference_ms", "accepted"),
    [
        (250, -251, False),
        (250, -250, True),
        (250, -249, True),
        (250, 2250, True),
        (250, 2251, False),
        (2500, -2501, False),
        (2500, -2500, True),
        (2500, -2499, True),
        (2500, 2499, True),
        (2500, 2500, True),
        (2500, 2501, False),
    ],
)
def test_backup_uses_saved_version_specific_difference_bounds(
    tmp_path: Path,
    seeded_round: SeededRound,
    tolerance_ms: int,
    difference_ms: int,
    accepted: bool,
) -> None:
    _use_client_timing(seeded_round, client_ahead_tolerance_ms=tolerance_ms)
    answer = seeded_round.session.get(Answer, UUID(int=8))
    assert answer is not None
    assert seeded_round.round.open_at is not None
    answer.client_elapsed_ms = 3000
    answer.received_at = seeded_round.round.open_at + timedelta(milliseconds=3000 + difference_ms)
    seeded_round.score_ledger.elapsed_ms = 3000
    seeded_round.session.add_all([answer, seeded_round.score_ledger])
    seeded_round.session.commit()
    service = _service(tmp_path, seeded_round.encryption_key)

    if not accepted:
        with pytest.raises(RoundBackupSourceError, match="round_backup_source_contract_invalid"):
            _create_backup(service, seeded_round)
        assert list(tmp_path.iterdir()) == []
        return

    receipt = _create_backup(service, seeded_round)
    plaintext = Fernet(seeded_round.encryption_key).decrypt(
        (tmp_path / receipt.artifact_name).read_bytes()
    )
    artifact = ROUND_BACKUP_ADAPTER.validate_json(plaintext)
    assert artifact.payload.answers[0].client_elapsed_ms == 3000
    assert canonical_json(artifact) == plaintext
    assert _create_backup(service, seeded_round).is_replay is True
    assert isinstance(artifact, UnifiedRoundBackupArtifact)
    assert artifact.format == "quiz-round-backup-v4"
    assert artifact.payload.scoring_policy.client_ahead_tolerance_ms == tolerance_ms
    assert artifact.payload.scoring_policy.clock_tolerance_ms == (
        500 if tolerance_ms == 2500 else 250
    )


def test_v4_backup_verification_rejects_invalid_time_with_recomputed_checksum(
    tmp_path: Path, seeded_round: SeededRound
) -> None:
    _use_client_timing(seeded_round, client_ahead_tolerance_ms=2500)
    service = _service(tmp_path, seeded_round.encryption_key)
    receipt = _create_backup(service, seeded_round)
    path = tmp_path / receipt.artifact_name
    fernet = Fernet(seeded_round.encryption_key)
    artifact = UnifiedRoundBackupArtifact.model_validate_json(fernet.decrypt(path.read_bytes()))
    altered_answer = artifact.payload.answers[0].model_copy(update={"client_elapsed_ms": 6001})
    altered_ledger = artifact.payload.score_ledgers[0].model_copy(update={"elapsed_ms": 6001})
    payload = artifact.payload.model_copy(
        update={"answers": [altered_answer], "score_ledgers": [altered_ledger]}
    )
    altered_artifact = artifact.model_copy(
        update={"payload": payload, "plaintext_sha256": plaintext_sha256(payload)}
    )
    path.write_bytes(fernet.encrypt(canonical_json(altered_artifact)))

    with pytest.raises(RoundBackupIntegrityError, match="round_backup_artifact_contract_invalid"):
        _create_backup(service, seeded_round)


@pytest.mark.parametrize(
    ("elapsed_ms", "received_ms", "accepted"),
    [
        (9999, 11999, True),
        (9999, 12000, False),
        (10000, 11999, False),
        (14999, 16999, False),
    ],
)
def test_v4_backup_keeps_selection_and_arrival_deadlines(
    tmp_path: Path,
    seeded_round: SeededRound,
    elapsed_ms: int,
    received_ms: int,
    accepted: bool,
) -> None:
    _use_client_timing(seeded_round, client_ahead_tolerance_ms=2500)
    answer = seeded_round.session.get(Answer, UUID(int=8))
    participant = seeded_round.session.get(Participant, PARTICIPANT_ID)
    assert answer is not None and participant is not None
    assert seeded_round.round.open_at is not None
    answer.client_elapsed_ms = elapsed_ms
    answer.received_at = seeded_round.round.open_at + timedelta(milliseconds=received_ms)
    seeded_round.score_ledger.elapsed_ms = elapsed_ms
    seeded_round.session.add_all([answer, participant, seeded_round.score_ledger])
    seeded_round.session.commit()
    service = _service(tmp_path, seeded_round.encryption_key)
    if accepted:
        assert _create_backup(service, seeded_round).is_replay is False
    else:
        with pytest.raises(RoundBackupSourceError, match="round_backup_source_contract_invalid"):
            _create_backup(service, seeded_round)
        assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("version", [1, 2, 3])
def test_archived_backup_original_bytes_still_validate(version: int) -> None:
    # HEAD時点の実装が架空fixtureから出力した原本。新形式への書換えをしない。
    original = (
        Path(__file__).parent / "fixtures" / "round-backups" / f"v{version}.json"
    ).read_bytes()
    artifact = ROUND_BACKUP_ADAPTER.validate_json(original)
    assert artifact.format == f"quiz-round-backup-v{version}"
    assert canonical_json(artifact) == original


def test_new_backup_has_no_retired_answer_fields(tmp_path: Path, seeded_round: SeededRound) -> None:
    _use_client_timing(seeded_round, client_ahead_tolerance_ms=2500)
    receipt = _create_backup(_service(tmp_path, seeded_round.encryption_key), seeded_round)
    raw = Fernet(seeded_round.encryption_key).decrypt(
        (tmp_path / receipt.artifact_name).read_bytes()
    )
    assert b"is_assisted" not in raw
    assert b"assisted_close_at" not in raw
    assert UnifiedRoundBackupArtifact.model_validate_json(raw).format == "quiz-round-backup-v4"
