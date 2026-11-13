"""ローカル確認用の問題JSONを、DBに接続せず検証する。"""

import argparse
import json
import sys
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

DEFAULT_DEMO_CONTENT_PATH = Path(__file__).with_name("demo-questions.json")
OptionText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class DemoQuestionContent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    prompt: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=1, max_length=500, pattern=r"^\[DEMO\]"
        ),
    ]
    explanation: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=1, max_length=1000, pattern=r"^\[DEMO\]"
        ),
    ]
    options: tuple[OptionText, ...] = Field(min_length=4, max_length=4)
    correct_label: Literal["A", "B", "C", "D"]

    @model_validator(mode="after")
    def validate_options(self) -> Self:
        if len(set(self.options)) != 4:
            raise ValueError("demo_option_content_must_be_distinct")
        return self


class DemoContent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    schema_version: int = Field(ge=1, le=1)
    questions: tuple[DemoQuestionContent, ...] = Field(min_length=3)


def load_demo_content(path: Path = DEFAULT_DEMO_CONTENT_PATH) -> DemoContent:
    """同梱JSONまたは確認用JSONを型付きの問題データとして読み込む。"""
    return DemoContent.model_validate_json(path.read_bytes())


def main(argv: list[str] | None = None) -> int:
    """問題本文・正答を出力せず、ファイルの編集結果だけを検証する。"""
    parser = argparse.ArgumentParser(description="ローカル用の問題JSONをDB接続なしで検証する")
    parser.add_argument("--content", type=Path, default=DEFAULT_DEMO_CONTENT_PATH)
    arguments = parser.parse_args(argv)
    try:
        content = load_demo_content(arguments.content)
    except (OSError, ValidationError):
        print("Error: demo_content_invalid", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "ready",
                "question_count": len(content.questions),
                "option_count": sum(len(question.options) for question in content.questions),
                "database_accessed": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
