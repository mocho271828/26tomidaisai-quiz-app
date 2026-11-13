import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine

from quiz_app.bootstrap.demo_content import DEMO_QUESTION_SEEDS, build_demo_question_seeds
from quiz_app.bootstrap.demo_content_loading import (
    DEFAULT_DEMO_CONTENT_PATH,
    DemoContent,
    DemoQuestionContent,
    load_demo_content,
    main,
)


def _question_payload() -> dict[str, object]:
    return {
        "prompt": "[DEMO] 架空のクイズです。",
        "explanation": "[DEMO] 正解はあおです。",
        "options": ["あか", "あお", "みどり", "きいろ"],
        "correct_label": "B",
    }


def _content_payload() -> dict[str, object]:
    return {"schema_version": 1, "questions": [_question_payload() for _ in range(6)]}


def test_bundled_json_is_the_source_of_seed_text_options_and_correct_answers() -> None:
    content = load_demo_content()

    assert len(content.questions) == 10
    for question, seed in zip(content.questions, DEMO_QUESTION_SEEDS, strict=True):
        assert (question.prompt, question.explanation) == (seed.prompt, seed.explanation)
        assert question.options == tuple(option.content for option in seed.options)
        assert question.correct_label == next(
            option.label for option in seed.options if option.is_correct
        )


def test_json_edit_changes_text_options_and_correct_answer_without_python_changes(
    tmp_path: Path,
) -> None:
    question = _question_payload() | {"options": ["春", "夏", "秋", "冬"], "correct_label": "D"}
    payload = _content_payload() | {"questions": [question for _ in range(6)]}
    content_path = tmp_path / "questions.json"
    content_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    loaded = load_demo_content(content_path)

    assert loaded.questions[0].options == ("春", "夏", "秋", "冬")
    assert loaded.questions[0].correct_label == "D"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 0),
        ("schema_version", 2),
        ("schema_version", True),
        ("schema_version", "1"),
        ("schema_version", 1.0),
        ("questions", []),
        ("questions", [_question_payload()]),
        ("questions", [_question_payload()] * 2),
        ("questions", None),
        ("unknown", "ignored otherwise"),
    ],
)
def test_content_rejects_invalid_shape_or_types(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        DemoContent.model_validate_json(json.dumps(_content_payload() | {field: value}))


@pytest.mark.parametrize("field", ["schema_version", "questions"])
def test_content_requires_all_fields(field: str) -> None:
    payload = _content_payload()
    del payload[field]
    with pytest.raises(ValidationError):
        DemoContent.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("question_count", [3, 8, 12])
def test_variable_question_counts_keep_seed_identity_and_reserved_ids(question_count: int) -> None:
    content = DemoContent.model_validate_json(
        json.dumps({"schema_version": 1, "questions": [_question_payload()] * question_count})
    )

    seeds = build_demo_question_seeds(content)

    assert len(seeds) == question_count
    stable_seeds = seeds[: len(DEMO_QUESTION_SEEDS)]
    assert [seed.id for seed in stable_seeds] == [
        seed.id for seed in DEMO_QUESTION_SEEDS[:question_count]
    ]
    all_ids = [
        identifier for seed in seeds for identifier in (seed.id, *(o.id for o in seed.options))
    ]
    assert len(set(all_ids)) == question_count * 5
    assert not {str(identifier)[-12:] for identifier in all_ids} & {
        "000000000001",
        "000000000007",
        "000000000008",
    }
    for seed, expected in zip(stable_seeds, DEMO_QUESTION_SEEDS, strict=False):
        assert [option.id for option in seed.options] == [option.id for option in expected.options]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("prompt", " "),
        ("prompt", "本番の問題"),
        ("prompt", "[DEMO]" + "あ" * 495),
        ("prompt", 1),
        ("explanation", ""),
        ("explanation", "本番の解説"),
        ("explanation", "[DEMO]" + "あ" * 995),
        ("explanation", None),
        ("options", []),
        ("options", ["A", "B", "C"]),
        ("options", ["A", "B", "C", "D", "E"]),
        ("options", ["A", "B", "C", " "]),
        ("options", ["A", "B", "C", "A"]),
        ("options", ["A", "B", "C", " A "]),
        ("options", ["A", "B", "C", "あ" * 501]),
        ("options", ["A", "B", "C", 4]),
        ("correct_label", "E"),
        ("correct_label", "a"),
        ("correct_label", None),
        ("correct_label", 1),
        ("correct_label", ["A", "B"]),
        ("unknown", "ignored otherwise"),
    ],
)
def test_question_rejects_invalid_text_options_or_answer(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        DemoQuestionContent.model_validate_json(json.dumps(_question_payload() | {field: value}))


@pytest.mark.parametrize("field", ["prompt", "explanation", "options", "correct_label"])
def test_question_requires_all_fields(field: str) -> None:
    payload = _question_payload()
    del payload[field]
    with pytest.raises(ValidationError):
        DemoQuestionContent.model_validate_json(json.dumps(payload))


def test_text_is_trimmed_and_accepts_length_boundaries() -> None:
    payload = _question_payload() | {
        "prompt": " [DEMO]" + "あ" * 494 + " ",
        "explanation": " [DEMO]" + "あ" * 994 + " ",
        "options": [" あ ", "い" * 500, "う", "え"],
    }
    question = DemoQuestionContent.model_validate_json(json.dumps(payload))

    assert len(question.prompt) == 500
    assert len(question.explanation) == 1000
    assert question.options[0] == "あ"
    assert len(question.options[1]) == 500


def test_content_is_deeply_immutable() -> None:
    content = load_demo_content()
    with pytest.raises(ValidationError, match="frozen_instance"):
        content.schema_version = 1
    with pytest.raises(ValidationError, match="frozen_instance"):
        content.questions[0].prompt = "[DEMO] 変更"


@pytest.mark.parametrize("body", [b'{"private-answer":', b'{"prompt":"\xff"}'])
def test_loader_rejects_malformed_json_without_exposing_input(tmp_path: Path, body: bytes) -> None:
    content_path = tmp_path / "questions.json"
    content_path.write_bytes(body)
    with pytest.raises(ValueError) as caught:
        load_demo_content(content_path)
    assert "private-answer" not in str(caught.value)


@pytest.mark.parametrize("is_directory", [False, True])
def test_loader_rejects_unavailable_files(tmp_path: Path, is_directory: bool) -> None:
    path = tmp_path if is_directory else tmp_path / "missing.json"
    with pytest.raises((OSError, ValueError)):
        load_demo_content(path)


@pytest.mark.parametrize("use_default", [False, True])
def test_check_is_offline_and_reports_only_counts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    use_default: bool,
) -> None:
    def reject_database(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("問題JSONの検証からDBへ接続してはいけません")

    monkeypatch.setattr(Engine, "connect", reject_database)
    arguments = [] if use_default else ["--content", str(DEFAULT_DEMO_CONTENT_PATH)]
    assert main(arguments) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "status": "ready",
        "question_count": 10,
        "option_count": 40,
        "database_accessed": False,
    }
    assert output.err == ""


@pytest.mark.parametrize("body", [None, b'{"private-answer":', b'{"prompt":"\xff"}'])
def test_check_redacts_invalid_input_and_file_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], body: bytes | None
) -> None:
    content_path = tmp_path / "private-questions.json"
    if body is not None:
        content_path.write_bytes(body)
    assert main(["--content", str(content_path)]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Error: demo_content_invalid\n"
