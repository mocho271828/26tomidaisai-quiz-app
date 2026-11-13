"""監視失敗の位置を残し、AWS応答・URL・例外本文は記録しない。"""

from pathlib import Path
from typing import Literal

import logfire

FailureStage = Literal["configuration", "cloudwatch_write", "probe", "collector"]


def record_failure(stage: FailureStage, error: BaseException) -> None:
    """秘密値を含み得る例外の代わりに、監視module内のstack位置だけを出力する。"""
    frames: list[dict[str, str | int]] = []
    trace = error.__traceback__
    root = Path(__file__).resolve().parent
    while trace is not None and len(frames) < 12:
        code = trace.tb_frame.f_code
        try:
            source = Path(code.co_filename).relative_to(root)
        except ValueError:
            trace = trace.tb_next
            continue
        frames.append({"module": source.name, "function": code.co_name, "line": trace.tb_lineno})
        trace = trace.tb_next
    logfire.error("system monitoring failed", stage=stage, stack=frames, _exc_info=False)
