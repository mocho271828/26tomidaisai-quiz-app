from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError

from quiz_app.domain.errors import DomainInvariantError
from quiz_app.quiz_configuration.contracts import QuizSettings

DEFAULT_SETTINGS_PATH = Path(__file__).with_name("quiz-settings.json")


def load_quiz_settings(path: Path) -> QuizSettings:
    """単一JSONを検証し、入力本文を例外へ含めずに拒否する。"""
    try:
        return QuizSettings.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as error:
        raise DomainInvariantError("quiz_configuration_invalid") from error


@lru_cache(maxsize=1)
def get_quiz_settings() -> QuizSettings:
    """起動時の設定を固定し、ファイル編集で開催途中の挙動を変更しない。"""
    return load_quiz_settings(DEFAULT_SETTINGS_PATH)
