"""既存schema・正式採点・専用運営・選択問題を、人が一度のcommandで準備する。"""

import argparse
import sys
from pathlib import Path
from uuid import UUID

import logfire
from pydantic import ValidationError

from quiz_app.aws_demo_paths import (
    RUNTIME_ENVIRONMENT_FILE_POLICY,
    AwsDemoDeploymentValidationError,
    read_validated_external_file,
    validate_external_regular_file,
)
from quiz_app.config import Settings
from quiz_app.rehearsal.content import (
    RehearsalContentError,
    load_rehearsal_content,
    read_rehearsal_json,
)
from quiz_app.rehearsal.initialization import (
    RehearsalInitializationSecrets,
    initialize_rehearsal_database,
)
from quiz_app.rehearsal.preparation import RehearsalEventInput, RehearsalPreparationRefusedError
from validation.aws_demo_deployment import (
    validate_postgres_ca_file,
    validate_release_execution_identity,
    validate_release_manifest_file,
    validate_round_backup_host_directory,
    validate_runtime_settings,
)
from validation.rehearsal import _prepare, _require_release_source, content_digest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="人が新規専用DBへ1回だけ行うリハーサル初期化")
    for field in ("content", "event-file", "release-file", "repository-root", "secrets-file"):
        parser.add_argument(f"--{field}", type=Path, required=True)
    parser.add_argument("--confirm-event-id", type=UUID, required=True)
    parser.add_argument("--expected-content-sha256", required=True)
    parser.add_argument("--confirm-current-policies", action="store_true", required=True)
    return parser


def _initialize(arguments: argparse.Namespace) -> None:
    content = load_rehearsal_content(arguments.content)
    event = RehearsalEventInput.model_validate_json(read_rehearsal_json(arguments.event_file))
    if (
        arguments.expected_content_sha256 != content_digest(content)
        or arguments.confirm_event_id != event.event_id
    ):
        raise RehearsalPreparationRefusedError("rehearsal_initialization_confirmation_mismatch")
    repository_root = Path(__file__).resolve().parents[1]
    if arguments.repository_root.resolve() != repository_root:
        raise RehearsalPreparationRefusedError("rehearsal_repository_root_mismatch")
    release = validate_release_manifest_file(arguments.release_file, repository_root)
    if release.round_scope != "rehearsal-six-round" or release.event_id != event.event_id:
        raise RehearsalPreparationRefusedError("rehearsal_release_identity_mismatch")
    _require_release_source(repository_root, release.source_commit)
    validate_release_execution_identity(release)
    settings = validate_runtime_settings(release, repository_root)
    validate_postgres_ca_file(release, repository_root)
    validate_round_backup_host_directory(
        release,
        repository_root,
        protected_paths=(
            arguments.release_file,
            release.runtime_env_file,
            release.postgres_ca_file,
            arguments.secrets_file,
        ),
    )
    validated = validate_external_regular_file(
        arguments.secrets_file, repository_root, RUNTIME_ENVIRONMENT_FILE_POLICY
    )
    secrets = RehearsalInitializationSecrets.model_validate_json(
        read_validated_external_file(
            validated,
            maximum_bytes=16_384,
            unavailable_code="rehearsal_initialization_secret_unavailable",
        )
    )
    settings = Settings(
        _env_file=None,
        **settings.model_dump(exclude={"aws_demo_expected_event_id"}),
        aws_demo_expected_event_id=event.event_id,
    )
    initialize_rehearsal_database(
        settings,
        event,
        secrets,
        repository_root=repository_root,
        postgres_ca_file=release.postgres_ca_file,
    )
    # 初期化後も既存prepareの確認・再読込・CA/DB guardを省略しない。
    _prepare(arguments, content)


def main(argv: list[str] | None = None) -> int:
    """人手実行時だけDBへ接続し、失敗は秘密値なしの固定codeで返す。"""
    arguments = _parser().parse_args(argv)
    logfire.configure(
        send_to_logfire=False,
        console=logfire.ConsoleOptions(
            output=sys.stderr,
            include_timestamps=False,
            show_project_link=False,
        ),
    )
    try:
        _initialize(arguments)
    except (ValidationError, OSError, UnicodeError):
        print("Error: rehearsal_initialization_input_invalid", file=sys.stderr)
        return 2
    except (
        AwsDemoDeploymentValidationError,
        RehearsalPreparationRefusedError,
        RehearsalContentError,
    ) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
