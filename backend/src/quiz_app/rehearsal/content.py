import os
import stat
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

QuestionId = Annotated[int, Field(ge=1)]
OptionLabel = Literal["A", "B", "C", "D"]
ContentText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
PENDING_ANSWER_CONTENT = "当日確定する回答を入力してください"
MAXIMUM_CONTENT_BYTES = 65_536


class RehearsalContentError(ValueError):
    """問題本文や入力pathを含めず、問題fileの読込失敗を固定codeで表す。"""


class RehearsalOption(BaseModel):
    """問題バンクに保存する、正誤を重複管理しない選択肢。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    label: OptionLabel
    content: ContentText


class RehearsalQuestion(BaseModel):
    """正答ラベルを指定した、非公開の配信問題。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    id: QuestionId
    respondent: ContentText
    prompt: ContentText
    options: tuple[RehearsalOption, ...] = Field(min_length=4, max_length=4)
    correct_label: OptionLabel
    answer_note: ContentText | None = None

    @model_validator(mode="after")
    def validate_options(self) -> Self:
        if {option.label for option in self.options} != {"A", "B", "C", "D"}:
            raise ValueError("rehearsal_option_labels_must_be_a_through_d")
        if len({option.content for option in self.options}) != 4:
            raise ValueError("rehearsal_option_content_must_be_distinct")
        if any(option.content == PENDING_ANSWER_CONTENT for option in self.options):
            raise ValueError("rehearsal_finalized_question_has_pending_option")
        return self

    @property
    def display_prompt(self) -> str:
        """出題時にも誰への質問か分かる本文を返す。"""

        return f"【{self.respondent}】{self.prompt}"

    @property
    def explanation(self) -> str:
        """正答の編集を一箇所に集約するため、選択肢から解説を導出する。"""

        return next(option.content for option in self.options if option.label == self.correct_label)


class RehearsalContent(BaseModel):
    """練習1問と本番問題を開催へ取り込む、ソース編集不要の準備入力。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    schema_version: Annotated[int, Field(ge=1, le=1)]
    title: ContentText
    selected_question_ids: tuple[QuestionId, ...] = Field(min_length=3)
    questions: tuple[RehearsalQuestion, ...] = Field(min_length=3)

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        question_ids = {question.id for question in self.questions}
        selected_ids = set(self.selected_question_ids)
        if len(question_ids) != len(self.questions):
            raise ValueError("rehearsal_question_ids_must_be_distinct")
        if len(selected_ids) != len(self.selected_question_ids):
            raise ValueError("rehearsal_selected_question_ids_must_be_distinct")
        if not selected_ids.issubset(question_ids):
            raise ValueError("rehearsal_selected_question_ids_must_exist")
        return self

    @property
    def selected_questions(self) -> tuple[RehearsalQuestion, ...]:
        """JSONで指定した出題順に、正答ラベルを指定した問題を返す。"""

        questions_by_id = {question.id: question for question in self.questions}
        return tuple(questions_by_id[question_id] for question_id in self.selected_question_ids)


def read_rehearsal_json(path: Path) -> bytes:
    """問題集・開催入力のJSONを通常fileとサイズ上限で検証して読み込む。"""

    try:
        # FIFOによる待機やsymlink差替えを避け、開いた実体を確認してから本文を読む。
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RehearsalContentError("rehearsal_content_file_not_regular")
            with os.fdopen(descriptor, "rb", closefd=False) as content_file:
                body = content_file.read(MAXIMUM_CONTENT_BYTES + 1)
        finally:
            os.close(descriptor)
    except OSError:
        raise RehearsalContentError("rehearsal_content_file_unavailable") from None
    if len(body) > MAXIMUM_CONTENT_BYTES:
        raise RehearsalContentError("rehearsal_content_file_too_large")
    return body


def load_rehearsal_content(path: Path) -> RehearsalContent:
    """非公開のJSONを検証し、DBやネットワークへ接続せず読み込む。"""

    return RehearsalContent.model_validate_json(read_rehearsal_json(path))
