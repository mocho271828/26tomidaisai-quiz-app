import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, Self, cast
from uuid import UUID

import pytest
from fastapi import Request
from sqlalchemy import Engine
from sqlmodel import Session

import quiz_app.operator.mutations as mutations_module
from quiz_app.domain.errors import InvalidStateTransitionError, RoundBackupFailedError
from quiz_app.domain.quiz_service import QuizDomainService
from quiz_app.domain.types import (
    AdvancedRound,
    CompletedEvent,
    RecoveredRoundResults,
    RoundBackupTransition,
)
from quiz_app.models import Event, IdempotencyRecord, OperationAudit, QuizRound
from quiz_app.models.enums import (
    AdminRole,
    EventState,
    RankingVisibility,
    RoundResultPublication,
    RoundState,
)
from quiz_app.operator.auth_service import OperatorPrincipal
from quiz_app.operator.control_contracts import (
    NextRoundMutationRequest,
    OperatorMutationRequest,
    OperatorMutationResponse,
)
from quiz_app.operator.error_handlers import _domain_error_handler
from quiz_app.operator.mutations import (
    MutationOutcome,
    OperatorMutationService,
    mutation_request_hash,
)
from quiz_app.quiz_configuration import ResultVisibility
from quiz_app.round_backup import (
    RoundBackupCounts,
    RoundBackupReceipt,
    RoundBackupService,
    RoundBackupWriteError,
)

pytestmark = pytest.mark.usefixtures("legacy_quiz_configuration")

ProgressionOperation = Literal["advance_round", "complete_event"]

ACCOUNT_ID = UUID("73000000-0000-4000-8000-000000000001")
SESSION_ID = UUID("73000000-0000-4000-8000-000000000002")
EVENT_ID = UUID("73000000-0000-4000-8000-000000000003")
OLD_ROUND_ID = UUID("73000000-0000-4000-8000-000000000004")
NEW_ROUND_ID = UUID("73000000-0000-4000-8000-000000000005")
OLD_QUESTION_ID = UUID("73000000-0000-4000-8000-000000000006")
NEXT_QUESTION_ID = UUID("73000000-0000-4000-8000-000000000007")
IDEMPOTENCY_KEY = UUID("73000000-0000-4000-8000-000000000008")
NOW = datetime(2026, 8, 21, 13, 0, tzinfo=UTC)
INTERNAL_BACKUP_SENTINEL = "INTERNAL-BACKUP-PATH-4d14e7"
PLAINTEXT_SHA256 = "a" * 64
BACKUP_COUNTS = RoundBackupCounts(
    rounds=1,
    questions=1,
    question_options=4,
    answers=2,
    score_ledgers=2,
    readiness_reports=1,
    preparation_targets=1,
    option_stats=4,
    rank_snapshots=1,
    rank_snapshot_rows=2,
)


@dataclass
class FakeDatabase:
    round: QuizRound
    event: Event
    committed_records: list[object] = field(default_factory=list)
    sessions: list["FakeSession"] = field(default_factory=list)
    commit_count: int = 0
    rollback_count: int = 0
    database_time: datetime = NOW


class FakeTransaction:
    def __init__(self, session: "FakeSession") -> None:
        self._session = session

    def __enter__(self) -> Self:
        return self

    def __exit__(self, error_type: object, _: object, __: object) -> bool:
        if error_type is None:
            self._session.database.committed_records.extend(self._session.pending_records)
            self._session.database.commit_count += 1
        else:
            self._session.database.rollback_count += 1
        self._session.pending_records.clear()
        return False


class FakeSession:
    def __init__(self, database: FakeDatabase) -> None:
        self.database = database
        self.pending_records: list[object] = []
        self.executed: list[tuple[str, dict[str, object] | None]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _: object, __: object, ___: object) -> None:
        return None

    def begin(self) -> FakeTransaction:
        return FakeTransaction(self)

    def execute(
        self,
        statement: object,
        parameters: dict[str, object] | None = None,
    ) -> None:
        self.executed.append((str(statement), parameters))

    def scalar(self, statement: object) -> object:
        sql = str(statement)
        if "FROM idempotency_records" in sql:
            return next(
                (
                    record
                    for record in self.database.committed_records
                    if isinstance(record, IdempotencyRecord)
                ),
                None,
            )
        if "FROM quiz_rounds" in sql:
            return self.database.round
        if "clock_timestamp" in sql:
            return self.database.database_time
        raise AssertionError(f"unexpected scalar query: {sql}")

    def get(self, model_type: type[Event], primary_key: UUID) -> Event | None:
        if model_type is Event and primary_key == self.database.event.id:
            return self.database.event
        return None

    def add(self, record: object) -> None:
        self.pending_records.append(record)


class FakeSessionFactory:
    def __init__(self, database: FakeDatabase) -> None:
        self._database = database

    def __call__(self, _: Engine) -> FakeSession:
        session = FakeSession(self._database)
        self._database.sessions.append(session)
        return session


@dataclass
class StubProgressionDomainService:
    round: QuizRound
    event: Event
    validate_next_count: int = 0
    begin_count: int = 0
    fail_count: int = 0
    advance_count: int = 0
    complete_count: int = 0
    retry_results_count: int = 0

    def validate_next_round_question_in_transaction(
        self,
        _: Session,
        round_id: UUID,
        question_id: UUID,
    ) -> None:
        assert round_id == self.round.id
        assert question_id == NEXT_QUESTION_ID
        self.validate_next_count += 1

    def begin_round_backup_in_transaction(
        self,
        _: Session,
        round_id: UUID,
        expected_version: int,
    ) -> RoundBackupTransition:
        assert round_id == self.round.id
        assert expected_version == self.round.version
        self.begin_count += 1
        self.round.state = RoundState.ROUND_BACKUP_PENDING
        self.round.version += 1
        return self._transition()

    def fail_round_backup_in_transaction(
        self,
        _: Session,
        round_id: UUID,
    ) -> RoundBackupTransition:
        assert round_id == self.round.id
        assert self.round.state is RoundState.ROUND_BACKUP_PENDING
        self.fail_count += 1
        self.round.state = RoundState.ROUND_BACKUP_FAILED
        self.round.version += 1
        self._bump_event()
        return self._transition()

    def advance_round_after_backup_in_transaction(
        self,
        _: Session,
        round_id: UUID,
        question_id: UUID,
    ) -> AdvancedRound:
        assert round_id == self.round.id
        assert question_id == NEXT_QUESTION_ID
        self.advance_count += 1
        self.round.state = RoundState.ROUND_BACKUP_VALIDATED
        self.round.version += 1
        self._bump_event()
        return AdvancedRound(
            event_id=self.event.id,
            event_version=self.event.version,
            previous_round_id=self.round.id,
            new_round_id=NEW_ROUND_ID,
            new_round_version=self.round.version + 1,
            new_round_state=RoundState.PREPARING_ASSETS,
        )

    def complete_event_after_backup_in_transaction(
        self,
        _: Session,
        round_id: UUID,
    ) -> CompletedEvent:
        assert round_id == self.round.id
        self.complete_count += 1
        self.round.state = RoundState.ROUND_BACKUP_VALIDATED
        self.round.version += 1
        self.event.state = EventState.COMPLETED
        self._bump_event()
        return CompletedEvent(
            event_id=self.event.id,
            event_version=self.event.version,
            round_id=self.round.id,
            round_version=self.round.version,
            round_state=self.round.state,
        )

    def retry_round_results_in_transaction(
        self,
        _: Session,
        round_id: UUID,
        expected_version: int,
    ) -> RecoveredRoundResults:
        assert round_id == self.round.id
        assert expected_version == self.round.version
        assert self.round.state is RoundState.VOID_CANDIDATE
        assert self.round.result_publication is RoundResultPublication.WITHHELD
        self.retry_results_count += 1
        self.round.state = RoundState.AWAITING_VALIDATION
        self.round.void_reason = None
        self.round.version += 1
        self._bump_event()
        return RecoveredRoundResults(
            event_id=self.event.id,
            event_version=self.event.version,
            round_id=self.round.id,
            round_version=self.round.version,
            round_state=self.round.state,
        )

    def _transition(self) -> RoundBackupTransition:
        return RoundBackupTransition(
            event_id=self.event.id,
            round_id=self.round.id,
            round_version=self.round.version,
            round_state=self.round.state,
        )

    def _bump_event(self) -> None:
        self.event.version += 1
        self.event.updated_at = NOW


class RecordingBackupService:
    def __init__(self, *, should_fail: bool) -> None:
        self.should_fail = should_fail
        self.calls: list[tuple[UUID, int, str, UUID]] = []

    def create_and_verify(
        self,
        _: Session,
        round_: QuizRound,
        *,
        artifact_suffix: str,
        idempotency_key: UUID,
    ) -> RoundBackupReceipt:
        self.calls.append((round_.id, round_.version, artifact_suffix, idempotency_key))
        if self.should_fail:
            raise RoundBackupWriteError(INTERNAL_BACKUP_SENTINEL)
        return RoundBackupReceipt(
            artifact_name=f"rb-fixture.{artifact_suffix}.fernet",
            plaintext_sha256=PLAINTEXT_SHA256,
            counts=BACKUP_COUNTS,
            round_id=round_.id,
            round_version=round_.version,
            is_replay=False,
        )


def _principal() -> OperatorPrincipal:
    return OperatorPrincipal(
        account_id=ACCOUNT_ID,
        session_id=SESSION_ID,
        username="operator",
        role=AdminRole.OPERATOR,
        csrf_hash="csrf-hash",
    )


def _round() -> QuizRound:
    return QuizRound(
        id=OLD_ROUND_ID,
        event_id=EVENT_ID,
        question_id=OLD_QUESTION_ID,
        version=7,
        state=RoundState.REVEAL,
        result_publication=RoundResultPublication.PUBLISHED,
        revealed_at=NOW,
    )


def _event() -> Event:
    return Event(
        id=EVENT_ID,
        slug="progression-mutation",
        title="[DEMO] progression mutation",
        state=EventState.IN_PROGRESS,
        ranking_visibility=RankingVisibility.HIDDEN_ALL,
        scoring_policy_version="score-v1",
        display_name_policy_version="name-v1",
        version=12,
    )


def _request(operation: ProgressionOperation) -> OperatorMutationRequest:
    values = {
        "expected_version": 7,
        "idempotency_key": IDEMPOTENCY_KEY,
    }
    if operation == "advance_round":
        return NextRoundMutationRequest(question_id=NEXT_QUESTION_ID, **values)
    return OperatorMutationRequest(**values)


def _invoke(
    service: OperatorMutationService,
    operation: ProgressionOperation,
    request: OperatorMutationRequest,
) -> OperatorMutationResponse:
    if operation == "advance_round":
        assert isinstance(request, NextRoundMutationRequest)
        return service.advance_round(_principal(), OLD_ROUND_ID, request)
    return service.complete_event(_principal(), OLD_ROUND_ID, request)


def _arrange(
    monkeypatch: pytest.MonkeyPatch,
    *,
    should_fail: bool,
) -> tuple[
    OperatorMutationService,
    FakeDatabase,
    StubProgressionDomainService,
    RecordingBackupService,
]:
    database = FakeDatabase(round=_round(), event=_event())
    domain = StubProgressionDomainService(database.round, database.event)
    backup = RecordingBackupService(should_fail=should_fail)
    monkeypatch.setattr(mutations_module, "Session", FakeSessionFactory(database))
    monkeypatch.setattr(mutations_module.logfire, "info", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mutations_module.logfire, "exception", lambda *_args, **_kwargs: None)
    service = OperatorMutationService(
        cast(Engine, object()),
        cast(QuizDomainService, domain),
        cast(RoundBackupService, backup),
    )
    return service, database, domain, backup


@pytest.mark.parametrize("operation", ["advance_round", "complete_event"])
def test_success_records_backup_audit_and_replays_without_recreating_artifact(
    monkeypatch: pytest.MonkeyPatch,
    operation: ProgressionOperation,
) -> None:
    service, database, domain, backup = _arrange(monkeypatch, should_fail=False)
    request = _request(operation)

    first = _invoke(service, operation, request)

    assert first.operation == operation
    assert first.is_replay is False
    assert backup.calls == [(OLD_ROUND_ID, 8, operation, IDEMPOTENCY_KEY)]
    audits = [record for record in database.committed_records if isinstance(record, OperationAudit)]
    records = [
        record for record in database.committed_records if isinstance(record, IdempotencyRecord)
    ]
    assert len(audits) == 2
    assert len(records) == 1
    backup_audit = next(audit for audit in audits if audit.operation == "validate_round_backup")
    operation_audit = next(audit for audit in audits if audit.operation == operation)
    assert backup_audit.target_type == "round_backup_artifact"
    assert backup_audit.target_id == PLAINTEXT_SHA256
    assert backup_audit.before_state == RoundState.ROUND_BACKUP_PENDING.value
    assert backup_audit.after_state == RoundState.ROUND_BACKUP_VALIDATED.value
    assert backup_audit.outcome == "succeeded"
    assert backup_audit.idempotency_key is None
    assert json.loads(backup_audit.reason) == {
        "artifact_name": f"rb-fixture.{operation}.fernet",
        "counts": BACKUP_COUNTS.model_dump(mode="json"),
        "plaintext_sha256": PLAINTEXT_SHA256,
        "round_version": 8,
    }
    assert operation_audit.target_type == (
        "quiz_round" if operation == "advance_round" else "event"
    )
    assert operation_audit.target_id == str(
        OLD_ROUND_ID if operation == "advance_round" else EVENT_ID
    )
    assert operation_audit.before_state == (
        RoundState.REVEAL.value if operation == "advance_round" else EventState.IN_PROGRESS.value
    )
    assert operation_audit.after_state == (
        RoundState.ROUND_BACKUP_VALIDATED.value
        if operation == "advance_round"
        else EventState.COMPLETED.value
    )
    assert operation_audit.outcome == "succeeded"
    assert operation_audit.idempotency_key == IDEMPOTENCY_KEY
    assert operation_audit.reason == ""

    record = records[0]
    assert record.operation == operation
    assert record.request_hash == mutation_request_hash(operation, OLD_ROUND_ID, request)
    assert record.response_status == 200
    assert OperatorMutationResponse.model_validate_json(record.response_json) == first
    assert record.resulting_version == first.round_version
    assert record.created_at == NOW
    assert record.expires_at == NOW + timedelta(hours=24)
    assert database.commit_count == 1
    assert database.rollback_count == 0

    replay = _invoke(service, operation, request)

    assert replay.is_replay is True
    assert replay.model_copy(update={"is_replay": False}) == first
    assert backup.calls == [(OLD_ROUND_ID, 8, operation, IDEMPOTENCY_KEY)]
    assert len(database.committed_records) == 3
    assert database.commit_count == 2
    assert domain.begin_count == 1
    assert domain.validate_next_count == (1 if operation == "advance_round" else 0)
    assert domain.advance_count == (1 if operation == "advance_round" else 0)
    assert domain.complete_count == (1 if operation == "complete_event" else 0)


@pytest.mark.parametrize("operation", ["advance_round", "complete_event"])
def test_expired_progression_refuses_before_backup_mutation(
    monkeypatch: pytest.MonkeyPatch,
    operation: ProgressionOperation,
) -> None:
    service, database, domain, backup = _arrange(monkeypatch, should_fail=False)
    request = _request(operation).model_copy(
        update={"execute_not_after": NOW - timedelta(microseconds=1)}
    )

    with pytest.raises(InvalidStateTransitionError, match="mutation_deadline_elapsed"):
        _invoke(service, operation, request)

    assert backup.calls == []
    assert domain.begin_count == 0
    assert database.commit_count == 0
    assert database.rollback_count == 1


def test_expired_result_retry_refuses_before_domain_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, database, domain, backup = _arrange(monkeypatch, should_fail=False)
    request = OperatorMutationRequest(
        expected_version=7,
        idempotency_key=IDEMPOTENCY_KEY,
        execute_not_after=NOW - timedelta(microseconds=1),
    )

    with pytest.raises(InvalidStateTransitionError, match="mutation_deadline_elapsed"):
        service.retry_round_results(_principal(), OLD_ROUND_ID, request)

    assert backup.calls == []
    assert domain.begin_count == 0
    assert database.commit_count == 0
    assert database.rollback_count == 1


def test_result_retry_rechecks_deadline_after_target_row_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, database, domain, backup = _arrange(monkeypatch, should_fail=False)
    request = OperatorMutationRequest(
        expected_version=7,
        idempotency_key=IDEMPOTENCY_KEY,
        execute_not_after=NOW + timedelta(seconds=1),
    )
    database_times = iter([NOW, NOW + timedelta(seconds=2)])
    monkeypatch.setattr(service, "_database_time", lambda _session: next(database_times))

    with pytest.raises(InvalidStateTransitionError, match="mutation_deadline_elapsed"):
        service.retry_round_results(_principal(), OLD_ROUND_ID, request)

    assert backup.calls == []
    assert domain.begin_count == 0
    assert database.commit_count == 0
    assert database.rollback_count == 1


def test_result_retry_is_audited_and_idempotently_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, database, domain, _backup = _arrange(monkeypatch, should_fail=False)
    database.round.state = RoundState.VOID_CANDIDATE
    database.round.result_publication = RoundResultPublication.WITHHELD
    database.round.void_reason = "automatic_aggregate_domain_failure:invalid_fixture"
    request = OperatorMutationRequest(
        expected_version=7,
        idempotency_key=IDEMPOTENCY_KEY,
    )

    first = service.retry_round_results(_principal(), OLD_ROUND_ID, request)
    replay = service.retry_round_results(_principal(), OLD_ROUND_ID, request)

    assert first.operation == "retry_round_results"
    assert first.round_state is RoundState.AWAITING_VALIDATION
    assert first.round_version == 8
    assert replay.is_replay is True
    assert replay.model_copy(update={"is_replay": False}) == first
    assert domain.retry_results_count == 1
    assert database.round.void_reason is None
    audits = [record for record in database.committed_records if isinstance(record, OperationAudit)]
    assert len(audits) == 1
    assert audits[0].operation == "retry_round_results"
    assert audits[0].before_state == RoundState.VOID_CANDIDATE.value
    assert audits[0].after_state == RoundState.AWAITING_VALIDATION.value
    assert audits[0].reason == ""


def test_regular_mutation_rolls_back_when_handler_crosses_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, database, _domain, _backup = _arrange(monkeypatch, should_fail=False)
    request = OperatorMutationRequest(
        expected_version=7,
        idempotency_key=IDEMPOTENCY_KEY,
        execute_not_after=NOW + timedelta(seconds=1),
    )
    database_times = iter([NOW, NOW + timedelta(seconds=2)])
    monkeypatch.setattr(service, "_database_time", lambda _session: next(database_times))
    handler_called = False

    def handler(_session: Session) -> MutationOutcome:
        nonlocal handler_called
        handler_called = True
        return MutationOutcome(
            response=OperatorMutationResponse(
                operation="retry_round_results",
                event_id=EVENT_ID,
                event_version=13,
                round_id=OLD_ROUND_ID,
                round_version=8,
                round_state=RoundState.REVEAL,
                result_visibility=ResultVisibility(
                    distribution=True, personal_ranking=False, overall_ranking=False
                ),
            ),
            event_id=EVENT_ID,
            round_id=OLD_ROUND_ID,
            target_type="quiz_round",
            target_id=str(OLD_ROUND_ID),
            before_state=RoundState.AWAITING_VALIDATION.value,
            after_state=RoundState.REVEAL.value,
        )

    with pytest.raises(InvalidStateTransitionError, match="mutation_deadline_elapsed"):
        service._execute(
            _principal(),
            operation="retry_round_results",
            target_id=OLD_ROUND_ID,
            request=request,
            handler=handler,
        )

    assert handler_called is True
    assert database.commit_count == 0
    assert database.rollback_count == 1
    assert database.committed_records == []


@pytest.mark.parametrize("operation", ["advance_round", "complete_event"])
def test_progression_rechecks_deadline_after_backup_before_domain_mutation(
    monkeypatch: pytest.MonkeyPatch,
    operation: ProgressionOperation,
) -> None:
    service, database, domain, backup = _arrange(monkeypatch, should_fail=False)
    request = _request(operation).model_copy(
        update={"execute_not_after": NOW + timedelta(seconds=1)}
    )
    database_times = iter([NOW, NOW, NOW + timedelta(seconds=2)])
    monkeypatch.setattr(service, "_database_time", lambda _session: next(database_times))

    with pytest.raises(InvalidStateTransitionError, match="mutation_deadline_elapsed"):
        _invoke(service, operation, request)

    assert backup.calls == [(OLD_ROUND_ID, 8, operation, IDEMPOTENCY_KEY)]
    assert domain.begin_count == 1
    assert domain.advance_count == 0
    assert domain.complete_count == 0
    assert database.commit_count == 0
    assert database.rollback_count == 1


@pytest.mark.parametrize("operation", ["advance_round", "complete_event"])
def test_failed_backup_rechecks_deadline_before_failure_state_mutation(
    monkeypatch: pytest.MonkeyPatch,
    operation: ProgressionOperation,
) -> None:
    service, database, domain, backup = _arrange(monkeypatch, should_fail=True)
    request = _request(operation).model_copy(
        update={"execute_not_after": NOW + timedelta(seconds=1)}
    )
    database_times = iter([NOW, NOW, NOW + timedelta(seconds=2)])
    monkeypatch.setattr(service, "_database_time", lambda _session: next(database_times))

    with pytest.raises(InvalidStateTransitionError, match="mutation_deadline_elapsed"):
        _invoke(service, operation, request)

    assert backup.calls == [(OLD_ROUND_ID, 8, operation, IDEMPOTENCY_KEY)]
    assert domain.begin_count == 1
    assert domain.fail_count == 0
    assert database.commit_count == 0
    assert database.rollback_count == 1


@pytest.mark.parametrize("operation", ["advance_round", "complete_event"])
def test_successful_progression_replays_after_deadline_without_backup_retry(
    monkeypatch: pytest.MonkeyPatch,
    operation: ProgressionOperation,
) -> None:
    service, database, domain, backup = _arrange(monkeypatch, should_fail=False)
    request = _request(operation).model_copy(
        update={"execute_not_after": NOW + timedelta(seconds=1)}
    )

    first = _invoke(service, operation, request)
    database.database_time = NOW + timedelta(seconds=2)
    replay = _invoke(service, operation, request)

    assert replay.is_replay is True
    assert replay.model_copy(update={"is_replay": False}) == first
    assert backup.calls == [(OLD_ROUND_ID, 8, operation, IDEMPOTENCY_KEY)]
    assert domain.begin_count == 1


@pytest.mark.parametrize("operation", ["advance_round", "complete_event"])
@pytest.mark.asyncio
async def test_backup_failure_commits_failed_state_and_safe_503_audit(
    monkeypatch: pytest.MonkeyPatch,
    operation: ProgressionOperation,
) -> None:
    service, database, domain, backup = _arrange(monkeypatch, should_fail=True)
    request = _request(operation)

    with pytest.raises(
        RoundBackupFailedError,
        match="round_backup_verification_failed",
    ) as raised:
        _invoke(service, operation, request)

    assert isinstance(raised.value.__cause__, RoundBackupWriteError)
    assert raised.value.event_id == EVENT_ID
    assert INTERNAL_BACKUP_SENTINEL not in str(raised.value)
    assert database.round.state is RoundState.ROUND_BACKUP_FAILED
    assert database.round.version == 9
    assert database.event.version == 13
    assert database.event.updated_at == NOW
    assert database.commit_count == 1
    assert database.rollback_count == 0
    assert backup.calls == [(OLD_ROUND_ID, 8, operation, IDEMPOTENCY_KEY)]
    assert domain.fail_count == 1
    assert domain.advance_count == 0
    assert domain.complete_count == 0

    assert len(database.committed_records) == 2
    audit = next(
        record for record in database.committed_records if isinstance(record, OperationAudit)
    )
    record = next(
        record for record in database.committed_records if isinstance(record, IdempotencyRecord)
    )
    assert audit.operation == operation
    assert audit.target_type == "quiz_round"
    assert audit.target_id == str(OLD_ROUND_ID)
    assert audit.before_state == RoundState.REVEAL.value
    assert audit.after_state == RoundState.ROUND_BACKUP_FAILED.value
    assert audit.reason == ""
    assert audit.outcome == "failed"
    assert audit.idempotency_key == IDEMPOTENCY_KEY
    assert audit.occurred_at == NOW
    assert record.operation == operation
    assert record.request_hash == mutation_request_hash(operation, OLD_ROUND_ID, request)
    assert record.response_status == 503
    assert json.loads(record.response_json) == {
        "code": "round_backup_failed",
        "event_id": str(EVENT_ID),
    }
    assert record.resulting_version == 9
    assert record.created_at == NOW
    assert record.expires_at == NOW + timedelta(hours=24)

    response = await _domain_error_handler(cast(Request, object()), raised.value)
    payload = json.loads(response.body)
    assert response.status_code == 503
    assert payload["code"] == "round_backup_failed"
    assert INTERNAL_BACKUP_SENTINEL not in response.body.decode("utf-8")

    with pytest.raises(
        RoundBackupFailedError,
        match="round_backup_verification_failed",
    ) as replayed:
        _invoke(service, operation, request)

    assert replayed.value.__cause__ is None
    assert replayed.value.event_id == EVENT_ID
    assert backup.calls == [(OLD_ROUND_ID, 8, operation, IDEMPOTENCY_KEY)]
    assert len(database.committed_records) == 2
    assert database.commit_count == 1
    assert database.rollback_count == 1
    assert domain.begin_count == 1
    assert domain.fail_count == 1
