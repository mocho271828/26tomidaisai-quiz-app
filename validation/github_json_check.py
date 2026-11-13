"""GitHub Actions向けに問題・設定JSONだけをオフライン検証する。"""

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from quiz_app.bootstrap.demo_content_loading import load_demo_content
from quiz_app.domain.errors import DomainInvariantError
from quiz_app.quiz_configuration.loading import load_quiz_settings
from quiz_app.rehearsal.content import RehearsalContentError, load_rehearsal_content

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
JSON_CHECKS: tuple[tuple[str, Callable[[Path], object]], ...] = (
    ("content/event-questions.json", load_rehearsal_content),
    ("backend/src/quiz_app/bootstrap/demo-questions.json", load_demo_content),
    ("backend/src/quiz_app/quiz_configuration/quiz-settings.json", load_quiz_settings),
)


def main(argv: list[str] | None = None) -> int:
    """本文・正答を出力せず、3つのJSONの検証結果を終了コードで返す。"""
    parser = argparse.ArgumentParser(description="問題・設定JSONをDB/AWS接続なしで検証する")
    parser.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT)
    arguments = parser.parse_args(argv)
    validated_files: list[str] = []
    for relative_path, loader in JSON_CHECKS:
        try:
            loader(arguments.repository_root / relative_path)
        except (OSError, ValidationError, RehearsalContentError, DomainInvariantError):
            # 入力値・正答・例外causeをCI logへ出さず、修正対象だけを固定pathで示す。
            print(f"Error: json_validation_failed: {relative_path}", file=sys.stderr)
        else:
            validated_files.append(relative_path)
    if len(validated_files) != len(JSON_CHECKS):
        return 2
    print(
        json.dumps(
            {
                "status": "ready",
                "validated_files": validated_files,
                "database_accessed": False,
                "aws_accessed": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
