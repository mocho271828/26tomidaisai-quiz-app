from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from quiz_app import db_verifier_cli as cli
from quiz_app.load_test.db_verifier import DbVerifierRunResult
from quiz_app.load_test.db_verifier_contracts import LockObservationInput
from quiz_app.load_test.db_verifier_database import DbVerifierDatabaseError
from quiz_app.load_test.verification_contracts import ArtifactIdentity


def _arguments(tmp_path: Path) -> argparse.Namespace:
    input_directory = tmp_path / "input"
    manifest_directory = tmp_path / "manifest"
    secret_directory = tmp_path / "secrets"
    evidence_directory = tmp_path / "evidence"
    output_directory = tmp_path / "output"
    for directory in (
        input_directory,
        manifest_directory,
        secret_directory,
        evidence_directory,
        output_directory,
    ):
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
    files = {
        input_directory / "request.json": b"request",
        manifest_directory / "manifest.json": b"manifest",
        secret_directory / "database-url": b"secret-database-url",
        secret_directory / "ca.pem": b"certificate",
        evidence_directory / "generator.json": b"generator-evidence",
        evidence_directory / "real.json": b"real-device-evidence",
    }
    for path, body in files.items():
        path.write_bytes(body)
        path.chmod(0o600)
    return argparse.Namespace(
        input=input_directory / "request.json",
        manifest=manifest_directory / "manifest.json",
        repository_root=tmp_path / "repository",
        database_url_file=secret_directory / "database-url",
        ca_file=secret_directory / "ca.pem",
        ca_sha256="a" * 64,
        output_directory=output_directory,
        generator_evidence=evidence_directory / "generator.json",
    )


class _Database:
    def __init__(self) -> None:
        self.is_disposed = False

    def dispose(self) -> None:
        self.is_disposed = True


def _result(decision: str) -> DbVerifierRunResult:
    return DbVerifierRunResult(
        mode="per-round",
        decision=decision,
        report=ArtifactIdentity(name="report.json", sha256="1" * 64, size_bytes=100),
        receipt=(
            ArtifactIdentity(name="receipt.json", sha256="2" * 64, size_bytes=100)
            if decision == "PASS"
            else None
        ),
    )


@pytest.mark.parametrize(
    ("decision", "expected_status"),
    [("PASS", os.EX_OK), ("FAIL", os.EX_DATAERR)],
)
def test_execute_wires_only_external_secret_paths_and_disposes_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    decision: str,
    expected_status: int,
) -> None:
    arguments = _arguments(tmp_path)
    manifest = object()
    loaded = SimpleNamespace(value=object())
    settings = object()
    engine = object()
    database = _Database()
    observed_settings_args: tuple[object, ...] | None = None
    observed_run_kwargs: dict[str, object] | None = None

    monkeypatch.setattr(cli, "load_manifest", lambda *_args: manifest)
    monkeypatch.setattr(cli, "load_db_verifier_request", lambda *_args: loaded)

    def load_settings(*args: object) -> object:
        nonlocal observed_settings_args
        observed_settings_args = args
        return settings

    monkeypatch.setattr(cli, "load_verifier_database_settings", load_settings)
    monkeypatch.setattr(cli, "create_verifier_engine", lambda value: engine)
    monkeypatch.setattr(
        cli,
        "VerifierDatabase",
        lambda actual_engine, actual_settings: (
            database
            if (actual_engine, actual_settings) == (engine, settings)
            else pytest.fail("unexpected database constructor arguments")
        ),
    )

    def run_verifier(*_args: object, **kwargs: object) -> DbVerifierRunResult:
        nonlocal observed_run_kwargs
        observed_run_kwargs = kwargs
        return _result(decision)

    monkeypatch.setattr(cli, "run_db_verifier", run_verifier)

    status = cli._execute(arguments)

    assert status == expected_status
    assert observed_settings_args == (
        arguments.database_url_file,
        arguments.ca_file,
        arguments.ca_sha256,
        arguments.repository_root,
    )
    assert observed_run_kwargs == {
        "generator_evidence_path": arguments.generator_evidence,
    }
    assert database.is_disposed
    output = capsys.readouterr()
    assert "secret-database-url" not in output.out + output.err


def test_execute_requires_generator_evidence_for_verification_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _arguments(tmp_path)
    arguments.generator_evidence = None
    monkeypatch.setattr(cli, "load_manifest", lambda *_args: object())
    monkeypatch.setattr(
        cli,
        "load_db_verifier_request",
        lambda *_args: SimpleNamespace(value=object()),
    )

    with pytest.raises(cli.DbVerifierArgumentError):
        cli._execute(arguments)


def test_execute_rejects_evidence_paths_for_lock_observation_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _arguments(tmp_path)
    lock_input = LockObservationInput(
        format="load-test-db-lock-observation-input-v1",
        mode="observe-locks",
        run_id=UUID("10000000-0000-4000-8000-000000000001"),
        event_id=UUID("10000000-0000-4000-8000-000000000002"),
        source_commit="a" * 40,
        schema_revision="20260804_0001",
        manifest_sha256="b" * 64,
        scenario_sha256="c" * 64,
        round_number=1,
        observation_start_at=datetime(2026, 9, 3, tzinfo=UTC),
        observation_end_at=datetime(2026, 9, 3, tzinfo=UTC) + timedelta(seconds=15),
    )
    monkeypatch.setattr(cli, "load_manifest", lambda *_args: object())
    monkeypatch.setattr(
        cli,
        "load_db_verifier_request",
        lambda *_args: SimpleNamespace(value=lock_input),
    )

    with pytest.raises(cli.DbVerifierArgumentError):
        cli._execute(arguments)


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (ValueError("credential=do-not-print"), os.EX_CONFIG, "invalid_contract"),
        (
            DbVerifierDatabaseError("credential=do-not-print"),
            os.EX_UNAVAILABLE,
            "database_dependency_error",
        ),
        (OSError("credential=do-not-print"), os.EX_IOERR, "local_io_error"),
    ],
)
def test_main_redacts_internal_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    monkeypatch.setattr(cli.logfire, "configure", lambda **_kwargs: None)
    monkeypatch.setattr(
        cli, "_execute", lambda _arguments: (_ for _ in ()).throw(error)
    )
    argv = [
        "--input",
        "/external/request.json",
        "--manifest",
        "/repository/manifest.json",
        "--repository-root",
        "/repository",
        "--database-url-file",
        "/external/database-url",
        "--ca-file",
        "/external/ca.pem",
        "--ca-sha256",
        "a" * 64,
        "--output-directory",
        "/external/output",
    ]

    assert cli.main(argv) == expected_status

    output = capsys.readouterr()
    assert expected_code in output.err
    assert "do-not-print" not in output.out + output.err


def test_parser_does_not_accept_inline_database_secret(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli.logfire, "configure", lambda **_kwargs: None)
    inline_database_url = "postgresql://user:do-not-print@db.example/quiz"

    status = cli.main(["--database-url", inline_database_url])

    output = capsys.readouterr()
    assert status == os.EX_CONFIG
    assert "invalid_contract" in output.err
    assert inline_database_url not in output.out + output.err
