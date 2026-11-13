"""既存の正式負荷試験を、承認済みの待機時間と公開条件へ限定する。"""

from quiz_app.load_test.legacy_control_contracts import (
    OperatorStateResponse,
)
from quiz_app.quiz_configuration import QuizSettings, ResultVisibility, VisibilityControls

FORMAL_START_DELAY_SECONDS = 5
FORMAL_RESULT_VISIBILITY = ResultVisibility(
    distribution=True,
    personal_ranking=False,
    overall_ranking=False,
)


def matches_formal_runtime_configuration(state: OperatorStateResponse) -> bool:
    """新しい開催設定を過去の正式試験契約へ暗黙に読み替えない。"""
    return (
        state.start_delay_seconds == FORMAL_START_DELAY_SECONDS
        and state.result_visibility == FORMAL_RESULT_VISIBILITY
        and _matches_formal_controls(state.visibility_controls)
    )


def matches_formal_quiz_settings(settings: QuizSettings) -> bool:
    """試験開始前に設定JSONの進行・公開条件を照合する。"""
    return settings.start_delay_seconds == FORMAL_START_DELAY_SECONDS and _matches_formal_controls(
        settings.visibility
    )


def _matches_formal_controls(controls: VisibilityControls) -> bool:
    return all(
        control.mode == "manual" and control.initially_visible is visible and not control.changes
        for control, visible in (
            (controls.distribution, True),
            (controls.personal_ranking, False),
            (controls.overall_ranking, False),
        )
    )
