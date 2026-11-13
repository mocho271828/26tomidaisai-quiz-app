import os
from uuid import UUID

import pytest
from cryptography.fernet import Fernet

from quiz_app.quiz_configuration import (
    EventConfiguration,
    get_quiz_settings,
    initial_event_configuration,
)


def pytest_configure() -> None:
    """収集時のアプリ初期化にも、ローカル実データと分離した実行ごとの鍵を渡す。"""
    os.environ["ROUND_BACKUP_ENCRYPTION_KEY"] = Fernet.generate_key().decode("ascii")


@pytest.fixture
def legacy_quiz_configuration(monkeypatch: pytest.MonkeyPatch) -> EventConfiguration:
    """旧採点・backup単体fixtureでは設定境界だけを置換し、既存計算を検証する。"""
    configuration = EventConfiguration(
        settings=get_quiz_settings(),
        record=initial_event_configuration(UUID(int=1), get_quiz_settings()),
    )
    for module in (
        "quiz_app.domain.quiz_service",
        "quiz_app.operator.mutations",
        "quiz_app.audience.projection",
    ):
        monkeypatch.setattr(
            f"{module}.require_event_configuration", lambda _session, _event: configuration
        )
    return configuration
