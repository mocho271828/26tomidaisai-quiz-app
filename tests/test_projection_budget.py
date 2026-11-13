from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import TimeoutError as DatabaseTimeoutError

from quiz_app.config import Settings
from quiz_app.realtime.projection_budget import ProjectionReadBudget


def test_background_capacity_does_not_allow_two_normal_reads() -> None:
    budget = ProjectionReadBudget(2)
    with budget.read(normal=True):
        with pytest.raises(DatabaseTimeoutError, match="admission_expired"):
            with budget.read(normal=True, timeout=0.001):
                pytest.fail("normal work must remain serialized")
        with budget.read():
            with pytest.raises(DatabaseTimeoutError, match="admission_expired"):
                with budget.read(timeout=0.001):
                    pytest.fail("background work must remain bounded")
    # 期限切れの待機も枠・進行待ち数を残さず、後続要求を受け付ける。
    with budget.read(normal=True), budget.read():
        pass


def test_waiting_progress_is_admitted_before_next_normal_read() -> None:
    budget = ProjectionReadBudget(1)
    progress_started = Event()
    progress_release = Event()
    normal_started = Event()

    def progress() -> None:
        with budget.read():
            progress_started.set()
            assert progress_release.wait(2)

    def normal() -> None:
        with budget.read(normal=True):
            normal_started.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        with budget.read(normal=True):
            progress_task = executor.submit(progress)
            with budget._condition:
                assert budget._condition.wait_for(lambda: budget._progress_waiting == 1, timeout=2)
            normal_task = executor.submit(normal)
        assert progress_started.wait(2)
        assert not normal_started.is_set()
        progress_release.set()
        progress_task.result(timeout=2)
        normal_task.result(timeout=2)
        assert normal_started.is_set()


def test_sync_pool_requires_operator_reserve_without_increasing_default() -> None:
    assert Settings(_env_file=None).database_sync_pool_size == 3
    with pytest.raises(ValidationError, match="database_sync_pool_size"):
        Settings(database_sync_pool_size=1, _env_file=None)
