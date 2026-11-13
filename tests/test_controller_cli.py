from __future__ import annotations

import io
import os
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import Self
from uuid import UUID

import pytest
from pydantic import SecretStr

from quiz_app import controller_cli
from quiz_app.load_test.controller_contracts import ControllerRefusal, ControllerStatus


class _TtyInput(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_parser_separates_readonly_result_wait_and_explicit_publish() -> None:
    parser = controller_cli._build_parser()
    arguments = [
        "--manifest",
        "/external/manifest.json",
        "--repository-root",
        "/repository",
        "--journal",
        "/external/journal.jsonl",
        "--checkpoint-directory",
        "/external/checkpoints",
        "--verification-request-directory",
        "/external/requests",
        "--ca-file",
        "/external/ca.pem",
        "--username",
        "operator",
    ]
    assert parser.parse_args(["wait-results", *arguments]).action == "wait-results"
    assert parser.parse_args(["publish", *arguments]).action == "publish"


def _arguments(tmp_path: Path) -> Namespace:
    directories = {
        name: tmp_path / name for name in ("manifest", "journal", "checkpoints", "requests", "tls")
    }
    for directory in directories.values():
        directory.mkdir(mode=0o700)
    manifest_path = directories["manifest"] / "manifest.json"
    manifest_path.write_text("{}")
    return Namespace(
        action="prepare",
        manifest=manifest_path,
        repository_root=tmp_path,
        journal=directories["journal"] / "controller.jsonl",
        checkpoint_directory=directories["checkpoints"],
        verification_request_directory=directories["requests"],
        username="operator",
        generator_evidence=None,
        ca_file=directories["tls"] / "ca.pem",
        receipt=None,
    )


def test_confirmation_requires_tty_and_exact_newline() -> None:
    with pytest.raises(ControllerRefusal, match="controller_confirmation_requires_tty"):
        controller_cli._read_confirmation("EXPECTED", io.StringIO("EXPECTED\n"))
    assert controller_cli._read_confirmation("EXPECTED", _TtyInput("EXPECTED\n")) == "EXPECTED"
    with pytest.raises(ControllerRefusal, match="controller_confirmation_mismatch"):
        controller_cli._read_confirmation("EXPECTED", _TtyInput("EXPECTED"))


def test_cli_rejects_manifest_and_writable_directory_overlap(tmp_path: Path) -> None:
    arguments = _arguments(tmp_path)
    arguments.verification_request_directory = arguments.manifest.parent

    with pytest.raises(controller_cli.ControllerArgumentError):
        controller_cli._require_cli_artifact_isolation(arguments)


def test_execute_reads_password_outside_argv_and_prints_only_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    arguments = _arguments(tmp_path)
    sensitive_value = "never-print-this-password"
    manifest = SimpleNamespace()

    class FakeHttpClient:
        def __init__(self, _manifest: object, *, reauthenticate: object, ca_file: Path) -> None:
            assert ca_file == arguments.ca_file
            self.reauthenticate = reauthenticate
            self.logins: list[tuple[str, str]] = []

        def login(self, username: str, password: SecretStr) -> None:
            self.logins.append((username, password.get_secret_value()))

    class FakeJournal:
        def __init__(self, *_args: object) -> None:
            pass

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class FakeController:
        def __init__(self, *_args: object) -> None:
            self.api = _args[-2]

        def confirmation_prompt(self, action: str, **_kwargs: object) -> str:
            assert action == "prepare"
            return "EXPECTED"

        def execute(
            self,
            action: str,
            *,
            password: SecretStr,
            **_kwargs: object,
        ) -> ControllerStatus:
            assert action == "prepare"
            assert password.get_secret_value() == sensitive_value
            self.api.reauthenticate(self.api)
            assert self.api.logins == [("operator", sensitive_value)]
            return ControllerStatus(
                run_id=UUID("11111111-1111-4111-8111-111111111111"),
                event_id=UUID("22222222-2222-4222-8222-222222222222"),
                source_commit="b" * 40,
                schema_revision="schema-v1",
                manifest_sha256="c" * 64,
                scenario_sha256="d" * 64,
                status="waiting",
                next_action="schedule",
                round_number=1,
                round_id=None,
                journal_head_sha256="a" * 64,
            )

    monkeypatch.setattr(controller_cli, "load_manifest", lambda *_args: manifest)
    monkeypatch.setattr(controller_cli, "ControllerHttpClient", FakeHttpClient)
    monkeypatch.setattr(controller_cli, "FileVerificationProvider", lambda: object())
    monkeypatch.setattr(controller_cli, "ControllerJournal", FakeJournal)
    monkeypatch.setattr(controller_cli, "LoadTestController", FakeController)

    result = controller_cli._execute(
        arguments,
        _TtyInput("EXPECTED\n"),
        lambda: sensitive_value,
    )

    captured = capsys.readouterr()
    assert result == os.EX_OK
    assert '"next_action":"schedule"' in captured.out
    assert sensitive_value not in captured.out + captured.err


def test_main_redacts_invalid_argv_detail(capsys: pytest.CaptureFixture[str]) -> None:
    sensitive_value = "argv-secret-must-not-leak"
    result = controller_cli.main([sensitive_value])

    captured = capsys.readouterr()
    assert result == os.EX_CONFIG
    assert captured.err == '{"status":"refused","code":"invalid_contract"}\n'
    assert sensitive_value not in captured.out + captured.err
