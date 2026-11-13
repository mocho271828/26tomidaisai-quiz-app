from datetime import UTC, datetime

import pytest
from test_load_test_controller import _manifest, _state

from quiz_app.load_test.runtime_configuration import matches_formal_runtime_configuration
from quiz_app.quiz_configuration import VisibilityChange, VisibilityControl


def test_formal_configuration_preserves_the_approved_five_second_schedule() -> None:
    state = _state(_manifest(), datetime(2026, 9, 23, tzinfo=UTC))
    assert matches_formal_runtime_configuration(state)
    changed = state.model_copy(update={"start_delay_seconds": 3})
    assert not matches_formal_runtime_configuration(changed)


@pytest.mark.parametrize("target", ["distribution", "personal_ranking", "overall_ranking"])
def test_formal_configuration_refuses_changed_or_automatic_visibility(target: str) -> None:
    state = _state(_manifest(), datetime(2026, 9, 23, tzinfo=UTC))
    visibility = state.result_visibility.model_copy(
        update={target: not getattr(state.result_visibility, target)}
    )
    assert not matches_formal_runtime_configuration(
        state.model_copy(update={"result_visibility": visibility})
    )
    automatic = VisibilityControl(
        mode="automatic",
        initially_visible=getattr(state.result_visibility, target),
        changes=(VisibilityChange(after_round=1, visible=True),),
    )
    assert not matches_formal_runtime_configuration(
        state.model_copy(
            update={
                "visibility_controls": state.visibility_controls.model_copy(
                    update={target: automatic}
                )
            }
        )
    )
