from dataclasses import dataclass
from uuid import UUID

from pydantic import ValidationError
from sqlmodel import Session

from quiz_app.domain.errors import DomainInvariantError
from quiz_app.domain.rounds import STANDARD_DURATION_MS
from quiz_app.models import Event, EventQuizConfiguration, ScoringPolicy
from quiz_app.quiz_configuration.contracts import (
    VISIBILITY_TARGETS,
    AwardCeremonyPhase,
    QuizSettings,
    ResultVisibility,
    VisibilityMutationTarget,
)
from quiz_app.quiz_configuration.loading import get_quiz_settings


@dataclass(frozen=True, slots=True)
class EventConfiguration:
    settings: QuizSettings
    record: EventQuizConfiguration
    final_round_id: UUID | None = None
    award_ceremony_phase: AwardCeremonyPhase = "Waiting"


class _StoredQuizSettings(QuizSettings):
    """静的設定の厳密照合を保ち、運営の進行設定を保存領域へ分離する。"""

    final_round_id: UUID | None = None
    award_ceremony_phase: AwardCeremonyPhase = "Waiting"


def configured_scoring_policy(settings: QuizSettings) -> ScoringPolicy:
    """人手初期化用の新規policyを構築する。保存や既存versionの更新はしない。"""
    return ScoringPolicy(
        version=settings.scoring.version,
        base_points=settings.scoring.correct_points,
        initial_speed_bonus=settings.scoring.maximum_speed_bonus,
        speed_bucket_ms=settings.scoring.speed_bucket_ms,
        speed_decrement_per_bucket=1,
        standard_duration_ms=STANDARD_DURATION_MS,
        timing_basis=settings.timing.basis,
        arrival_grace_ms=settings.timing.arrival_grace_ms,
        clock_tolerance_ms=settings.timing.clock_tolerance_ms,
        client_ahead_tolerance_ms=settings.timing.client_ahead_tolerance_ms,
        is_approved=True,
    )


def initial_event_configuration(event_id: UUID, settings: QuizSettings) -> EventQuizConfiguration:
    """未開始の新規開催へ保存する設定と初期表示を構築する。"""
    return EventQuizConfiguration(
        event_id=event_id,
        settings_json=settings.model_dump_json(),
        distribution_visible=settings.visibility.distribution.initially_visible,
        personal_ranking_visible=settings.visibility.personal_ranking.initially_visible,
        overall_ranking_visible=settings.visibility.overall_ranking.initially_visible,
        last_applied_round=0,
    )


def require_matching_scoring_policy(policy: ScoringPolicy | None, settings: QuizSettings) -> None:
    """versionと全係数を照合し、同versionの上書きや別設定での進行を拒否する。"""
    expected = configured_scoring_policy(settings)
    if policy is None or any(
        getattr(policy, field) != getattr(expected, field)
        for field in (
            "version",
            "base_points",
            "initial_speed_bonus",
            "speed_bucket_ms",
            "speed_decrement_per_bucket",
            "standard_duration_ms",
            "timing_basis",
            "arrival_grace_ms",
            "clock_tolerance_ms",
            "client_ahead_tolerance_ms",
            "is_approved",
        )
    ):
        raise DomainInvariantError("quiz_configuration_scoring_mismatch")


def require_event_configuration(session: Session, event: Event) -> EventConfiguration:
    """DB保存済み設定を起動設定と照合する。読取りで初期化・補正は行わない。"""
    record = session.get(EventQuizConfiguration, event.id)
    if record is None:
        raise DomainInvariantError("event_quiz_configuration_missing")
    try:
        stored = _StoredQuizSettings.model_validate_json(record.settings_json)
        stored_settings = QuizSettings.model_validate(
            stored.model_dump(exclude={"final_round_id", "award_ceremony_phase"})
        )
    except ValidationError as error:
        raise DomainInvariantError("event_quiz_configuration_invalid") from error
    settings = get_quiz_settings()
    if stored_settings != settings or event.scoring_policy_version != settings.scoring.version:
        raise DomainInvariantError("event_quiz_configuration_mismatch")
    require_matching_scoring_policy(
        session.get(ScoringPolicy, event.scoring_policy_version), settings
    )
    return EventConfiguration(
        settings=settings,
        record=record,
        final_round_id=stored.final_round_id,
        award_ceremony_phase=stored.award_ceremony_phase,
    )


def set_final_round_configuration(
    configuration: EventConfiguration, final_round_id: UUID | None
) -> None:
    """Event lock下の明示操作だけで最終問を保存し、未指定時は元のJSON形を保つ。"""

    _save_progress_configuration(configuration, final_round_id, configuration.award_ceremony_phase)


def set_award_ceremony_configuration(
    configuration: EventConfiguration, phase: AwardCeremonyPhase
) -> None:
    """Event lock下で表彰の進行だけを変更し、最終問題の指定を保持する。"""

    _save_progress_configuration(configuration, configuration.final_round_id, phase)


def _save_progress_configuration(
    configuration: EventConfiguration,
    final_round_id: UUID | None,
    phase: AwardCeremonyPhase,
) -> None:
    stored = _StoredQuizSettings(
        **configuration.settings.model_dump(),
        final_round_id=final_round_id,
        award_ceremony_phase=phase,
    )
    excluded = set()
    if final_round_id is None:
        excluded.add("final_round_id")
    if phase == "Waiting":
        excluded.add("award_ceremony_phase")
    configuration.record.settings_json = stored.model_dump_json(exclude=excluded)


def visibility_state(record: EventQuizConfiguration) -> ResultVisibility:
    return ResultVisibility(
        distribution=record.distribution_visible,
        personal_ranking=record.personal_ranking_visible,
        overall_ranking=record.overall_ranking_visible,
    )


def set_manual_visibility(
    record: EventQuizConfiguration, target: VisibilityMutationTarget, visible: bool
) -> None:
    """callerのEvent lock・監査transaction内で指定項目をまとめて更新する。"""
    if target == "distribution":
        record.distribution_visible = visible
    elif target == "rankings":
        record.personal_ranking_visible = visible
        record.overall_ranking_visible = visible
    elif target == "personal_ranking":
        record.personal_ranking_visible = visible
    else:
        record.overall_ranking_visible = visible


def apply_automatic_visibility(
    record: EventQuizConfiguration, settings: QuizSettings, completed_round: int
) -> None:
    """未適用の終了問だけを処理し、手動再表示を毎問の再評価で取り消さない。"""
    if completed_round <= record.last_applied_round:
        return
    for target in VISIBILITY_TARGETS:
        control = getattr(settings.visibility, target)
        for change in control.changes:
            if record.last_applied_round < change.after_round <= completed_round:
                set_manual_visibility(record, target, change.visible)
    record.last_applied_round = completed_round
