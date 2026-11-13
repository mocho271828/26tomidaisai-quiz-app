import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from quiz_app.rehearsal.content import (
    MAXIMUM_CONTENT_BYTES,
    PENDING_ANSWER_CONTENT,
    RehearsalContent,
    RehearsalContentError,
    load_rehearsal_content,
)

CONTENT_PATH = Path(__file__).resolve().parents[1] / "content" / "event-questions.json"


def _content() -> RehearsalContent:
    return load_rehearsal_content(CONTENT_PATH)


def _validate_changes(**changes: object) -> RehearsalContent:
    payload = _content().model_dump(mode="json")
    payload.update(changes)
    return RehearsalContent.model_validate_json(json.dumps(payload, ensure_ascii=False))


def test_content_registers_one_practice_five_official_and_two_reserve_questions() -> None:
    content = _content()

    assert len(content.questions) == 8
    assert content.selected_question_ids == tuple(range(1, 9))
    assert tuple(question.id for question in content.selected_questions) == tuple(range(1, 9))
    for question in content.selected_questions:
        assert len(question.options) == 4
        assert sum(option.label == question.correct_label for option in question.options) == 1
        assert question.display_prompt == f"【{question.respondent}】{question.prompt}"
        assert question.explanation == next(
            option.content for option in question.options if option.label == question.correct_label
        )


@pytest.mark.parametrize(
    "selected_ids",
    [
        [],
        [1],
        [1, 2],
        [1, 2, 3, 4, 5, 6, 6],
        [1, 2, 3, 4, 5, 5],
        [1, 2, 3, 4, 5, 11],
        [1, 2, 3, 4, 5, 0],
        [1, 2, 3, 4, 5, "6"],
        [True, 2, 3, 4, 5, 6],
        [1, 2, 3, 4, 5, 6.0],
    ],
)
def test_selection_requires_at_least_three_distinct_existing_positive_integer_ids(
    selected_ids: list[object],
) -> None:
    with pytest.raises(ValidationError):
        _validate_changes(selected_question_ids=selected_ids)


def test_selection_preserves_explicit_order() -> None:
    content = _validate_changes(selected_question_ids=[6, 5, 4, 3, 2, 1])

    assert tuple(question.id for question in content.selected_questions) == (6, 5, 4, 3, 2, 1)


def test_selection_accepts_additional_questions_and_nonconsecutive_ids() -> None:
    questions = [question.model_dump(mode="json") for question in _content().questions]
    questions.extend(questions[0] | {"id": question_id} for question_id in (11, 20, 100))
    selected_ids = [100, 2, 20, 4, 5, 11, 6, 1]

    content = _validate_changes(questions=questions, selected_question_ids=selected_ids)

    assert [question.id for question in content.selected_questions] == selected_ids


def test_selection_accepts_minimum_three_questions() -> None:
    content = _validate_changes(selected_question_ids=[6, 2, 4])

    assert tuple(question.id for question in content.selected_questions) == (6, 2, 4)


def test_question_accepts_custom_respondent_and_omitted_note() -> None:
    questions = [question.model_dump(mode="json") for question in _content().questions]
    questions[0]["respondent"] = " デモ担当者 "
    questions[0].pop("answer_note")

    content = _validate_changes(questions=questions)

    assert content.questions[0].respondent == "デモ担当者"
    assert content.questions[0].answer_note is None


@pytest.mark.parametrize("field", ["respondent", "answer_note"])
@pytest.mark.parametrize("value", [" ", "あ" * 501, 1])
def test_question_rejects_invalid_respondent_and_present_note(field: str, value: object) -> None:
    questions = [question.model_dump(mode="json") for question in _content().questions]
    questions[0][field] = value

    with pytest.raises(ValidationError):
        _validate_changes(questions=questions)


def test_pending_answer_cannot_be_selected() -> None:
    questions = [question.model_dump(mode="json") for question in _content().questions]
    questions[1]["correct_label"] = None
    with pytest.raises(ValidationError):
        _validate_changes(questions=questions)


def test_answer_can_be_finalized_and_selected_by_editing_json_only() -> None:
    questions = [question.model_dump(mode="json") for question in _content().questions]
    questions[1]["options"][0]["content"] = "富山でのラジオ出演"
    questions[1]["correct_label"] = "A"
    questions[1]["answer_note"] = "当日確認済み。"

    content = _validate_changes(questions=questions)

    assert content.selected_questions[1].explanation == "富山でのラジオ出演"


def test_setting_a_correct_label_does_not_accept_the_pending_placeholder() -> None:
    questions = [question.model_dump(mode="json") for question in _content().questions]
    questions[1]["correct_label"] = "A"
    questions[1]["options"][0]["content"] = PENDING_ANSWER_CONTENT

    with pytest.raises(ValidationError, match="rehearsal_finalized_question_has_pending_option"):
        _validate_changes(questions=questions)


@pytest.mark.parametrize("question_ids", [[1, 2, 3, 4, 5], [1, 2, 3, 4, 5, 5], [*range(1, 9), 8]])
def test_question_bank_requires_unique_ids_and_all_selected_questions(
    question_ids: list[int],
) -> None:
    questions = [question.model_dump(mode="json") for question in _content().questions]
    selected = [questions[question_id - 1] for question_id in question_ids]

    with pytest.raises(ValidationError):
        _validate_changes(questions=selected)


@pytest.mark.parametrize("question_id", [0, -1, True, "11", 11.0])
def test_question_bank_requires_positive_integer_ids(question_id: object) -> None:
    questions = [question.model_dump(mode="json") for question in _content().questions]
    questions.append(questions[0] | {"id": question_id})

    with pytest.raises(ValidationError):
        _validate_changes(questions=questions)


@pytest.mark.parametrize(
    ("field", "value"),
    [("label", "B"), ("content", "duplicate"), ("content", " ")],
)
def test_options_reject_duplicate_labels_duplicate_content_and_blank_text(
    field: str, value: str
) -> None:
    questions = [question.model_dump(mode="json") for question in _content().questions]
    if value == "duplicate":
        value = f" {questions[0]['options'][1]['content']} "
    questions[0]["options"][0][field] = value

    with pytest.raises(ValidationError):
        _validate_changes(questions=questions)


@pytest.mark.parametrize("option_count", [0, 3, 5])
def test_each_question_requires_four_options(option_count: int) -> None:
    questions = [question.model_dump(mode="json") for question in _content().questions]
    options = questions[0]["options"]
    questions[0]["options"] = (options + options[:1])[:option_count]

    with pytest.raises(ValidationError):
        _validate_changes(questions=questions)


@pytest.mark.parametrize("correct_label", [None, "E", ["A", "B"], 1])
def test_confirmed_question_requires_one_valid_correct_label(correct_label: object) -> None:
    questions = [question.model_dump(mode="json") for question in _content().questions]
    questions[0]["correct_label"] = correct_label

    with pytest.raises(ValidationError):
        _validate_changes(questions=questions)


@pytest.mark.parametrize("schema_version", [0, 2, "1", True, 1.0])
def test_schema_version_is_strict(schema_version: object) -> None:
    with pytest.raises(ValidationError):
        _validate_changes(schema_version=schema_version)


def test_content_rejects_unknown_fields_and_is_deeply_immutable() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        _validate_changes(unknown_field="ignored otherwise")
    content = _content()
    with pytest.raises(ValidationError, match="frozen_instance"):
        content.title = "replacement"
    with pytest.raises(ValidationError, match="frozen_instance"):
        content.questions[0].options[0].content = "replacement"


def test_load_rejects_malformed_json_without_exposing_content_in_error(tmp_path: Path) -> None:
    content_path = tmp_path / "invalid.json"
    content_path.write_text('{"private-value":', encoding="utf-8")

    with pytest.raises(ValidationError) as caught:
        load_rehearsal_content(content_path)

    assert "private-value" not in str(caught.value)


@pytest.mark.parametrize("extra_bytes", [0, 1])
def test_content_byte_limit_accepts_boundary_and_rejects_one_extra_byte(
    tmp_path: Path, extra_bytes: int
) -> None:
    content_path = tmp_path / "questions.json"
    content = CONTENT_PATH.read_bytes()
    content_path.write_bytes(content + b" " * (MAXIMUM_CONTENT_BYTES - len(content) + extra_bytes))

    if extra_bytes == 0:
        assert load_rehearsal_content(content_path).selected_question_ids == tuple(range(1, 9))
    else:
        with pytest.raises(RehearsalContentError, match="rehearsal_content_file_too_large"):
            load_rehearsal_content(content_path)


@pytest.mark.parametrize("file_kind", ["directory", "device", "fifo"])
def test_content_requires_regular_file_without_reading_devices_or_waiting_on_fifo(
    tmp_path: Path, file_kind: str
) -> None:
    content_path = tmp_path
    if file_kind == "device":
        content_path = Path("/dev/null")
    elif file_kind == "fifo":
        content_path = tmp_path / "input.fifo"
        os.mkfifo(content_path)

    with pytest.raises(RehearsalContentError, match="rehearsal_content_file_not_regular"):
        load_rehearsal_content(content_path)


def test_content_rejects_symlink_and_missing_file_without_exposing_path(tmp_path: Path) -> None:
    content_path = tmp_path / "private-path.json"
    content_path.symlink_to(CONTENT_PATH)
    for path in (content_path, tmp_path / "private-missing.json"):
        with pytest.raises(RehearsalContentError) as caught:
            load_rehearsal_content(path)
        assert str(caught.value) == "rehearsal_content_file_unavailable"


def test_content_rejects_invalid_utf8(tmp_path: Path) -> None:
    content_path = tmp_path / "questions.json"
    content_path.write_bytes(b'{"schema_version":1,"title":"\xff"}')

    with pytest.raises(ValidationError):
        load_rehearsal_content(content_path)
