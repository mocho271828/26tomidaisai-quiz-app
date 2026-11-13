import json
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import DateTime, func, literal, select
from sqlmodel import Session
from test_result_visibility import (
    DATABASE_NOW,
    EVENT_ID,
    VisibilityFixture,
    _AdvisoryIgnoringSession,
    _round,
)
from test_result_visibility import visibility_fixture as visibility_fixture

import quiz_app.operator.mutations as mutations_module
from quiz_app.domain.errors import (
    DomainInvariantError,
    InvalidStateTransitionError,
    VersionConflictError,
)
from quiz_app.domain.final_round import set_final_round_in_transaction
from quiz_app.models import (
    Event,
    EventQuizConfiguration,
    IdempotencyRecord,
    OperationAudit,
    QuizRound,
)
from quiz_app.models.enums import AdminRole, EventState, RoundResultPublication, RoundState
from quiz_app.operator.auth_service import OperatorPrincipal
from quiz_app.operator.errors import IdempotencyKeyConflictError
from quiz_app.operator.final_round import FinalRoundService
from quiz_app.operator.final_round_contracts import FinalRoundMutationRequest
from quiz_app.operator.mutations import OperatorMutationService
from quiz_app.quiz_configuration import QuizSettings, require_event_configuration
from quiz_app.quiz_configuration.persistence import set_final_round_configuration


@pytest.fixture
def current_round(visibility_fixture: VisibilityFixture, monkeypatch: pytest.MonkeyPatch) -> UUID:
    monkeypatch.setattr(
        "quiz_app.domain.final_round.func.clock_timestamp",
        lambda: literal(DATABASE_NOW, type_=DateTime()),
    )
    with Session(visibility_fixture.engine) as session, session.begin():
        round_ = _round(session, 1, is_published=False)
        round_.state = RoundState.PREPARING_ASSETS
        return round_.id


def _change(session: Session, round_id: UUID, *, version: int = 1, is_final: bool = True) -> int:
    return set_final_round_in_transaction(
        session,
        event_id=EVENT_ID,
        round_id=round_id,
        expected_version=version,
        is_final=is_final,
    ).event_version


def test_final_round_persists_without_changing_static_configuration_or_publication(
    visibility_fixture: VisibilityFixture, current_round: UUID
) -> None:
    fixture = visibility_fixture
    with Session(fixture.engine) as session, session.begin():
        record = session.get(EventQuizConfiguration, EVENT_ID)
        assert record is not None
        original = record.settings_json
        assert _change(session, current_round) == 2
        saved = json.loads(record.settings_json)
        assert saved.pop("final_round_id") == str(current_round)
        assert saved == json.loads(original)
        round_ = session.get(QuizRound, current_round)
        assert round_ is not None
        assert round_.state is RoundState.PREPARING_ASSETS
        assert round_.result_publication is RoundResultPublication.WITHHELD
    # 新しいservice instanceでも共有DBの設定を読み、process memoryを正本にしない。
    selection = FinalRoundService(fixture.engine).get_final_round(EVENT_ID)
    assert selection.final_round_id == current_round
    assert selection.event_version == 2
    with Session(fixture.engine) as session, session.begin():
        assert _change(session, current_round, version=2) == 2
        assert _change(session, current_round, version=2, is_final=False) == 3
        assert _change(session, current_round, version=3, is_final=False) == 3
        record = session.get(EventQuizConfiguration, EVENT_ID)
        assert record is not None and record.settings_json == original


@pytest.mark.parametrize("state", list(EventState))
def test_final_round_requires_in_progress_event(
    visibility_fixture: VisibilityFixture, current_round: UUID, state: EventState
) -> None:
    with Session(visibility_fixture.engine) as session, session.begin():
        event = session.get(Event, EVENT_ID)
        assert event is not None
        event.state = state
        if state is EventState.IN_PROGRESS:
            assert _change(session, current_round) == 2
        else:
            with pytest.raises(InvalidStateTransitionError, match="in_progress"):
                _change(session, current_round)
            assert event.version == 1


def test_final_round_rejects_stale_version_wrong_round_and_voided_round(
    visibility_fixture: VisibilityFixture, current_round: UUID
) -> None:
    with Session(visibility_fixture.engine) as session, session.begin():
        with pytest.raises(VersionConflictError):
            _change(session, current_round, version=2)
        with pytest.raises(InvalidStateTransitionError, match="must_be_current"):
            _change(session, UUID(int=999))
        round_ = session.get(QuizRound, current_round)
        assert round_ is not None
        round_.state = RoundState.VOIDED
        with pytest.raises(InvalidStateTransitionError, match="must_not_be_voided"):
            _change(session, current_round)


def test_final_round_replacement_does_not_let_new_round_clear_previous_selection(
    visibility_fixture: VisibilityFixture, current_round: UUID
) -> None:
    with Session(visibility_fixture.engine) as session, session.begin():
        assert _change(session, current_round) == 2
        next_round = _round(session, 2, is_published=False)
        session.flush()
        with pytest.raises(InvalidStateTransitionError, match="must_be_current"):
            _change(session, current_round, version=2, is_final=False)
        with pytest.raises(InvalidStateTransitionError, match="clear_target_mismatch"):
            _change(session, next_round.id, version=2, is_final=False)
        assert _change(session, next_round.id, version=2) == 3


def test_saved_final_round_keeps_strict_static_settings_and_never_repairs_reads(
    visibility_fixture: VisibilityFixture, current_round: UUID
) -> None:
    fixture = visibility_fixture
    with Session(fixture.engine) as session:
        event = session.get(Event, EVENT_ID)
        assert event is not None
        configuration = require_event_configuration(session, event)
        original = configuration.record.settings_json
        assert configuration.final_round_id is None
        set_final_round_configuration(configuration, current_round)
        saved = configuration.record.settings_json
        loaded = require_event_configuration(session, event)
        assert loaded.final_round_id == current_round
        assert loaded.settings == fixture.settings
        assert configuration.record.settings_json == saved
        # 保存専用fieldを起動設定へ混ぜる変更は引き続き拒否する。
        with pytest.raises(ValidationError):
            QuizSettings.model_validate_json(saved)
        for extra in ({"unexpected": True}, {"final_round_id": "invalid"}):
            configuration.record.settings_json = json.dumps(json.loads(original) | extra)
            with pytest.raises(DomainInvariantError, match="configuration_invalid"):
                require_event_configuration(session, event)
        configuration.record.settings_json = json.dumps(
            json.loads(saved) | {"start_delay_seconds": 7}
        )
        with pytest.raises(DomainInvariantError, match="configuration_mismatch"):
            require_event_configuration(session, event)


def test_final_round_mutations_are_audited_replayed_and_rolled_back(
    visibility_fixture: VisibilityFixture,
    current_round: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = visibility_fixture
    monkeypatch.setattr(mutations_module, "Session", _AdvisoryIgnoringSession)
    service = OperatorMutationService(fixture.engine, fixture.domain)
    principal = OperatorPrincipal(
        account_id=UUID(int=81_004),
        session_id=UUID(int=81_005),
        username="operator",
        role=AdminRole.OPERATOR,
        csrf_hash="test-hash",
    )
    request = FinalRoundMutationRequest(
        expected_version=1,
        idempotency_key=uuid4(),
        round_id=current_round,
        is_final=True,
    )
    first = service.set_final_round(principal, EVENT_ID, request)
    replay = service.set_final_round(principal, EVENT_ID, request)
    assert first.operation == "set_final_round" and first.event_version == 2
    assert replay.is_replay and replay.event_version == 2
    unchanged = service.set_final_round(
        principal,
        EVENT_ID,
        request.model_copy(update={"expected_version": 2, "idempotency_key": uuid4()}),
    )
    assert unchanged.event_version == 2 and not unchanged.is_replay
    with pytest.raises(IdempotencyKeyConflictError):
        service.set_final_round(principal, EVENT_ID, request.model_copy(update={"is_final": False}))
    with pytest.raises(VersionConflictError):
        service.set_final_round(
            principal, EVENT_ID, request.model_copy(update={"idempotency_key": uuid4()})
        )
    with Session(fixture.engine) as session:
        audits = session.scalars(select(OperationAudit)).all()
        assert len(audits) == 2
        assert all(audit.round_id is None for audit in audits)
        assert all(audit.event_id == EVENT_ID for audit in audits)
        assert all(audit.target_type == "final_round" for audit in audits)
        assert all(audit.target_id == str(EVENT_ID) for audit in audits)
        assert {audit.before_state for audit in audits} == {None, str(current_round)}
        assert all(audit.after_state == str(current_round) for audit in audits)
        assert all(audit.operation == "set_final_round" for audit in audits)
        assert all(audit.reason == "" for audit in audits)
        assert session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 2

    # 更新後の監査時計取得に失敗しても、指定とEvent版を同一transactionで戻す。
    def fail_clock(_session: Session) -> None:
        raise DomainInvariantError("test_clock_failure")

    monkeypatch.setattr(service, "_database_time", fail_clock)
    with pytest.raises(DomainInvariantError, match="test_clock_failure"):
        service.set_final_round(
            principal,
            EVENT_ID,
            request.model_copy(
                update={"expected_version": 2, "idempotency_key": uuid4(), "is_final": False}
            ),
        )
    assert (
        FinalRoundService(fixture.engine).get_final_round(EVENT_ID).final_round_id == current_round
    )
    assert FinalRoundService(fixture.engine).get_final_round(EVENT_ID).event_version == 2


def test_final_round_projection_rejects_corrupt_cross_event_reference(
    visibility_fixture: VisibilityFixture, current_round: UUID
) -> None:
    with Session(visibility_fixture.engine) as session, session.begin():
        event = session.get(Event, EVENT_ID)
        assert event is not None
        set_final_round_configuration(require_event_configuration(session, event), UUID(int=999))
    with pytest.raises(DomainInvariantError, match="final_round_event_mismatch"):
        FinalRoundService(visibility_fixture.engine).get_final_round(EVENT_ID)
