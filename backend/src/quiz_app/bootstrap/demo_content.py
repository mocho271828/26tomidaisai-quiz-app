from dataclasses import dataclass
from uuid import UUID

from quiz_app.bootstrap.demo_content_loading import DemoContent, load_demo_content


@dataclass(frozen=True, slots=True)
class DemoOptionSeed:
    id: UUID
    label: str
    content: str
    is_correct: bool


@dataclass(frozen=True, slots=True)
class DemoQuestionSeed:
    id: UUID
    version: int
    prompt: str
    explanation: str
    options: tuple[DemoOptionSeed, ...]


def _demo_id(sequence: int) -> UUID:
    return UUID(f"60000000-0000-4000-8000-{sequence:012d}")


def build_demo_question_seeds(content: DemoContent) -> tuple[DemoQuestionSeed, ...]:
    """検証済みの編集内容へ、既存seedと同じ固定identityを割り当てる。"""
    seeds: list[DemoQuestionSeed] = []
    # 内容編集で既存レコードのidentityが変わらないよう、UUIDはJSONの外で固定する。
    for index, question in enumerate(content.questions, start=1):
        # 初問の直後に予約済みのround/operator ID（7/8）を飛ばし、以降は5個ずつ使う。
        sequence = 2 if index == 1 else 9 + (index - 2) * 5
        options = tuple(
            DemoOptionSeed(
                id=_demo_id(sequence + option_index),
                label=label,
                content=option_text,
                is_correct=label == question.correct_label,
            )
            for option_index, (label, option_text) in enumerate(
                zip(("A", "B", "C", "D"), question.options, strict=True), start=1
            )
        )
        seeds.append(
            DemoQuestionSeed(
                id=_demo_id(sequence),
                version=index,
                prompt=question.prompt,
                explanation=question.explanation,
                options=options,
            )
        )
    return tuple(seeds)


DEMO_QUESTION_ID = _demo_id(2)
DEMO_OPTION_A_ID = _demo_id(3)
DEMO_OPTION_B_ID = _demo_id(4)
DEMO_OPTION_C_ID = _demo_id(5)
DEMO_OPTION_D_ID = _demo_id(6)
DEMO_QUESTION_SEEDS = build_demo_question_seeds(load_demo_content())
DEMO_QUESTION_IDS = tuple(question.id for question in DEMO_QUESTION_SEEDS)
