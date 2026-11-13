"""問題集のオフライン確認と、人が行うAWSリハーサルのDB準備を分離する。"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import logfire
from pydantic import ValidationError

from quiz_app.aws_demo_paths import AwsDemoDeploymentValidationError
from quiz_app.config import Settings
from quiz_app.rehearsal.content import (
    RehearsalContent,
    RehearsalContentError,
    load_rehearsal_content,
    read_rehearsal_json,
)
from quiz_app.rehearsal.preparation import (
    RehearsalEventInput,
    RehearsalPreparationRefusedError,
    prepare_rehearsal_event,
)
from validation.aws_demo_deployment import (
    validate_postgres_ca_file,
    validate_release_execution_identity,
    validate_release_manifest_file,
    validate_round_backup_host_directory,
    validate_runtime_settings,
)


def content_digest(content: RehearsalContent) -> str:
    """確認時の選択・本文・正答を、投入時に同じ内容へ束縛する。"""

    return hashlib.sha256(content.model_dump_json().encode("utf-8")).hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AWSリハーサルの問題確認・人手DB準備")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="DB/AWSへ接続せず問題と出題順を検証する")
    check.add_argument("--content", type=Path, required=True)
    prepare = commands.add_parser("prepare", help="人が準備済みDBへ開催と選択問題をINSERTする")
    prepare.add_argument("--content", type=Path, required=True)
    prepare.add_argument("--event-file", type=Path, required=True)
    prepare.add_argument("--release-file", type=Path, required=True)
    prepare.add_argument("--repository-root", type=Path, required=True)
    prepare.add_argument("--confirm-event-id", type=UUID, required=True)
    prepare.add_argument("--expected-content-sha256", required=True)
    return parser


def _require_release_source(repository_root: Path, source_commit: str) -> None:
    """問題集だけの編集を許し、DB投入コードと検証済みreleaseの食違いを拒否する。"""

    git = shutil.which("git")
    if git is None:
        raise RehearsalPreparationRefusedError("rehearsal_git_unavailable")
    commands = (
        [git, "rev-parse", "HEAD"],
        [
            git,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            "backend/src",
            "validation",
            "backend/pyproject.toml",
            "backend/uv.lock",
        ],
    )
    try:
        results = [
            # argvは上の固定read-only Git操作だけとし、shellへ入力を展開しない。
            subprocess.run(  # noqa: S603
                command,
                cwd=repository_root,
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout.strip()
            for command in commands
        ]
    except (subprocess.SubprocessError, OSError):
        raise RehearsalPreparationRefusedError("rehearsal_source_check_failed") from None
    if results != [source_commit, ""]:
        raise RehearsalPreparationRefusedError("rehearsal_release_source_mismatch")


def _prepare(arguments: argparse.Namespace, content: RehearsalContent) -> None:
    if arguments.expected_content_sha256 != content_digest(content):
        raise RehearsalPreparationRefusedError("rehearsal_content_digest_mismatch")
    event = RehearsalEventInput.model_validate_json(read_rehearsal_json(arguments.event_file))
    if event.event_id != arguments.confirm_event_id:
        raise RehearsalPreparationRefusedError("rehearsal_event_confirmation_mismatch")

    # 自己導出したcheckout以外のpathで外部設定fileの隔離検証をすり抜けさせない。
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
        ),
    )
    settings = Settings(
        _env_file=None,
        **settings.model_dump(exclude={"aws_demo_expected_event_id"}),
        aws_demo_expected_event_id=release.event_id,
    )
    result = prepare_rehearsal_event(
        settings, event, content, postgres_ca_file=release.postgres_ca_file
    )
    print(result.model_dump_json())


def main(argv: list[str] | None = None) -> int:
    """通常確認はread-onlyとし、秘密値・正答本文を標準出力へ展開しない。"""

    arguments = _parser().parse_args(argv)
    logfire.configure(
        send_to_logfire=False,
        console=logfire.ConsoleOptions(
            output=sys.stderr, include_timestamps=False, show_project_link=False
        ),
    )
    try:
        content = load_rehearsal_content(arguments.content)
        if arguments.command == "check":
            print(
                json.dumps(
                    {
                        "status": "ready",
                        "selected_question_ids": [
                            question.id for question in content.selected_questions
                        ],
                        "content_sha256": content_digest(content),
                        "database_accessed": False,
                    }
                )
            )
        else:
            _prepare(arguments, content)
    except (ValidationError, OSError, UnicodeError):
        print("Error: rehearsal_input_invalid", file=sys.stderr)
        return 2
    except (
        AwsDemoDeploymentValidationError,
        RehearsalPreparationRefusedError,
        RehearsalContentError,
    ) as error:
        code = error.code if isinstance(error, AwsDemoDeploymentValidationError) else str(error)
        print(f"Error: {code}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
