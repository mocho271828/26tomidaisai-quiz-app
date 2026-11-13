from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Never, TextIO
from uuid import UUID

from pydantic import ValidationError
from pydantic_settings import SettingsError
from sqlalchemy.exc import SQLAlchemyError

from quiz_app.config import Settings
from quiz_app.load_test.bootstrap import (
    LoadTestBootstrapRefusedError,
    bootstrap_load_test_event,
)
from quiz_app.load_test.manifest import (
    build_load_test_manifest,
    load_manifest,
    write_manifest,
)


class LoadTestArgumentError(ValueError):
    """入力値やargparse messageを保持せず、CLI契約違反だけを表す。"""


class RedactingArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> Never:
        """usageと入力値を出力せず、mainの固定JSON errorへ委譲する。"""

        raise LoadTestArgumentError


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("UUIDを指定してください") from error


def _aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("timezone付きISO 8601日時を指定してください") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timezone付きISO 8601日時を指定してください")
    return parsed


def _strict_bool(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise argparse.ArgumentTypeError("trueまたはfalseを指定してください")


def _build_parser() -> RedactingArgumentParser:
    parser = RedactingArgumentParser(description="負荷耐久試験Eventのmanifest作成とbootstrap")
    commands = parser.add_subparsers(dest="command", required=True)

    init_parser = commands.add_parser("init", help="strict manifestを新規作成する")
    init_parser.add_argument("--run-id", required=True, type=_uuid)
    init_parser.add_argument("--event-id", required=True, type=_uuid)
    init_parser.add_argument("--event-slug", required=True)
    init_parser.add_argument("--scheduled-start-at", required=True, type=_aware_datetime)
    init_parser.add_argument("--public-origin", required=True)
    init_parser.add_argument("--source-commit", required=True)
    init_parser.add_argument("--api-image", required=True)
    init_parser.add_argument("--gateway-image", required=True)
    init_parser.add_argument("--generator-image", required=True)
    init_parser.add_argument("--scoring-policy-version", required=True)
    init_parser.add_argument("--scoring-base-points", required=True, type=int)
    init_parser.add_argument("--scoring-initial-speed-bonus", required=True, type=int)
    init_parser.add_argument("--scoring-speed-bucket-ms", required=True, type=int)
    init_parser.add_argument("--scoring-speed-decrement-per-bucket", required=True, type=int)
    init_parser.add_argument("--scoring-standard-duration-ms", required=True, type=int)
    init_parser.add_argument("--scoring-is-approved", required=True, type=_strict_bool)
    init_parser.add_argument("--display-name-policy-version", required=True)
    init_parser.add_argument("--display-notice-version", required=True)
    init_parser.add_argument("--display-max-code-points", required=True, type=int)
    init_parser.add_argument("--display-reject-patterns-json", required=True)
    init_parser.add_argument("--output", required=True, type=Path)
    init_parser.add_argument("--repository-root", required=True, type=Path)

    bootstrap_parser = commands.add_parser("bootstrap", help="fresh EventをINSERTする")
    bootstrap_parser.add_argument("--manifest", required=True, type=Path)
    bootstrap_parser.add_argument("--repository-root", required=True, type=Path)
    return parser


def _init_manifest(arguments: argparse.Namespace) -> int:
    manifest = build_load_test_manifest(
        run_id=arguments.run_id,
        event_id=arguments.event_id,
        event_slug=arguments.event_slug,
        scheduled_start_at=arguments.scheduled_start_at,
        public_origin=arguments.public_origin,
        source_commit=arguments.source_commit,
        api_image=arguments.api_image,
        gateway_image=arguments.gateway_image,
        generator_image=arguments.generator_image,
        scoring_policy_version=arguments.scoring_policy_version,
        scoring_base_points=arguments.scoring_base_points,
        scoring_initial_speed_bonus=arguments.scoring_initial_speed_bonus,
        scoring_speed_bucket_ms=arguments.scoring_speed_bucket_ms,
        scoring_speed_decrement_per_bucket=(arguments.scoring_speed_decrement_per_bucket),
        scoring_standard_duration_ms=arguments.scoring_standard_duration_ms,
        scoring_is_approved=arguments.scoring_is_approved,
        display_name_policy_version=arguments.display_name_policy_version,
        display_notice_version=arguments.display_notice_version,
        display_max_code_points=arguments.display_max_code_points,
        display_reject_patterns_json=arguments.display_reject_patterns_json,
        created_at=datetime.now(UTC),
    )
    write_manifest(arguments.output, manifest, arguments.repository_root)
    print(manifest.model_dump_json())
    return os.EX_OK


def _require_operator_confirmation(
    manifest_run_id: UUID,
    event_slug: str,
    input_stream: TextIO,
) -> None:
    if not input_stream.isatty():
        raise LoadTestBootstrapRefusedError("load_test_bootstrap_confirmation_requires_tty")
    expected_phrase = f"CONFIRM {manifest_run_id} BOOTSTRAP {event_slug}"
    print(f"Type exactly: {expected_phrase}", file=sys.stderr)
    supplied_phrase = input_stream.readline(256).rstrip("\r\n")
    if supplied_phrase != expected_phrase:
        raise LoadTestBootstrapRefusedError("load_test_bootstrap_confirmation_mismatch")


def _bootstrap(arguments: argparse.Namespace, input_stream: TextIO) -> int:
    manifest = load_manifest(arguments.manifest, arguments.repository_root)
    _require_operator_confirmation(manifest.run_id, manifest.event.slug, input_stream)
    settings = Settings(_env_file=None)
    result = bootstrap_load_test_event(settings, manifest)
    print(result.model_dump_json())
    return os.EX_OK


def _run_cli(argv: list[str] | None = None, *, input_stream: TextIO | None = None) -> int:
    """成功時だけ非秘密JSONを返し、拒否理由は固定codeで返す。"""

    try:
        arguments = _build_parser().parse_args(argv)
        if arguments.command == "init":
            return _init_manifest(arguments)
        return _bootstrap(arguments, input_stream if input_stream is not None else sys.stdin)
    except (ValidationError, SettingsError, ValueError):
        print('{"status":"refused","code":"invalid_contract"}', file=sys.stderr)
        return os.EX_CONFIG
    except FileExistsError:
        print('{"status":"refused","code":"manifest_exists"}', file=sys.stderr)
        return os.EX_CANTCREAT
    except (OSError, SQLAlchemyError):
        print('{"status":"failed","code":"dependency_error"}', file=sys.stderr)
        return os.EX_UNAVAILABLE
    except LoadTestBootstrapRefusedError as error:
        print(f'{{"status":"refused","code":"{error}"}}', file=sys.stderr)
        return os.EX_CONFIG


def main(argv: list[str] | None = None, *, input_stream: TextIO | None = None) -> int:
    """v3契約を検証し、manifest作成または明示bootstrapを実行する。"""

    return _run_cli(argv, input_stream=input_stream)


if __name__ == "__main__":
    raise SystemExit(main())
