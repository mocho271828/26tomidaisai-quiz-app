"""CloudWatchへの送信先と有限稼働期限。API・DBの秘密値は持たない。"""

import os
import stat
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

STATE_DIRECTORY = Path("/var/lib/quiz-monitoring")
NAMESPACE = "GSQuiz/SystemHealth"


class CollectorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    region: Literal["ap-northeast-1"] = "ap-northeast-1"
    environment_id: str = Field(pattern=r"^gs-rehearsal-[a-z0-9-]{1,21}$")
    expires_at: AwareDatetime


def read_collector_config(path: Path) -> bytes:
    """管理者が配置したregular fileだけを、有限サイズで読み取る。"""
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("monitoring_config_path_invalid")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o022
            or metadata.st_size > 16_384
        ):
            raise ValueError("monitoring_config_file_invalid")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            body = stream.read(16_385)
        if len(body) > 16_384:
            raise ValueError("monitoring_config_too_large")
        return body
    finally:
        os.close(descriptor)


def load_collector_config(path: Path) -> CollectorConfig:
    """読取り済み設定を現在の送信専用契約で検証する。"""
    return CollectorConfig.model_validate_json(read_collector_config(path))
