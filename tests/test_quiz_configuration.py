import json
from pathlib import Path
from typing import cast
from unittest.mock import Mock
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlmodel import Session

from quiz_app.domain.errors import DomainInvariantError
from quiz_app.domain.scoring import score_answer
from quiz_app.domain.types import AnswerScoreInput, ScoringRule
from quiz_app.models import Event, EventQuizConfiguration, ScoringPolicy
from quiz_app.quiz_configuration import (
    QuizSettings,
    apply_automatic_visibility,
    configured_scoring_policy,
    get_quiz_settings,
    initial_event_configuration,
    load_quiz_settings,
    require_event_configuration,
    require_matching_scoring_policy,
    set_manual_visibility,
    visibility_state,
)

EVENT_ID = UUID(int=22)


@pytest.mark.parametrize(
    "field,value",
    [
        ("start_delay_seconds", 0),
        ("start_delay_seconds", 301),
        ("start_delay_seconds", True),
        ("start_delay_seconds", "3"),
        ("start_delay_seconds", 3.5),
        ("schema_version", True),
        ("schema_version", 1),
        ("schema_version", 2),
        ("unexpected", 1),
    ],
)
def test_configuration_rejects_invalid_top_level(field: str, value: object) -> None:
    payload = get_quiz_settings().model_dump(mode="json")
    payload[field] = value
    with pytest.raises(ValidationError):
        QuizSettings.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "field,value",
    [
        ("correct_points", -1),
        ("correct_points", True),
        ("maximum_speed_bonus", -1),
        ("maximum_speed_bonus", 1_000_001),
        ("seconds_per_point", 0),
        ("seconds_per_point", -0.1),
        ("seconds_per_point", 0.0001),
        ("seconds_per_point", True),
        ("seconds_per_point", "0.1"),
        ("seconds_per_point", float("nan")),
        ("seconds_per_point", float("inf")),
        ("version", ""),
        ("extra", 1),
    ],
)
def test_configuration_rejects_invalid_scoring(field: str, value: object) -> None:
    scoring = get_quiz_settings().scoring.model_dump(mode="json")
    scoring[field] = value
    payload = get_quiz_settings().model_dump(mode="json") | {"scoring": scoring}
    with pytest.raises(ValidationError):
        QuizSettings.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "field", ["version", "visibility", "scoring", "start_delay_seconds", "timing"]
)
def test_configuration_requires_every_field(field: str) -> None:
    payload = get_quiz_settings().model_dump(mode="json")
    del payload[field]
    with pytest.raises(ValidationError):
        QuizSettings.model_validate_json(json.dumps(payload))


def _automatic_settings(changes: list[dict[str, object]], mode: str = "automatic") -> QuizSettings:
    visibility = get_quiz_settings().visibility.model_dump(mode="json")
    visibility["overall_ranking"] = {
        "mode": mode,
        "initially_visible": True,
        "changes": changes,
    }
    return QuizSettings.model_validate_json(
        json.dumps(get_quiz_settings().model_dump(mode="json") | {"visibility": visibility})
    )


@pytest.mark.parametrize(
    "changes,mode",
    [
        ([], "automatic"),
        ([{"after_round": 2, "visible": False}], "manual"),
        ([{"after_round": 0, "visible": False}], "automatic"),
        ([{"after_round": True, "visible": False}], "automatic"),
        ([{"after_round": 2, "visible": "false"}], "automatic"),
        ([{"after_round": 2, "visible": False, "extra": 1}], "automatic"),
        ([{"after_round": 2, "visible": False}, {"after_round": 2, "visible": True}], "automatic"),
        ([{"after_round": 2, "visible": False}, {"after_round": 1, "visible": True}], "automatic"),
    ],
)
def test_configuration_rejects_ambiguous_visibility(
    changes: list[dict[str, object]], mode: str
) -> None:
    with pytest.raises(ValidationError):
        _automatic_settings(changes, mode)


def test_automatic_hide_reshow_and_manual_override_survive_subsequent_rounds() -> None:
    settings = _automatic_settings(
        [
            {"after_round": 2, "visible": False},
            {"after_round": 5, "visible": True},
            {"after_round": 7, "visible": False},
        ]
    )
    record = initial_event_configuration(EVENT_ID, settings)
    apply_automatic_visibility(record, settings, 1)
    assert record.overall_ranking_visible
    apply_automatic_visibility(record, settings, 2)
    assert not record.overall_ranking_visible
    set_manual_visibility(record, "overall_ranking", True)
    apply_automatic_visibility(record, settings, 2)
    apply_automatic_visibility(record, settings, 3)
    assert record.overall_ranking_visible
    set_manual_visibility(record, "overall_ranking", False)
    apply_automatic_visibility(record, settings, 4)
    assert not record.overall_ranking_visible
    apply_automatic_visibility(record, settings, 5)
    assert record.overall_ranking_visible
    apply_automatic_visibility(record, settings, 7)
    assert not record.overall_ranking_visible
    assert record.distribution_visible and not record.personal_ranking_visible
    assert record.last_applied_round == 7


def test_manual_controls_are_independent() -> None:
    settings = get_quiz_settings()
    record = initial_event_configuration(EVENT_ID, settings)
    set_manual_visibility(record, "distribution", False)
    set_manual_visibility(record, "personal_ranking", True)
    apply_automatic_visibility(record, settings, 100)
    assert visibility_state(record).model_dump() == {
        "distribution": False,
        "personal_ranking": True,
        "overall_ranking": False,
    }


def test_configuration_load_failure_is_fixed_and_runtime_file_edits_do_not_change_snapshot(
    tmp_path: Path,
) -> None:
    path = tmp_path / "quiz-settings.json"
    with pytest.raises(DomainInvariantError, match="quiz_configuration_invalid"):
        load_quiz_settings(path)
    path.write_text(get_quiz_settings().model_dump_json())
    snapshot = load_quiz_settings(path)
    path.write_text('{"start_delay_seconds": 99}')
    with pytest.raises(DomainInvariantError, match="quiz_configuration_invalid"):
        load_quiz_settings(path)
    assert snapshot.start_delay_seconds == 3


@pytest.mark.parametrize("previous_version", ["quiz-2026-09-23-v1", "quiz-2026-10-15-v2"])
def test_saved_configuration_and_scoring_are_read_only_and_must_match(
    previous_version: str,
) -> None:
    settings = get_quiz_settings()
    record = initial_event_configuration(EVENT_ID, settings)
    policy = configured_scoring_policy(settings)
    event = Event(
        id=EVENT_ID,
        title="設定確認",
        slug="config-check",
        scoring_policy_version=policy.version,
        display_name_policy_version="display-test",
    )
    session = Mock(spec=Session)
    session.get.side_effect = lambda model, _: (
        record if model is EventQuizConfiguration else policy if model is ScoringPolicy else None
    )
    assert require_event_configuration(cast(Session, session), event).record is record
    session.add.assert_not_called()
    session.commit.assert_not_called()
    record.settings_json = settings.model_copy(update={"start_delay_seconds": 4}).model_dump_json()
    with pytest.raises(DomainInvariantError, match="event_quiz_configuration_mismatch"):
        require_event_configuration(cast(Session, session), event)
    previous_settings = settings.model_copy(update={"version": previous_version})
    assert previous_settings.scoring == settings.scoring
    record = initial_event_configuration(EVENT_ID, previous_settings)
    with pytest.raises(DomainInvariantError, match="event_quiz_configuration_mismatch"):
        require_event_configuration(cast(Session, session), event)
    assert record.settings_json == previous_settings.model_dump_json()
    session.add.assert_not_called()
    session.commit.assert_not_called()
    record.settings_json = settings.model_dump_json()
    event.scoring_policy_version = "old-version"
    with pytest.raises(DomainInvariantError, match="event_quiz_configuration_mismatch"):
        require_event_configuration(cast(Session, session), event)
    session.get.side_effect = None
    session.get.return_value = None
    with pytest.raises(DomainInvariantError, match="event_quiz_configuration_missing"):
        require_event_configuration(cast(Session, session), event)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "old"),
        ("base_points", 399),
        ("initial_speed_bonus", 99),
        ("speed_bucket_ms", 101),
        ("speed_decrement_per_bucket", 2),
        ("standard_duration_ms", 9999),
        ("timing_basis", "server_received_v1"),
        ("arrival_grace_ms", 1999),
        ("clock_tolerance_ms", 499),
        ("client_ahead_tolerance_ms", 2499),
        ("is_approved", False),
    ],
)
def test_scoring_policy_rejects_each_mismatched_field(field: str, value: object) -> None:
    settings = get_quiz_settings()
    policy = configured_scoring_policy(settings)
    setattr(policy, field, value)
    with pytest.raises(DomainInvariantError, match="quiz_configuration_scoring_mismatch"):
        require_matching_scoring_policy(policy, settings)
    with pytest.raises(DomainInvariantError, match="quiz_configuration_scoring_mismatch"):
        require_matching_scoring_policy(None, settings)


@pytest.mark.parametrize(
    "elapsed_ms,total", [(0, 500), (99, 500), (100, 499), (9899, 402), (9900, 401), (9999, 401)]
)
def test_approved_configuration_scoring_boundaries(elapsed_ms: int, total: int) -> None:
    policy = configured_scoring_policy(get_quiz_settings())
    rule = ScoringRule(**policy.model_dump(exclude={"created_at", "is_approved"}))
    answer = AnswerScoreInput(UUID(int=1), "AAA", True, elapsed_ms)
    assert score_answer(rule, answer).total_points == total


def test_approved_configuration_rejects_deadline_and_keeps_wrong_answer_zero() -> None:
    policy = configured_scoring_policy(get_quiz_settings())
    rule = ScoringRule(**policy.model_dump(exclude={"created_at", "is_approved"}))
    with pytest.raises(DomainInvariantError, match="outside_scoring_window"):
        score_answer(rule, AnswerScoreInput(UUID(int=1), "AAA", True, 10000))
    assert score_answer(rule, AnswerScoreInput(UUID(int=1), "AAA", False, 0)).total_points == 0


@pytest.mark.parametrize("value", [-1, True, "2500", 2500.5, 10_001])
def test_client_ahead_tolerance_requires_bounded_integer(value: object) -> None:
    payload = get_quiz_settings().model_dump(mode="json")
    payload["timing"]["client_ahead_tolerance_ms"] = value
    with pytest.raises(ValidationError):
        QuizSettings.model_validate_json(json.dumps(payload))


def test_configuration_requires_independent_client_ahead_tolerance() -> None:
    settings = get_quiz_settings()
    assert settings.schema_version == 3
    assert settings.version == "quiz-2026-10-23-v1"
    assert settings.scoring.version == "scoring-2026-10-15-v2"
    assert settings.timing.client_ahead_tolerance_ms == 2500
    assert settings.timing.clock_tolerance_ms == 10000
    assert settings.timing.arrival_grace_ms == 4000
    payload = settings.model_dump(mode="json")
    del payload["timing"]["client_ahead_tolerance_ms"]
    with pytest.raises(ValidationError):
        QuizSettings.model_validate_json(json.dumps(payload))
