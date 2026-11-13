from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Never, TextIO, cast

from pydantic import SecretStr, ValidationError

from quiz_app.load_test.controller import (
    ControllerAction,
    ControllerRefusal,
    LoadTestController,
)
from quiz_app.load_test.controller_contracts import canonical_json_bytes
from quiz_app.load_test.controller_http import (
    ControllerHttpClient,
    ControllerHttpError,
)
from quiz_app.load_test.controller_journal import ControllerJournal
from quiz_app.load_test.controller_verification import (
    FileVerificationProvider,
    VerificationHandoffError,
)
from quiz_app.load_test.manifest import load_manifest


class ControllerArgumentError(ValueError):
    """入力値を保持せずCLI契約違反だけを表す。"""


class RedactingArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> Never:
        raise ControllerArgumentError


def _build_parser() -> RedactingArgumentParser:
    parser = RedactingArgumentParser(description="負荷耐久試験の固定10 Round controller")
    parser.add_argument(
        "action", choices=("prepare", "schedule", "publish", "wait-results", "advance")
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--repository-root", required=True, type=Path)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--checkpoint-directory", required=True, type=Path)
    parser.add_argument("--verification-request-directory", required=True, type=Path)
    parser.add_argument("--username", required=True)
    parser.add_argument("--generator-evidence", type=Path)
    parser.add_argument("--ca-file", required=True, type=Path)
    parser.add_argument("--receipt", type=Path)
    return parser


def _read_confirmation(expected: str, input_stream: TextIO) -> str:
    if not input_stream.isatty():
        raise ControllerRefusal("controller_confirmation_requires_tty")
    print(f"Type exactly: {expected}", file=sys.stderr)
    supplied = input_stream.readline(512)
    if not supplied.endswith(("\n", "\r")):
        raise ControllerRefusal("controller_confirmation_mismatch")
    return supplied.rstrip("\r\n")


def _require_cli_artifact_isolation(arguments: argparse.Namespace) -> None:
    paths = (
        arguments.manifest.parent,
        arguments.ca_file.parent,
        arguments.journal.parent,
        arguments.checkpoint_directory,
        arguments.verification_request_directory,
        *(
            path.parent
            for path in (
                arguments.generator_evidence,
                arguments.receipt,
            )
            if path is not None
        ),
    )
    try:
        resolved = tuple(path.resolve(strict=True) for path in paths)
    except OSError as error:
        raise ControllerArgumentError from error
    for index, left in enumerate(resolved):
        for right in resolved[index + 1 :]:
            if left == right or left.is_relative_to(right) or right.is_relative_to(left):
                raise ControllerArgumentError


def _execute(
    arguments: argparse.Namespace,
    input_stream: TextIO,
    password_reader: Callable[[], str],
) -> int:
    _require_cli_artifact_isolation(arguments)
    manifest = load_manifest(arguments.manifest, arguments.repository_root)
    password_holder: list[SecretStr] = []

    def reauthenticate(client: ControllerHttpClient) -> None:
        if not password_holder:
            raise ControllerHttpError("controller_http_reauthentication_unavailable")
        client.login(arguments.username.strip(), password_holder[0])

    api = ControllerHttpClient(manifest, reauthenticate=reauthenticate, ca_file=arguments.ca_file)
    verification = FileVerificationProvider()
    action = cast(ControllerAction, arguments.action)
    with ControllerJournal(arguments.journal, arguments.repository_root) as journal:
        controller = LoadTestController(
            manifest,
            journal,
            arguments.checkpoint_directory,
            arguments.verification_request_directory,
            arguments.repository_root,
            api,
            verification,
        )
        expected = controller.confirmation_prompt(action, receipt_path=arguments.receipt)
        confirmation = _read_confirmation(expected, input_stream)
        password = SecretStr(password_reader())
        password_holder.append(password)
        try:
            status = controller.execute(
                action,
                username=arguments.username,
                password=password,
                confirmation_phrase=confirmation,
                generator_evidence_path=arguments.generator_evidence,
                receipt_path=arguments.receipt,
            )
        finally:
            password_holder.clear()
    print(canonical_json_bytes(status).decode("ascii"))
    return os.EX_OK


def main(
    argv: list[str] | None = None,
    *,
    input_stream: TextIO | None = None,
    password_reader: Callable[[], str] | None = None,
) -> int:
    """成功時だけsecret-free statusを返し、失敗時は固定codeへ縮退する。"""

    try:
        arguments = _build_parser().parse_args(argv)
        return _execute(
            arguments,
            input_stream if input_stream is not None else sys.stdin,
            password_reader if password_reader is not None else getpass.getpass,
        )
    except ControllerRefusal as error:
        print(f'{{"status":"refused","code":"{error}"}}', file=sys.stderr)
        return os.EX_CONFIG
    except (ControllerHttpError, VerificationHandoffError):
        print('{"status":"failed","code":"dependency_error"}', file=sys.stderr)
        return os.EX_UNAVAILABLE
    except (ControllerArgumentError, ValidationError, ValueError):
        print('{"status":"refused","code":"invalid_contract"}', file=sys.stderr)
        return os.EX_CONFIG
    except (EOFError, OSError):
        print('{"status":"failed","code":"local_io_error"}', file=sys.stderr)
        return os.EX_IOERR


if __name__ == "__main__":
    raise SystemExit(main())
