import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import product
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import DateTime, Engine, create_engine, func, literal, select
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import ORMExecuteState, SessionTransaction
from sqlmodel import Session, SQLModel

import quiz_app.operator.mutations as mutations_module
import quiz_app.quiz_configuration.persistence as configuration_module
from quiz_app.audience.auth_service import ParticipantPrincipal
from quiz_app.audience.projection import AudienceProjectionService
from quiz_app.audience.result_batch import ResultBatchKey, load_result_batch
from quiz_app.audience.result_delivery import result_revision
from quiz_app.domain.errors import (
    DomainInvariantError,
    InvalidStateTransitionError,
    VersionConflictError,
)
from quiz_app.domain.quiz_service import QuizDomainService
from quiz_app.models import (
    Event,
    EventQuizConfiguration,
    IdempotencyRecord,
    OperationAudit,
    Question,
    QuestionOption,
    QuizRound,
    RankSnapshot,
    RankSnapshotRow,
    RoundOptionStat,
    ScoreLedger,
)
from quiz_app.models.enums import (
    AdminRole,
    EventState,
    RoundResultPublication,
    RoundState,
    SpeedCohort,
)
from quiz_app.operator.auth_service import OperatorPrincipal
from quiz_app.operator.control_contracts import ResultVisibilityMutationRequest
from quiz_app.operator.errors import IdempotencyKeyConflictError
from quiz_app.operator.mutations import OperatorMutationService
from quiz_app.operator.projection import OperatorProjectionService
from quiz_app.quiz_configuration import (
    QuizSettings,
    configured_scoring_policy,
    get_quiz_settings,
    initial_event_configuration,
)

NOW = datetime(2026, 9, 23, tzinfo=UTC)
DATABASE_NOW = NOW.replace(tzinfo=None)
EVENT_ID = UUID(int=80_001)
PARTICIPANT_ID = UUID(int=80_002)


@dataclass(frozen=True)
class VisibilityFixture:
    engine: Engine
    settings: QuizSettings
    domain: QuizDomainService


@pytest.fixture
def visibility_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[VisibilityFixture]:
    payload = get_quiz_settings().model_dump(mode="json")
    for target in ("distribution", "personal_ranking", "overall_ranking"):
        payload["visibility"][target] = {
            "mode": "automatic",
            "initially_visible": True,
            "changes": [{"after_round": 2, "visible": False}, {"after_round": 4, "visible": True}],
        }
    settings = QuizSettings.model_validate_json(json.dumps(payload))
    monkeypatch.setattr(configuration_module, "get_quiz_settings", lambda: settings)
    engine = create_engine("sqlite://")

    @sqlalchemy_event.listens_for(engine, "connect")
    def register_clock(connection: object, _: object) -> None:
        assert isinstance(connection, sqlite3.Connection)
        connection.create_function(
            "clock_timestamp", 0, lambda: DATABASE_NOW.strftime("%Y-%m-%d %H:%M:%S.%f")
        )

    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        # SQLiteではPostgreSQLの部分index条件を再現できないため、fixture内だけ外す。
        connection.exec_driver_sql("DROP INDEX uq_quiz_rounds_one_active_per_event")
    with Session(engine) as session, session.begin():
        session.add(configured_scoring_policy(settings))
        session.add(
            Event(
                id=EVENT_ID,
                slug="visibility-test",
                title="表示テスト",
                state=EventState.IN_PROGRESS,
                scoring_policy_version=settings.scoring.version,
                display_name_policy_version="display-test",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(initial_event_configuration(EVENT_ID, settings))
    domain = QuizDomainService(engine)
    monkeypatch.setattr(domain, "_database_time", lambda _session: DATABASE_NOW)
    yield VisibilityFixture(engine, settings, domain)
    engine.dispose()


def _round(session: Session, number: int, *, is_published: bool) -> QuizRound:
    question = Question(
        event_id=EVENT_ID,
        version=number,
        display_order=number,
        prompt=f"問題{number}",
    )
    option = QuestionOption(
        question_id=question.id,
        label="A",
        content="正解",
        display_order=0,
        is_correct=True,
    )
    round_ = QuizRound(
        event_id=EVENT_ID,
        question_id=question.id,
        version=number * 10,
        state=RoundState.ROUND_BACKUP_VALIDATED if is_published else RoundState.AWAITING_VALIDATION,
        result_publication=(
            RoundResultPublication.PUBLISHED if is_published else RoundResultPublication.WITHHELD
        ),
        open_at=None if is_published else DATABASE_NOW - timedelta(milliseconds=14_000),
        standard_close_at=None if is_published else DATABASE_NOW - timedelta(milliseconds=4_000),
    )
    session.add_all([question, option, round_])
    if is_published:
        session.add(
            RoundOptionStat(
                round_id=round_.id,
                option_id=option.id,
                valid_response_count=1,
                response_percentage=100,
            )
        )
        snapshot = RankSnapshot(event_id=EVENT_ID, round_id=round_.id, version=number)
        session.add_all(
            [
                snapshot,
                RankSnapshotRow(
                    snapshot_id=snapshot.id,
                    public_label="ABC",
                    overall_rank=1,
                    total_points=500,
                    correct_count=1,
                ),
                ScoreLedger(
                    round_id=round_.id,
                    participant_id=PARTICIPANT_ID,
                    scoring_policy_version=get_quiz_settings().scoring.version,
                    is_correct=True,
                    base_points=400,
                    speed_points=100,
                    total_points=500,
                    elapsed_ms=0,
                    speed_rank=1,
                    speed_cohort_size=1,
                    speed_cohort=SpeedCohort.CORRECT,
                ),
            ]
        )
    return round_


def _publish_pending(
    fixture: VisibilityFixture, session: Session, round_id: UUID, version: int
) -> None:
    aggregated = fixture.domain.aggregate_round_results_in_transaction(session, round_id, version)
    fixture.domain.publish_round_results_in_transaction(session, round_id, aggregated.round_version)


@pytest.mark.parametrize("distribution,personal,overall", list(product((False, True), repeat=3)))
def test_all_visibility_combinations_omit_private_data_on_both_audiences(
    visibility_fixture: VisibilityFixture,
    monkeypatch: pytest.MonkeyPatch,
    distribution: bool,
    personal: bool,
    overall: bool,
) -> None:
    fixture = visibility_fixture
    with Session(fixture.engine) as session, session.begin():
        _round(session, 1, is_published=True)
        record = session.get(EventQuizConfiguration, EVENT_ID)
        assert record is not None
        record.distribution_visible = distribution
        record.personal_ranking_visible = personal
        record.overall_ranking_visible = overall
    service = AudienceProjectionService(fixture.engine)
    monkeypatch.setattr(service, "_database_time", lambda _session: NOW)
    principal = ParticipantPrincipal(
        participant_id=PARTICIPANT_ID,
        session_id=UUID(int=80_003),
        event_id=EVENT_ID,
        public_label="ABC",
        is_ranking_eligible=True,
    )
    participant = service.get_participant_state(principal).model_dump(mode="json")

    for response in (participant,):
        result = response["latest_result"]["result"]
        assert result["correct_option_id"]
        assert (result["distribution"] is not None) is distribution
        if not distribution:
            assert "valid_answer_count" not in json.dumps(result)
            assert "valid_response_count" not in json.dumps(result)
            assert "response_percentage" not in json.dumps(result)
    assert "outcome" not in participant["latest_result"]["result"]
    assert "ranking" not in participant
    common = service.get_common_state(EVENT_ID)
    revision = result_revision(common)
    assert revision is not None
    batch = load_result_batch(fixture.engine, ResultBatchKey.from_revision(revision))
    personal_result = batch.for_participant(principal, revision).model_dump(mode="json")
    outcome = personal_result["outcome"]
    assert outcome["total_points"] == 500
    assert outcome["is_correct"] is True
    assert outcome["speed_rank"] == (1 if personal else None)
    assert outcome["speed_cohort_size"] == (1 if personal else None)
    assert (personal_result["ranking"] is not None) is personal
    assert "rows" not in (personal_result["ranking"] or {})


def test_publish_uses_question_number_and_replays_never_hide_manual_restore(
    visibility_fixture: VisibilityFixture,
) -> None:
    fixture = visibility_fixture
    with Session(fixture.engine, expire_on_commit=False) as session, session.begin():
        _round(session, 1, is_published=True)
        pending = _round(session, 2, is_published=False)
        pending_id = pending.id
    with Session(fixture.engine) as session, session.begin():
        _publish_pending(fixture, session, pending_id, 20)
    with Session(fixture.engine) as session:
        record = session.get(EventQuizConfiguration, EVENT_ID)
        event = session.get(Event, EVENT_ID)
        assert record is not None and event is not None
        assert record.last_applied_round == 2
        assert record.overall_ranking_visible is False
        assert session.scalar(select(func.count()).select_from(RankSnapshot)) == 2
        event_version = event.version
    fixture.domain.set_result_visibility(EVENT_ID, event_version, "overall_ranking", True)
    with Session(fixture.engine) as session, session.begin():
        pending = session.get(QuizRound, pending_id)
        assert pending is not None
        with pytest.raises(InvalidStateTransitionError):
            fixture.domain.publish_round_results_in_transaction(
                session, pending_id, pending.version
            )
        third = _round(session, 3, is_published=False)
        third_id = third.id
    with Session(fixture.engine) as session, session.begin():
        _publish_pending(fixture, session, third_id, 30)
    with Session(fixture.engine) as session:
        record = session.get(EventQuizConfiguration, EVENT_ID)
        assert record is not None
        assert record.last_applied_round == 3
        assert record.overall_ranking_visible is True
        assert record.personal_ranking_visible is False
    with Session(fixture.engine) as session, session.begin():
        fourth = _round(session, 4, is_published=False)
        fourth_id = fourth.id
    with Session(fixture.engine) as session, session.begin():
        _publish_pending(fixture, session, fourth_id, 40)
    with Session(fixture.engine) as session:
        record = session.get(EventQuizConfiguration, EVENT_ID)
        assert record is not None
        assert record.last_applied_round == 4
        assert record.distribution_visible is True
        assert record.personal_ranking_visible is True


def test_publication_failure_rolls_back_visibility_but_preserves_aggregated_results(
    visibility_fixture: VisibilityFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = visibility_fixture
    with Session(fixture.engine, expire_on_commit=False) as session, session.begin():
        _round(session, 1, is_published=True)
        pending = _round(session, 2, is_published=False)
        pending_id = pending.id
    with Session(fixture.engine) as session, session.begin():
        aggregated = fixture.domain.aggregate_round_results_in_transaction(session, pending_id, 20)

    def fail_after_visibility(_event: Event, _time: datetime) -> None:
        raise DomainInvariantError("simulated_failure_after_visibility")

    monkeypatch.setattr(fixture.domain, "_bump_event", fail_after_visibility)
    with pytest.raises(DomainInvariantError, match="simulated_failure_after_visibility"):
        with Session(fixture.engine) as session, session.begin():
            fixture.domain.publish_round_results_in_transaction(
                session, pending_id, aggregated.round_version
            )
    with Session(fixture.engine) as session:
        record = session.get(EventQuizConfiguration, EVENT_ID)
        pending = session.get(QuizRound, pending_id)
        assert record is not None and pending is not None
        assert record.last_applied_round == 0
        assert record.distribution_visible is True
        assert pending.result_publication is RoundResultPublication.WITHHELD
        assert pending.state is RoundState.READY_TO_REVEAL
        assert session.scalar(select(func.count()).select_from(RankSnapshot)) == 2
        assert session.scalar(select(func.count()).select_from(RoundOptionStat)) == 2


class _AdvisoryIgnoringSession(Session):
    def execute(self, statement: object, *args: object, **kwargs: object) -> object:
        if "pg_advisory_xact_lock" in str(statement):
            return None
        return super().execute(statement, *args, **kwargs)


def test_manual_changes_are_versioned_idempotent_audited_and_persisted(
    visibility_fixture: VisibilityFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = visibility_fixture
    monkeypatch.setattr(mutations_module, "Session", _AdvisoryIgnoringSession)
    service = OperatorMutationService(fixture.engine, fixture.domain)
    monkeypatch.setattr(service, "_database_time", lambda _session: DATABASE_NOW)
    principal = OperatorPrincipal(
        account_id=UUID(int=80_004),
        session_id=UUID(int=80_005),
        username="operator",
        role=AdminRole.OPERATOR,
        csrf_hash="test-hash",
    )
    request = ResultVisibilityMutationRequest(
        expected_version=1,
        idempotency_key=uuid4(),
        target="distribution",
        visible=False,
    )
    first = service.set_result_visibility(principal, EVENT_ID, request)
    replay = service.set_result_visibility(principal, EVENT_ID, request)
    assert first.event_version == 2
    assert first.result_visibility.distribution is False
    assert first.result_visibility.personal_ranking is True
    assert replay.is_replay is True
    assert replay.event_version == first.event_version
    with pytest.raises(IdempotencyKeyConflictError):
        service.set_result_visibility(
            principal, EVENT_ID, request.model_copy(update={"visible": True})
        )
    with pytest.raises(VersionConflictError):
        service.set_result_visibility(
            principal, EVENT_ID, request.model_copy(update={"idempotency_key": uuid4()})
        )
    restored = service.set_result_visibility(
        principal,
        EVENT_ID,
        request.model_copy(
            update={
                "expected_version": 2,
                "idempotency_key": uuid4(),
                "visible": True,
            }
        ),
    )
    assert restored.event_version == 3
    with Session(fixture.engine) as session:
        record = session.get(EventQuizConfiguration, EVENT_ID)
        assert record is not None and record.distribution_visible is True
        audits = session.scalars(select(OperationAudit).order_by(OperationAudit.after_state)).all()
        assert len(audits) == 2
        assert audits[0].before_state == "true"
        assert audits[0].after_state == "false"
        assert audits[0].target_id == f"{EVENT_ID}:distribution"
        assert audits[0].operation == "set_result_visibility"
        assert session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 2


def test_manual_change_locks_event_before_configuration_write() -> None:
    class CapturingSession:
        statement: object | None = None

        def scalar(self, statement: object) -> Event:
            self.statement = statement
            return Event(
                id=EVENT_ID,
                slug="test",
                title="test",
                scoring_policy_version="test",
                display_name_policy_version="test",
            )

    session = CapturingSession()
    QuizDomainService._event_for_update(cast(Session, session), EVENT_ID)
    assert "FOR UPDATE" in str(session.statement)


@pytest.mark.parametrize("audience", ["participant", "operator"])
def test_projection_holds_event_share_lock_until_all_visibility_and_result_reads_complete(
    visibility_fixture: VisibilityFixture,
    monkeypatch: pytest.MonkeyPatch,
    audience: str,
) -> None:
    """FOR SHAREの取得順序とtransaction境界を検証し、publish/manualの割込みを防ぐ。"""
    fixture = visibility_fixture
    reads: dict[Session, list[str]] = {}

    def record_statement(execution: ORMExecuteState) -> None:
        if execution.is_select:
            reads.setdefault(execution.session, []).append(
                str(execution.statement.compile(dialect=postgresql.dialect()))
            )

    def record_transaction_end(session: Session, transaction: SessionTransaction) -> None:
        if transaction.parent is None:
            reads.setdefault(session, []).append("TRANSACTION_END")

    sqlalchemy_event.listen(Session, "do_orm_execute", record_statement)
    sqlalchemy_event.listen(Session, "after_transaction_end", record_transaction_end)
    try:
        if audience == "operator":
            monkeypatch.setattr(
                "quiz_app.operator.projection.func.clock_timestamp",
                lambda: literal(NOW, type_=DateTime(timezone=True)),
            )
            OperatorProjectionService(fixture.engine).get_event_state(EVENT_ID)
        else:
            projection = AudienceProjectionService(fixture.engine)

            def database_time(session: Session) -> datetime:
                reads.setdefault(session, []).append("DB_CLOCK_SAMPLE")
                return NOW

            monkeypatch.setattr(projection, "_database_time", database_time)
            projection.get_participant_state(
                ParticipantPrincipal(
                    participant_id=PARTICIPANT_ID,
                    session_id=UUID(int=80_003),
                    event_id=EVENT_ID,
                    public_label="ABC",
                    is_ranking_eligible=True,
                )
            )
    finally:
        sqlalchemy_event.remove(Session, "do_orm_execute", record_statement)
        sqlalchemy_event.remove(Session, "after_transaction_end", record_transaction_end)

    # singleton確認はlock前でよいが、公開範囲と結果は各transactionのlock内で読む。
    assert len(reads) == (2 if audience == "participant" else 1)
    protected_tables = (
        "event_quiz_configurations",
        "quiz_rounds",
        "questions",
        "question_options",
        "round_option_stats",
        "rank_snapshots",
        "rank_snapshot_rows",
        "score_ledgers",
    )
    for transaction_reads in reads.values():
        lock_reads = [
            index
            for index, query in enumerate(transaction_reads)
            if "FROM events" in query and "FOR SHARE" in query
        ]
        assert len(lock_reads) == 1
        assert transaction_reads.count("TRANSACTION_END") == 1
        assert transaction_reads[-1] == "TRANSACTION_END"
        for index, query in enumerate(transaction_reads):
            if any(f"FROM {table}" in query for table in protected_tables):
                assert lock_reads[0] < index < len(transaction_reads) - 1
    all_queries = [query for transaction_reads in reads.values() for query in transaction_reads]
    assert any("FROM event_quiz_configurations" in query for query in all_queries)
    assert any("FROM quiz_rounds" in query for query in all_queries)
    common_reads = next(iter(reads.values()))
    if audience == "participant":
        assert common_reads[-2] == "DB_CLOCK_SAMPLE"
    else:
        # このfixtureのDB clockはliteralへ置き換えている。最後のSQLが時計だけとなる。
        assert "SELECT" in common_reads[-2] and "FROM" not in common_reads[-2]


@pytest.mark.parametrize("void_phase", ["unscheduled", "before_open", "after_open"])
def test_automatic_visibility_uses_displayed_round_number_after_void(
    visibility_fixture: VisibilityFixture,
    void_phase: str,
) -> None:
    fixture = visibility_fixture
    with Session(fixture.engine, expire_on_commit=False) as session, session.begin():
        voided = _round(session, 1, is_published=False)
        voided.state = RoundState.VOIDED
        if void_phase == "unscheduled":
            voided.open_at = None
            voided.standard_close_at = None
        else:
            assert voided.open_at is not None
            session.add(
                OperationAudit(
                    event_id=EVENT_ID,
                    round_id=voided.id,
                    operation="void_round",
                    target_type="quiz_round",
                    target_id=str(voided.id),
                    before_state=RoundState.SCHEDULED.value,
                    after_state=RoundState.VOIDED.value,
                    outcome="succeeded",
                    reason="出題取消の境界確認",
                    occurred_at=voided.open_at
                    + timedelta(seconds=1 if void_phase == "after_open" else -1),
                )
            )
        pending = _round(session, 2, is_published=False)
        pending_id = pending.id
    with Session(fixture.engine) as session, session.begin():
        _publish_pending(fixture, session, pending_id, 20)
    with Session(fixture.engine) as session:
        record = session.get(EventQuizConfiguration, EVENT_ID)
        event = session.get(Event, EVENT_ID)
        assert record is not None and event is not None
        # 公開snapshotは同じ1件でも、出題後VOIDだけが画面と自動変更の問数へ加算される。
        assert session.scalar(select(func.count()).select_from(RankSnapshot)) == 1
        assert record.last_applied_round == (2 if void_phase == "after_open" else 1)
        assert record.distribution_visible is (void_phase != "after_open")
        event_version = event.version
    fixture.domain.set_result_visibility(EVENT_ID, event_version, "distribution", True)
    with Session(fixture.engine, expire_on_commit=False) as session, session.begin():
        following = _round(session, 3, is_published=False)
        following_id = following.id
    with Session(fixture.engine) as session, session.begin():
        _publish_pending(fixture, session, following_id, 30)
    with Session(fixture.engine) as session:
        record = session.get(EventQuizConfiguration, EVENT_ID)
        assert record is not None
        assert record.last_applied_round == (3 if void_phase == "after_open" else 2)
        # 第2問後の手動再表示は第3問でも続き、開始前VOIDではこの公開が第2問になる。
        assert record.distribution_visible is (void_phase == "after_open")


def test_aggregated_preview_does_not_change_visibility_or_participant_ranking(
    visibility_fixture: VisibilityFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = visibility_fixture
    with Session(fixture.engine, expire_on_commit=False) as session, session.begin():
        published = _round(session, 1, is_published=True)
        pending = _round(session, 2, is_published=False)
        published_id, pending_id = published.id, pending.id
        record = session.get(EventQuizConfiguration, EVENT_ID)
        assert record is not None
        record.personal_ranking_visible = True
        record.overall_ranking_visible = True
    with Session(fixture.engine) as session, session.begin():
        fixture.domain.aggregate_round_results_in_transaction(session, pending_id, 20)
    with Session(fixture.engine) as session:
        record = session.get(EventQuizConfiguration, EVENT_ID)
        assert record is not None
        assert record.last_applied_round == 0
        assert record.personal_ranking_visible is True
        assert record.overall_ranking_visible is True
        assert session.scalar(select(func.count()).select_from(RankSnapshot)) == 2
    audience = AudienceProjectionService(fixture.engine)
    monkeypatch.setattr(audience, "_database_time", lambda _session: NOW)
    # SQLiteはtimezoneを保持しないため、同じsessionでPostgreSQL相当の値に揃える。
    with Session(fixture.engine) as session:
        pending_round = session.get(QuizRound, pending_id)
        event = session.get(Event, EVENT_ID)
        assert pending_round is not None and event is not None
        assert pending_round.open_at is not None and pending_round.standard_close_at is not None
        pending_round.open_at = pending_round.open_at.replace(tzinfo=UTC)
        pending_round.standard_close_at = pending_round.standard_close_at.replace(tzinfo=UTC)
        common = audience._build_common_snapshot(session, event).state
    assert common.latest_result is not None
    assert common.latest_result.round_id == published_id
    assert common.latest_ranking_snapshot_version == 1
    assert common.current_round is not None
    assert common.current_round.round_id == pending_id
    assert "correct_option_id" not in common.current_round.model_dump_json()
    operator_ranking = OperatorProjectionService(fixture.engine).get_latest_ranking(EVENT_ID)
    assert operator_ranking is not None
    assert operator_ranking.round_id == pending_id
    assert operator_ranking.snapshot_version == 2
    # 内部keyに未公開版が混入しても、本人結果の読取り側で公開境界を守る。
    with pytest.raises(DomainInvariantError, match="personal_result_ranking_missing"):
        load_result_batch(fixture.engine, ResultBatchKey(EVENT_ID, None, None, 2))
