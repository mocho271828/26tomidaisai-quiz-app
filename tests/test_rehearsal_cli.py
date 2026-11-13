import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import logfire
import pytest

from quiz_app.config import Settings
from quiz_app.rehearsal.preparation import RehearsalPreparationResult
from validation import rehearsal

ROOT = Path(__file__).resolve().parents[1]
CONTENT = ROOT / "content/event-questions.json"
EVENT_ID = UUID("5fbeec40-7c35-4b63-8d6f-597888888888")


def test_check_is_offline_and_reports_selection_without_answers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def reject_prepare(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("checkからDB準備を呼んではいけません")

    monkeypatch.setattr(rehearsal, "prepare_rehearsal_event", reject_prepare)
    monkeypatch.setattr(rehearsal, "validate_release_manifest_file", reject_prepare)
    assert rehearsal.main(["check", "--content", str(CONTENT)]) == 0
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["selected_question_ids"] == list(range(1, 9))
    assert report["database_accessed"] is False
    assert len(report["content_sha256"]) == 64
    for question in rehearsal.load_rehearsal_content(CONTENT).questions:
        assert question.prompt not in output.out
        assert question.explanation not in output.out
    assert output.err == ""


@pytest.mark.parametrize("body", ['{"secret":"must-not-appear"}', "invalid"])
def test_check_redacts_invalid_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], body: str
) -> None:
    content = tmp_path / "questions.json"
    content.write_text(body)
    assert rehearsal.main(["check", "--content", str(content)]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Error: rehearsal_input_invalid\n"


def _prepare_arguments(tmp_path: Path) -> list[str]:
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "event_id": str(EVENT_ID),
                "title": "ゲストクイズ",
                "slug": "quiz-rehearsal",
                "scoring_policy_version": "approved-scoring",
                "display_name_policy_version": "approved-display",
            }
        )
    )
    content = rehearsal.load_rehearsal_content(CONTENT)
    return [
        "prepare",
        "--content",
        str(CONTENT),
        "--event-file",
        str(event),
        "--release-file",
        str(tmp_path / "release.env"),
        "--repository-root",
        str(ROOT),
        "--confirm-event-id",
        str(EVENT_ID),
        "--expected-content-sha256",
        rehearsal.content_digest(content),
    ]


@pytest.mark.parametrize(
    ("flag", "value", "code"),
    [
        ("--expected-content-sha256", "0" * 64, "rehearsal_content_digest_mismatch"),
        (
            "--confirm-event-id",
            "1fbeec40-7c35-4b63-8d6f-597888888888",
            "rehearsal_event_confirmation_mismatch",
        ),
        ("--repository-root", "/private/tmp", "rehearsal_repository_root_mismatch"),
    ],
)
def test_prepare_refuses_changed_confirmation_before_release_or_database_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flag: str,
    value: str,
    code: str,
) -> None:
    def reject_access(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("確認不一致時に外部設定やDBへ進んではいけません")

    monkeypatch.setattr(rehearsal, "validate_release_manifest_file", reject_access)
    monkeypatch.setattr(rehearsal, "prepare_rehearsal_event", reject_access)
    arguments = _prepare_arguments(tmp_path)
    arguments[arguments.index(flag) + 1] = value
    assert rehearsal.main(arguments) == 2
    assert capsys.readouterr().err == f"Error: {code}\n"


def test_prepare_passes_verified_host_ca_and_event_identity_to_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    arguments = _prepare_arguments(tmp_path)
    release = SimpleNamespace(
        event_id=EVENT_ID,
        source_commit="a" * 40,
        round_scope="rehearsal-six-round",
        runtime_env_file=tmp_path / "runtime.env",
        postgres_ca_file=tmp_path / "approved-ca.pem",
    )
    calls: list[str] = []
    monkeypatch.setattr(rehearsal, "validate_release_manifest_file", lambda *_: release)
    monkeypatch.setattr(rehearsal, "_require_release_source", lambda *_: None)
    for name in (
        "validate_release_execution_identity",
        "validate_postgres_ca_file",
        "validate_round_backup_host_directory",
    ):

        def record_check(*_args: object, _name: str = name, **_kwargs: object) -> None:
            calls.append(_name)

        monkeypatch.setattr(rehearsal, name, record_check)
    settings = Settings(_env_file=None)
    monkeypatch.setattr(rehearsal, "validate_runtime_settings", lambda *_: settings)

    def fake_prepare(
        actual_settings: Settings,
        event: rehearsal.RehearsalEventInput,
        content: rehearsal.RehearsalContent,
        *,
        postgres_ca_file: Path,
    ) -> RehearsalPreparationResult:
        assert len(calls) == 3
        assert actual_settings.aws_demo_expected_event_id == EVENT_ID
        assert postgres_ca_file == release.postgres_ca_file
        assert event.event_id == EVENT_ID
        assert len(content.selected_questions) == 8
        return RehearsalPreparationResult(
            event_id=EVENT_ID, created_questions=8, created_options=32
        )

    monkeypatch.setattr(rehearsal, "prepare_rehearsal_event", fake_prepare)
    assert rehearsal.main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["created_questions"] == 8


@pytest.mark.parametrize("head,dirty", [("b" * 40, ""), ("a" * 40, " M validation/rehearsal.py")])
def test_source_check_rejects_different_commit_and_modified_preparation_code(
    monkeypatch: pytest.MonkeyPatch,
    head: str,
    dirty: str,
) -> None:
    monkeypatch.setattr(rehearsal.shutil, "which", lambda _: "/usr/bin/git")
    runner = Mock(side_effect=[SimpleNamespace(stdout=head), SimpleNamespace(stdout=dirty)])
    monkeypatch.setattr(rehearsal.subprocess, "run", runner)
    with pytest.raises(
        rehearsal.RehearsalPreparationRefusedError, match="rehearsal_release_source_mismatch"
    ):
        rehearsal._require_release_source(ROOT, "a" * 40)
    assert runner.call_args.kwargs["timeout"] == 10
    assert "content" not in runner.call_args.args[0]


def test_cli_logs_only_sanitized_preparation_failure_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail_prepare(*_args: object) -> None:
        try:
            raise ValueError("secret-original-driver-message")
        except ValueError:
            try:
                raise rehearsal.RehearsalPreparationRefusedError(
                    "rehearsal_database_failed"
                ) from None
            except rehearsal.RehearsalPreparationRefusedError:
                logfire.exception("rehearsal preparation database failed")
                raise

    monkeypatch.setattr(rehearsal, "_prepare", fail_prepare)
    assert rehearsal.main(_prepare_arguments(tmp_path)) == 2
    logfire.force_flush()
    output = capsys.readouterr()
    assert "rehearsal_database_failed" in output.err
    assert "Traceback" in output.err
    assert "secret-original-driver-message" not in output.err
    assert output.out == ""
