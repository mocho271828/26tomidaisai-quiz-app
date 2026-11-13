import json
from typing import Literal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import DateTime, literal, select
from sqlmodel import Session
from test_result_visibility import (
    DATABASE_NOW,
    EVENT_ID,
    NOW,
    VisibilityFixture,
    _AdvisoryIgnoringSession,
    _round,
)
from test_result_visibility import visibility_fixture as visibility_fixture

import quiz_app.operator.mutations as mutations_module
from quiz_app.audience.award_ceremony import award_ceremony_projection
from quiz_app.audience.contracts import (
    AwardCeremonyRankingRowResponse,
    AwardCeremonyResponse,
    ParticipantCommonStateResponse,
)
from quiz_app.audience.projection import AudienceProjectionService
from quiz_app.domain.award_ceremony import set_award_ceremony_in_transaction
from quiz_app.domain.errors import (
    DomainInvariantError,
    InvalidStateTransitionError,
    VersionConflictError,
)
from quiz_app.models import (
    Event,
    OperationAudit,
    QuizRound,
    RankSnapshot,
    RankSnapshotRow,
)
from quiz_app.models.enums import AdminRole, EventState, RoundResultPublication
from quiz_app.operator.auth_service import OperatorPrincipal
from quiz_app.operator.control_contracts import AwardCeremonyMutationRequest
from quiz_app.operator.errors import IdempotencyKeyConflictError
from quiz_app.operator.mutations import OperatorMutationService
from quiz_app.quiz_configuration import QuizSettings, require_event_configuration
from quiz_app.quiz_configuration.persistence import (
    set_award_ceremony_configuration,
    set_final_round_configuration,
)


@pytest.fixture
def completed_round(visibility_fixture: VisibilityFixture, monkeypatch: pytest.MonkeyPatch) -> UUID:
    monkeypatch.setattr(
        "quiz_app.domain.award_ceremony.func.clock_timestamp",
        lambda: literal(DATABASE_NOW, type_=DateTime()),
    )
    with Session(visibility_fixture.engine) as session, session.begin():
        round_ = _round(session, 1, is_published=True)
        event = session.get(Event, EVENT_ID)
        assert event is not None
        event.state = EventState.COMPLETED
        configuration = require_event_configuration(session, event)
        set_final_round_configuration(configuration, round_.id)
        configuration.record.personal_ranking_visible = False
        configuration.record.overall_ranking_visible = False
        return round_.id


def change(session: Session, phase: Literal["Announced", "Finished"], version: int = 1) -> int:
    return set_award_ceremony_in_transaction(
        session, event_id=EVENT_ID, expected_version=version, phase=phase
    ).event_version


def test_persisted_progress_preserves_final_round_settings_and_visibility(
    visibility_fixture: VisibilityFixture, completed_round: UUID
) -> None:
    with Session(visibility_fixture.engine) as session, session.begin():
        event = session.get(Event, EVENT_ID)
        assert event is not None
        configuration = require_event_configuration(session, event)
        original = configuration.record.settings_json
        assert configuration.award_ceremony_phase == "Waiting"
        assert require_event_configuration(session, event).record.settings_json == original
        assert change(session, "Announced") == 2
        configuration = require_event_configuration(session, event)
        assert configuration.award_ceremony_phase == "Announced"
        assert configuration.final_round_id == completed_round
        assert configuration.settings == visibility_fixture.settings
        assert not configuration.record.personal_ranking_visible
        assert not configuration.record.overall_ranking_visible
        saved = json.loads(configuration.record.settings_json)
        assert saved.pop("award_ceremony_phase") == "Announced"
        assert saved == json.loads(original)
        assert change(session, "Announced", 2) == 2
        assert change(session, "Finished", 2) == 3
        assert change(session, "Finished", 3) == 3
        with pytest.raises(InvalidStateTransitionError, match="invalid_transition"):
            change(session, "Announced", 3)
        with pytest.raises(VersionConflictError):
            change(session, "Finished", 2)
    with Session(visibility_fixture.engine) as session:
        event = session.get(Event, EVENT_ID)
        assert event is not None
        assert require_event_configuration(session, event).award_ceremony_phase == "Finished"


@pytest.mark.parametrize(
    "state", [state for state in EventState if state is not EventState.COMPLETED]
)
def test_rejects_non_completed_events(
    visibility_fixture: VisibilityFixture, completed_round: UUID, state: EventState
) -> None:
    with Session(visibility_fixture.engine) as session, session.begin():
        event = session.get(Event, EVENT_ID)
        assert event is not None
        event.state = state
        with pytest.raises(InvalidStateTransitionError, match="requires_completed"):
            change(session, "Announced")
        assert event.version == 1
        assert award_ceremony_projection(session, event, "Waiting") is None


def test_rejects_skipping_announcement_and_unpublished_final_result(
    visibility_fixture: VisibilityFixture, completed_round: UUID
) -> None:
    with Session(visibility_fixture.engine) as session, session.begin():
        with pytest.raises(InvalidStateTransitionError, match="invalid_transition"):
            change(session, "Finished")
        round_ = session.get(QuizRound, completed_round)
        assert round_ is not None
        round_.result_publication = RoundResultPublication.WITHHELD
        with pytest.raises(DomainInvariantError, match="final_result_unavailable"):
            change(session, "Announced")


def test_projection_publishes_all_top_five_ties_only_during_announcement(
    visibility_fixture: VisibilityFixture, completed_round: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    audience = AudienceProjectionService(visibility_fixture.engine)
    monkeypatch.setattr(audience, "_database_time", lambda _session: NOW)
    with Session(visibility_fixture.engine) as session, session.begin():
        first = session.scalar(select(RankSnapshotRow))
        assert first is not None
        session.add_all(
            [
                RankSnapshotRow(
                    snapshot_id=first.snapshot_id,
                    public_label=label,
                    overall_rank=rank,
                    total_points=100,
                    correct_count=1,
                )
                for label, rank in (
                    ("DEF", 2),
                    ("GHI", 3),
                    ("JKL", 4),
                    ("MNO", 5),
                    ("PQR", 5),
                    ("STU", 7),
                )
            ]
        )
        event = session.get(Event, EVENT_ID)
        assert event is not None
        waiting = audience._build_common_snapshot(session, event).state
        assert waiting.award_ceremony == AwardCeremonyResponse(phase="Waiting", top_rows=None)
        assert waiting.latest_ranking_snapshot_version is None
        assert change(session, "Announced") == 2
        announced = audience._build_common_snapshot(session, event).state
        assert announced.award_ceremony is not None
        rows = announced.award_ceremony.top_rows
        assert rows is not None
        assert [row.public_label for row in rows] == ["ABC", "DEF", "GHI", "JKL", "MNO", "PQR"]
        assert [row.overall_rank for row in rows] == [1, 2, 3, 4, 5, 5]
        assert all(set(row.model_dump()) == {"public_label", "overall_rank"} for row in rows)
        assert announced.latest_ranking_snapshot_version is None
        assert not announced.result_visibility.personal_ranking
        assert not announced.result_visibility.overall_ranking
        # 共通stateだけで全員へ同じ順位表を届け、本人結果や回答の有無へ依存しない。
        serialized = announced.model_dump_json()
        assert "participant_id" not in serialized and "STU" not in serialized
        assert ParticipantCommonStateResponse.model_validate_json(serialized) == announced
        assert change(session, "Finished", 2) == 3
        finished = audience._build_common_snapshot(session, event).state
        assert finished.award_ceremony == AwardCeremonyResponse(phase="Finished", top_rows=None)


def test_empty_ranking_remains_empty_and_announcement_ignores_final_round_marker(
    visibility_fixture: VisibilityFixture, completed_round: UUID
) -> None:
    with Session(visibility_fixture.engine) as session, session.begin():
        event = session.get(Event, EVENT_ID)
        assert event is not None
        set_final_round_configuration(require_event_configuration(session, event), None)
        for row in session.scalars(select(RankSnapshotRow)):
            session.delete(row)
        assert change(session, "Announced") == 2
        assert award_ceremony_projection(session, event, "Announced") == AwardCeremonyResponse(
            phase="Announced", top_rows=[]
        )


def test_announcement_uses_latest_snapshot_version_for_the_completed_round(
    visibility_fixture: VisibilityFixture, completed_round: UUID
) -> None:
    with Session(visibility_fixture.engine) as session, session.begin():
        snapshot = RankSnapshot(event_id=EVENT_ID, round_id=completed_round, version=2)
        session.add_all(
            [
                snapshot,
                RankSnapshotRow(
                    snapshot_id=snapshot.id,
                    public_label="DEF",
                    overall_rank=1,
                    total_points=900,
                    correct_count=2,
                ),
            ]
        )
        assert change(session, "Announced") == 2
        event = session.get(Event, EVENT_ID)
        assert event is not None
        ceremony = award_ceremony_projection(session, event, "Announced")
        assert ceremony is not None and ceremony.top_rows is not None
        assert [(row.public_label, row.overall_rank) for row in ceremony.top_rows] == [("DEF", 1)]


def test_announcement_rejects_latest_snapshot_of_another_round(
    visibility_fixture: VisibilityFixture, completed_round: UUID
) -> None:
    with Session(visibility_fixture.engine) as session, session.begin():
        previous_round = _round(session, 2, is_published=True)
        previous_round.version = 1
        with pytest.raises(DomainInvariantError, match="ranking_unavailable"):
            change(session, "Announced")
        event = session.get(Event, EVENT_ID)
        assert event is not None and event.version == 1
        assert require_event_configuration(session, event).award_ceremony_phase == "Waiting"


def test_storage_rejects_invalid_phase_and_static_settings_stay_strict(
    visibility_fixture: VisibilityFixture, completed_round: UUID
) -> None:
    with Session(visibility_fixture.engine) as session:
        event = session.get(Event, EVENT_ID)
        assert event is not None
        configuration = require_event_configuration(session, event)
        set_award_ceremony_configuration(configuration, "Announced")
        saved = configuration.record.settings_json
        with pytest.raises(ValidationError):
            QuizSettings.model_validate_json(saved)
        configuration.record.settings_json = json.dumps(
            json.loads(saved) | {"award_ceremony_phase": "bad"}
        )
        with pytest.raises(DomainInvariantError, match="configuration_invalid"):
            require_event_configuration(session, event)


def test_mutation_replay_conflict_audit_and_rollback(
    visibility_fixture: VisibilityFixture, completed_round: UUID, monkeypatch: pytest.MonkeyPatch
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
    request = AwardCeremonyMutationRequest(
        expected_version=1, idempotency_key=uuid4(), phase="Announced"
    )
    first = service.set_award_ceremony(principal, EVENT_ID, request)
    replay = service.set_award_ceremony(principal, EVENT_ID, request)
    assert first.operation == "set_award_ceremony" and first.event_version == 2
    assert replay.is_replay and replay.event_version == 2
    with pytest.raises(IdempotencyKeyConflictError):
        service.set_award_ceremony(
            principal, EVENT_ID, request.model_copy(update={"phase": "Finished"})
        )
    with Session(fixture.engine) as session:
        audit = session.scalar(select(OperationAudit))
        assert audit is not None
        assert audit.operation == "set_award_ceremony" and audit.round_id is None
        assert audit.before_state == "Waiting" and audit.after_state == "Announced"
        assert audit.target_type == "award_ceremony" and audit.target_id == str(EVENT_ID)
        assert audit.reason == ""

    def fail_clock(_session: Session) -> None:
        raise DomainInvariantError("test_clock_failure")

    monkeypatch.setattr(service, "_database_time", fail_clock)
    with pytest.raises(DomainInvariantError, match="test_clock_failure"):
        service.set_award_ceremony(
            principal,
            EVENT_ID,
            request.model_copy(
                update={"expected_version": 2, "idempotency_key": uuid4(), "phase": "Finished"}
            ),
        )
    with Session(fixture.engine) as session:
        event = session.get(Event, EVENT_ID)
        assert event is not None and event.version == 2
        assert require_event_configuration(session, event).award_ceremony_phase == "Announced"


@pytest.mark.parametrize("phase,rows", [("Waiting", []), ("Finished", []), ("Announced", None)])
def test_contract_rejects_phase_and_rows_mismatch(phase: str, rows: list[object] | None) -> None:
    with pytest.raises(ValidationError, match="rows_publication_mismatch"):
        AwardCeremonyResponse.model_validate({"phase": phase, "top_rows": rows})


@pytest.mark.parametrize("field", ["total_points", "correct_count"])
def test_award_ranking_contract_rejects_unneeded_score_fields(field: str) -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AwardCeremonyRankingRowResponse.model_validate(
            {"public_label": "ABC", "overall_rank": 1, field: 100}
        )
