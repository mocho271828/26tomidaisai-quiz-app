"""fault controller専用Docker CLI configのroot-owned境界。"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Final

from quiz_app.load_test.fault_host_config import FaultControllerHostError

DOCKER_CONFIG_DIRECTORY: Final = Path("/etc/gs-loadtest/docker-cli")


def require_root_owned_docker_config_directory() -> Path:
    """HOME由来config/pluginを遮断する空のroot-owned directoryを検証する。"""

    directory = DOCKER_CONFIG_DIRECTORY
    descriptor = -1
    try:
        before = directory.lstat()
        resolved = directory.resolve(strict=True)
        if resolved != directory:
            raise FaultControllerHostError("fault_host_docker_config_invalid")
        for ancestor in (directory, *directory.parents):
            status = ancestor.lstat()
            if (
                stat.S_ISLNK(status.st_mode)
                or not stat.S_ISDIR(status.st_mode)
                or status.st_uid != 0
                or status.st_gid != 0
                or stat.S_IMODE(status.st_mode) & 0o022
            ):
                raise FaultControllerHostError("fault_host_docker_config_invalid")
        descriptor = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or not stat.S_ISDIR(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o755
            or opened.st_uid != 0
            or opened.st_gid != 0
            or os.listdir(descriptor)
        ):
            raise FaultControllerHostError("fault_host_docker_config_invalid")
        return directory
    except FaultControllerHostError:
        raise
    except OSError as error:
        raise FaultControllerHostError("fault_host_docker_config_invalid") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
