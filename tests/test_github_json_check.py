import json
import shutil
import socket
from pathlib import Path

import boto3.session
import pytest
from sqlalchemy import Engine

from quiz_app.bootstrap.demo_content_loading import load_demo_content
from quiz_app.quiz_configuration.loading import load_quiz_settings
from quiz_app.rehearsal.content import load_rehearsal_content
from validation.github_json_check import JSON_CHECKS, REPOSITORY_ROOT, main

CONTENT_PATH, DEMO_PATH, SETTINGS_PATH = (path for path, _loader in JSON_CHECKS)


@pytest.fixture
def repository_root(tmp_path: Path) -> Path:
    for relative_path, _loader in JSON_CHECKS:
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPOSITORY_ROOT / relative_path, target)
    return tmp_path


def test_all_three_json_checks_are_offline_and_omit_question_content(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def reject_external_io(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("JSON検証からDB・AWS・ネットワークへ接続してはいけません")

    monkeypatch.setattr(Engine, "connect", reject_external_io)
    monkeypatch.setattr(boto3.session.Session, "client", reject_external_io)
    monkeypatch.setattr(boto3.session.Session, "resource", reject_external_io)
    monkeypatch.setattr(socket, "create_connection", reject_external_io)

    assert main([]) == 0

    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "status": "ready",
        "validated_files": [path for path, _loader in JSON_CHECKS],
        "database_accessed": False,
        "aws_accessed": False,
    }
    assert output.err == ""


@pytest.mark.parametrize("relative_path", [CONTENT_PATH, DEMO_PATH, SETTINGS_PATH])
@pytest.mark.parametrize("body", [None, b'{"private-answer":', b'{"private-answer":"\xff"}'])
def test_invalid_or_missing_json_fails_without_exposing_input_or_absolute_path(
    repository_root: Path,
    capsys: pytest.CaptureFixture[str],
    relative_path: str,
    body: bytes | None,
) -> None:
    target = repository_root / relative_path
    if body is None:
        target.unlink()
    else:
        target.write_bytes(body)

    assert main(["--repository-root", str(repository_root)]) == 2

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == f"Error: json_validation_failed: {relative_path}\n"


@pytest.mark.parametrize("relative_path", [CONTENT_PATH, DEMO_PATH, SETTINGS_PATH])
def test_valid_json_with_invalid_domain_values_is_rejected(
    repository_root: Path,
    capsys: pytest.CaptureFixture[str],
    relative_path: str,
) -> None:
    target = repository_root / relative_path
    if relative_path == CONTENT_PATH:
        payload = load_rehearsal_content(target).model_dump(mode="json")
        payload["selected_question_ids"] = [1, 2, 3, 4, 4]
    elif relative_path == DEMO_PATH:
        payload = load_demo_content(target).model_dump(mode="json")
        payload["questions"] = []
    else:
        payload = load_quiz_settings(target).model_dump(mode="json")
        payload["start_delay_seconds"] = 0
    target.write_text(json.dumps(payload), encoding="utf-8")

    assert main(["--repository-root", str(repository_root)]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == f"Error: json_validation_failed: {relative_path}\n"


def test_failure_reports_every_invalid_file_without_a_success_summary(
    repository_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for relative_path, _loader in JSON_CHECKS:
        (repository_root / relative_path).write_text("{}", encoding="utf-8")

    assert main(["--repository-root", str(repository_root)]) == 2

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.splitlines() == [
        f"Error: json_validation_failed: {relative_path}" for relative_path, _loader in JSON_CHECKS
    ]
