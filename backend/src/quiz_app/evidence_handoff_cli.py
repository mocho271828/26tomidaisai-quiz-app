from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Never

from pydantic import ValidationError

from quiz_app.load_test.controller_contracts import canonical_json_bytes
from quiz_app.load_test.evidence_handoff import (
    EvidenceHandoffError,
    load_controller_status,
    seal_db_verifier_input,
    seal_lock_observation_input,
)
from quiz_app.load_test.manifest import load_manifest


class EvidenceHandoffArgumentError(ValueError):
    """argv の値を保持せず CLI 契約違反だけを表す。"""


class RedactingArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> Never:
        raise EvidenceHandoffArgumentError


def _build_parser() -> RedactingArgumentParser:
    parser = RedactingArgumentParser(description="負荷試験 evidence handoff artifact sealer")
    commands = parser.add_subparsers(dest="command", required=True)

    database = commands.add_parser("build-db-input")
    _add_manifest_and_output(database)
    database.add_argument("--controller-status", required=True, type=Path)
    database.add_argument("--verification-request-directory", required=True, type=Path)
    database.add_argument("--mode", required=True, choices=("per-round", "final"))

    locks = commands.add_parser("build-lock-input")
    _add_manifest_and_output(locks)
    locks.add_argument("--round-number", required=True, type=int)
    return parser


def _add_manifest_and_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--repository-root", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)


def _execute(arguments: argparse.Namespace) -> int:
    _require_artifact_directory_isolation(arguments)
    manifest = load_manifest(arguments.manifest, arguments.repository_root)
    if arguments.command == "build-db-input":
        status = load_controller_status(arguments.controller_status)
        artifact = seal_db_verifier_input(
            manifest,
            status,
            arguments.verification_request_directory,
            arguments.output_directory,
            mode=arguments.mode,
        )
    else:
        artifact = seal_lock_observation_input(
            manifest,
            arguments.output_directory,
            round_number=arguments.round_number,
        )
    print(canonical_json_bytes(artifact).decode("ascii"))
    return os.EX_OK


def _require_artifact_directory_isolation(arguments: argparse.Namespace) -> None:
    directories = [
        arguments.repository_root,
        arguments.manifest.parent,
        arguments.output_directory,
    ]
    if hasattr(arguments, "controller_status"):
        directories.append(arguments.controller_status.parent)
    if hasattr(arguments, "verification_request_directory"):
        directories.append(arguments.verification_request_directory)
    try:
        resolved = tuple(directory.resolve(strict=True) for directory in directories)
    except OSError:
        raise EvidenceHandoffArgumentError from None
    for index, left in enumerate(resolved):
        for right in resolved[index + 1 :]:
            if left == right or left.is_relative_to(right) or right.is_relative_to(left):
                raise EvidenceHandoffArgumentError


def main(argv: list[str] | None = None) -> int:
    """成功時は identity のみ、失敗時は固定 JSON code のみを返す。"""

    try:
        arguments = _build_parser().parse_args(argv)
        return _execute(arguments)
    except (EvidenceHandoffArgumentError, EvidenceHandoffError, ValidationError, ValueError):
        print('{"status":"refused","code":"invalid_contract"}', file=sys.stderr)
        return os.EX_CONFIG
    except OSError:
        print('{"status":"failed","code":"local_io_error"}', file=sys.stderr)
        return os.EX_IOERR


if __name__ == "__main__":
    raise SystemExit(main())
