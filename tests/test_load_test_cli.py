import io
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from quiz_app.load_test import cli as cli_module
from quiz_app.load_test.bootstrap import LoadTestBootstrapResult
from quiz_app.load_test.cli import main
from quiz_app.load_test.manifest import load_manifest
from quiz_app.schema_revision import SCHEMA_REVISION

IMAGE = f"registry.aws.internal/quiz/api@sha256:{'1' * 64}"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "11111111-1111-4111-8111-111111111111"
EVENT_ID = "22222222-2222-4222-8222-222222222222"
EVENT_SLUG = "load-test-2026-09-03"


class TtyInput(io.StringIO):
    def isatty(self) -> bool:
        return True


def _init_arguments(output: Path, repository_root: Path) -> list[str]:
    return [
        "init",
        "--run-id",
        RUN_ID,
        "--event-id",
        EVENT_ID,
        "--event-slug",
        EVENT_SLUG,
        "--scheduled-start-at",
        "2099-09-03T01:00:00+00:00",
        "--public-origin",
        "https://quiz.aws.internal",
        "--source-commit",
        "a" * 40,
        "--api-image",
        IMAGE,
        "--gateway-image",
        IMAGE,
        "--generator-image",
        IMAGE,
        "--scoring-policy-version",
        "scoring-v1",
        "--scoring-base-points",
        "10000",
        "--scoring-initial-speed-bonus",
        "500",
        "--scoring-speed-bucket-ms",
        "100",
        "--scoring-speed-decrement-per-bucket",
        "10",
        "--scoring-standard-duration-ms",
        "10000",
        "--scoring-is-approved",
        "true",
        "--display-name-policy-version",
        "display-v1",
        "--display-notice-version",
        "notice-v1",
        "--display-max-code-points",
        "20",
        "--display-reject-patterns-json",
        "[]",
        "--output",
        str(output),
        "--repository-root",
        str(repository_root),
    ]


def test_init_cli_writes_secret_free_manifest_without_overwrite(
    tmp_path: Path,
) -> None:
    artifact_directory = tmp_path / "artifacts"
    artifact_directory.mkdir(mode=0o700)
    output = artifact_directory / "run.json"

    assert main(_init_arguments(output, REPOSITORY_ROOT)) == os.EX_OK
    manifest = load_manifest(output, REPOSITORY_ROOT)
    assert manifest.event.slug == "load-test-2026-09-03"
    assert manifest.format == "load-test-manifest-v3"
    assert "database_url" not in output.read_text(encoding="utf-8")

    assert main(_init_arguments(output, REPOSITORY_ROOT)) == os.EX_CANTCREAT
    assert load_manifest(output, REPOSITORY_ROOT) == manifest


@pytest.mark.parametrize(
    "missing_flag",
    [
        "--run-id",
        "--event-id",
        "--source-commit",
        "--api-image",
    ],
)
def test_init_cli_requires_identity_before_manifest_generation(
    missing_flag: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifact_directory = tmp_path / "artifacts"
    artifact_directory.mkdir(mode=0o700)
    output = artifact_directory / "run.json"
    arguments = _init_arguments(output, REPOSITORY_ROOT)
    missing_index = arguments.index(missing_flag)
    del arguments[missing_index : missing_index + 2]
    monkeypatch.setattr(
        cli_module,
        "build_load_test_manifest",
        lambda **_: pytest.fail("manifestを生成してはいけません"),
    )

    assert main(arguments) == os.EX_CONFIG
    assert output.exists() is False
    assert capsys.readouterr().err == '{"status":"refused","code":"invalid_contract"}\n'


def test_init_cli_redacts_invalid_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifact_directory = tmp_path / "artifacts"
    artifact_directory.mkdir(mode=0o700)
    arguments = _init_arguments(artifact_directory / "run.json", REPOSITORY_ROOT)
    unsafe_marker = f"invalid-private-value-{uuid4()}"
    arguments[arguments.index("--api-image") + 1] = unsafe_marker

    assert main(arguments) == os.EX_CONFIG

    captured = capsys.readouterr()
    assert unsafe_marker not in captured.out
    assert unsafe_marker not in captured.err
    assert captured.err == '{"status":"refused","code":"invalid_contract"}\n'


def test_cli_parser_redacts_invalid_number_unknown_argument_and_missing_argument(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifact_directory = tmp_path / "artifacts"
    artifact_directory.mkdir(mode=0o700)
    output = artifact_directory / "run.json"
    invalid_number_marker = f"private-number-{uuid4()}"
    unknown_argument_marker = f"private-argument-{uuid4()}"

    invalid_number_arguments = _init_arguments(output, REPOSITORY_ROOT)
    invalid_number_arguments[
        invalid_number_arguments.index("--scoring-base-points") + 1
    ] = invalid_number_marker
    unknown_arguments = [
        *_init_arguments(output, REPOSITORY_ROOT),
        f"--{unknown_argument_marker}",
    ]
    missing_arguments = _init_arguments(output, REPOSITORY_ROOT)
    event_id_index = missing_arguments.index("--event-id")
    del missing_arguments[event_id_index : event_id_index + 2]

    for arguments, secret_marker in (
        (invalid_number_arguments, invalid_number_marker),
        (unknown_arguments, unknown_argument_marker),
        (missing_arguments, "--event-id"),
    ):
        assert main(arguments) == os.EX_CONFIG
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == '{"status":"refused","code":"invalid_contract"}\n'
        assert secret_marker not in captured.err

    assert output.exists() is False


def _bootstrap_arguments(manifest_path: Path) -> list[str]:
    return [
        "bootstrap",
        "--manifest",
        str(manifest_path),
        "--repository-root",
        str(REPOSITORY_ROOT),
    ]


def _create_manifest(tmp_path: Path) -> Path:
    artifact_directory = tmp_path / "artifacts"
    artifact_directory.mkdir(mode=0o700)
    manifest_path = artifact_directory / "run.json"
    assert main(_init_arguments(manifest_path, REPOSITORY_ROOT)) == os.EX_OK
    return manifest_path


def test_bootstrap_cli_requires_tty_before_database_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path = _create_manifest(tmp_path)
    capsys.readouterr()
    monkeypatch.setattr(
        cli_module,
        "bootstrap_load_test_event",
        lambda *_: pytest.fail("bootstrapしてはいけません"),
    )

    assert (
        main(_bootstrap_arguments(manifest_path), input_stream=io.StringIO())
        == os.EX_CONFIG
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        '{"status":"refused","code":"load_test_bootstrap_confirmation_requires_tty"}\n'
    )


def test_bootstrap_cli_rejects_confirmation_phrase_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path = _create_manifest(tmp_path)
    capsys.readouterr()
    monkeypatch.setattr(
        cli_module,
        "bootstrap_load_test_event",
        lambda *_: pytest.fail("bootstrapしてはいけません"),
    )

    assert (
        main(
            _bootstrap_arguments(manifest_path),
            input_stream=TtyInput("CONFIRM wrong\n"),
        )
        == os.EX_CONFIG
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "confirmation_mismatch" in captured.err


def test_bootstrap_cli_accepts_exact_confirmation_and_passes_validated_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path = _create_manifest(tmp_path)
    capsys.readouterr()
    settings = SimpleNamespace(database_url="postgresql+psycopg://safe.invalid/load")
    received_settings: list[object] = []

    monkeypatch.setattr(
        cli_module,
        "Settings",
        lambda **_: settings,
    )

    def bootstrap(
        settings_argument: object, _manifest: object
    ) -> LoadTestBootstrapResult:
        received_settings.append(settings_argument)
        return LoadTestBootstrapResult(
            run_id=RUN_ID,
            event_id=EVENT_ID,
            schema_revision=SCHEMA_REVISION,
            created_events=1,
            created_questions=10,
            created_options=40,
            created_rounds=1,
        )

    monkeypatch.setattr(
        cli_module,
        "bootstrap_load_test_event",
        bootstrap,
    )
    confirmation = TtyInput(f"CONFIRM {RUN_ID} BOOTSTRAP {EVENT_SLUG}\n")

    assert (
        main(_bootstrap_arguments(manifest_path), input_stream=confirmation) == os.EX_OK
    )

    captured = capsys.readouterr()
    assert '"created_questions":10' in captured.out
    assert received_settings == [settings]


def test_bootstrap_cli_redacts_database_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path = _create_manifest(tmp_path)
    capsys.readouterr()
    secret_marker = f"private-db-secret-{uuid4()}"
    monkeypatch.setattr(
        cli_module,
        "Settings",
        lambda **_: SimpleNamespace(
            database_url=f"postgresql+psycopg://user:{secret_marker}@db.invalid/load"
        ),
    )

    def fail_bootstrap(*_: object) -> LoadTestBootstrapResult:
        raise SQLAlchemyError(f"driver leaked {secret_marker}")

    monkeypatch.setattr(cli_module, "bootstrap_load_test_event", fail_bootstrap)
    confirmation = TtyInput(f"CONFIRM {RUN_ID} BOOTSTRAP {EVENT_SLUG}\n")

    assert (
        main(_bootstrap_arguments(manifest_path), input_stream=confirmation)
        == os.EX_UNAVAILABLE
    )

    captured = capsys.readouterr()
    assert secret_marker not in captured.out
    assert secret_marker not in captured.err
    assert captured.out == ""
    assert captured.err.endswith('{"status":"failed","code":"dependency_error"}\n')
