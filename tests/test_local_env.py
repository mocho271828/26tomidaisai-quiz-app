import stat
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from quiz_app.config import Settings


def test_local_env_initialization_preserves_private_key_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ROUND_BACKUP_ENCRYPTION_KEY")
    root = Path(__file__).resolve().parents[1]
    template = (root / ".env.example").read_bytes()
    command = [sys.executable, str(root / "scripts" / "init-local-env.py")]
    keys = []
    for name in ("one", "two"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / ".env.example").write_bytes(template)
        first_run = subprocess.run(command, cwd=directory, check=True, capture_output=True)  # noqa: S603
        destination = directory / ".env"
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600
        original = destination.read_bytes()
        key = (
            Settings(_env_file=destination).round_backup_encryption_key.get_secret_value().encode()
        )
        Fernet(key)
        assert key not in first_run.stdout + first_run.stderr
        keys.append(key)
        result = subprocess.run(command, cwd=directory, check=True, capture_output=True)  # noqa: S603
        assert key not in result.stdout + result.stderr
        assert destination.read_bytes() == original
    assert keys[0] != keys[1]
