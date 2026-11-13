"""既存sync poolを背景読取りだけで使い切らないための取得前制御。"""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from threading import Condition
from time import monotonic

from sqlalchemy.exc import TimeoutError as DatabaseTimeoutError

from quiz_app.config import get_settings


class ProjectionReadDeferred(DatabaseTimeoutError):
    """DBへ接続する前の背景読取り枠が空いていない。"""


class ProjectionReadBudget:
    """運営用に最低一接続を残し、待機中の進行構築を通常結果より先にする。"""

    def __init__(self, capacity: int = 2) -> None:
        if capacity < 1:
            raise ValueError("projection_read_capacity_must_be_positive")
        self._capacity = capacity
        self._condition = Condition()
        self._active = 0
        self._normal_active = 0
        self._progress_waiting = 0

    @contextmanager
    def read(self, *, normal: bool = False, timeout: float = 5.0) -> Iterator[None]:
        deadline = monotonic() + timeout
        with self._condition:
            if not normal:
                self._progress_waiting += 1
            try:
                while self._active >= self._capacity or (
                    normal and (self._normal_active > 0 or self._progress_waiting > 0)
                ):
                    remaining = deadline - monotonic()
                    if remaining <= 0 or not self._condition.wait(remaining):
                        raise ProjectionReadDeferred("projection_read_admission_expired")
                self._active += 1
                if normal:
                    self._normal_active += 1
            finally:
                if not normal:
                    self._progress_waiting -= 1
                self._condition.notify_all()
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                if normal:
                    self._normal_active -= 1
                self._condition.notify_all()


@lru_cache
def get_projection_read_budget() -> ProjectionReadBudget:
    # 既定sync pool=3のうち背景全体2、本人結果1まで。pool拡張は行わない。
    return ProjectionReadBudget(min(2, get_settings().database_sync_pool_size - 1))
