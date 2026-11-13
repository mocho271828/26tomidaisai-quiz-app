import json
from itertools import product
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlmodel import Session
from test_result_visibility import (
    DATABASE_NOW,
    EVENT_ID,
    VisibilityFixture,
    _AdvisoryIgnoringSession,
)
from test_result_visibility import visibility_fixture as visibility_fixture

import quiz_app.operator.mutations as mutations_module
from quiz_app.domain.errors import DomainInvariantError, VersionConflictError
from quiz_app.models import Event, EventQuizConfiguration, IdempotencyRecord, OperationAudit
from quiz_app.models.enums import AdminRole
from quiz_app.operator.auth_service import OperatorPrincipal
from quiz_app.operator.control_contracts import ResultVisibilityMutationRequest
from quiz_app.operator.errors import IdempotencyKeyConflictError
from quiz_app.operator.mutations import OperatorMutationService

PRINCIPAL = OperatorPrincipal(
    account_id=UUID(int=80_004),
    session_id=UUID(int=80_005),
    username="operator",
    role=AdminRole.OPERATOR,
    csrf_hash="test-hash",
)


@pytest.fixture
def mutation_service(
    visibility_fixture: VisibilityFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> OperatorMutationService:
    monkeypatch.setattr(mutations_module, "Session", _AdvisoryIgnoringSession)
    service = OperatorMutationService(visibility_fixture.engine, visibility_fixture.domain)
    monkeypatch.setattr(service, "_database_time", lambda _session: DATABASE_NOW)
    return service


@pytest.mark.parametrize(
    ("distribution", "personal", "overall", "visible"), list(product((False, True), repeat=4))
)
def test_grouped_ranking_changes_are_atomic_versioned_audited_and_idempotent(
    visibility_fixture: VisibilityFixture,
    mutation_service: OperatorMutationService,
    distribution: bool,
    personal: bool,
    overall: bool,
    visible: bool,
) -> None:
    with Session(visibility_fixture.engine) as session, session.begin():
        record = session.get(EventQuizConfiguration, EVENT_ID)
        assert record is not None
        record.distribution_visible = distribution
        record.personal_ranking_visible = personal
        record.overall_ranking_visible = overall
        original_settings = record.settings_json

    request = ResultVisibilityMutationRequest(
        expected_version=1,
        idempotency_key=uuid4(),
        target="rankings",
        visible=visible,
    )
    result = mutation_service.set_result_visibility(PRINCIPAL, EVENT_ID, request)
    replay = mutation_service.set_result_visibility(PRINCIPAL, EVENT_ID, request)
    expected_version = 1 + (personal != visible or overall != visible)
    assert result.event_version == expected_version
    assert result.result_visibility.distribution is distribution
    assert result.result_visibility.personal_ranking is visible
    assert result.result_visibility.overall_ranking is visible
    assert replay.is_replay is True
    assert replay.event_version == expected_version
    assert replay.result_visibility == result.result_visibility
    with pytest.raises(IdempotencyKeyConflictError):
        mutation_service.set_result_visibility(
            PRINCIPAL, EVENT_ID, request.model_copy(update={"visible": not visible})
        )

    with Session(visibility_fixture.engine) as session:
        record = session.get(EventQuizConfiguration, EVENT_ID)
        event = session.get(Event, EVENT_ID)
        assert record is not None and event is not None
        assert record.distribution_visible is distribution
        assert record.personal_ranking_visible is visible
        assert record.overall_ranking_visible is visible
        assert record.settings_json == original_settings
        assert record.last_applied_round == 0
        assert event.version == expected_version
        audits = session.scalars(select(OperationAudit)).all()
        assert len(audits) == 1
        audit = audits[0]
        assert audit.target_id == f"{EVENT_ID}:rankings"
        assert audit.operation == "set_result_visibility"
        assert audit.reason == ""
        assert audit.before_state is not None and len(audit.before_state) <= 80
        assert audit.after_state is not None and len(audit.after_state) <= 80
        assert json.loads(audit.before_state) == {
            "personal_ranking": personal,
            "overall_ranking": overall,
        }
        assert json.loads(audit.after_state) == {
            "personal_ranking": visible,
            "overall_ranking": visible,
        }
        assert session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 1


@pytest.mark.parametrize("failure", ["version_conflict", "audit_clock_failure"])
def test_grouped_ranking_failure_rolls_back_both_settings_and_version(
    visibility_fixture: VisibilityFixture,
    mutation_service: OperatorMutationService,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    if failure == "audit_clock_failure":

        def fail_audit_clock(_session: Session) -> None:
            raise DomainInvariantError("database_clock_unavailable")

        monkeypatch.setattr(mutation_service, "_database_time", fail_audit_clock)
    request = ResultVisibilityMutationRequest(
        expected_version=2 if failure == "version_conflict" else 1,
        idempotency_key=uuid4(),
        target="rankings",
        visible=False,
    )
    error_type = VersionConflictError if failure == "version_conflict" else DomainInvariantError
    with pytest.raises(error_type):
        mutation_service.set_result_visibility(PRINCIPAL, EVENT_ID, request)

    with Session(visibility_fixture.engine) as session:
        record = session.get(EventQuizConfiguration, EVENT_ID)
        event = session.get(Event, EVENT_ID)
        assert record is not None and event is not None
        assert record.distribution_visible is True
        assert record.personal_ranking_visible is True
        assert record.overall_ranking_visible is True
        assert event.version == 1
        assert session.scalar(select(func.count()).select_from(OperationAudit)) == 0
        assert session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 0
