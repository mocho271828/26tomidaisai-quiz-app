from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Never

import logfire
from pydantic import ValidationError

from quiz_app.load_test.db_verifier import (
    DbVerifierError,
    load_db_verifier_request,
    run_db_verifier,
)
from quiz_app.load_test.db_verifier_contracts import LockObservationInput
from quiz_app.load_test.db_verifier_database import (
    DbVerifierDatabaseError,
    VerifierDatabase,
    create_verifier_engine,
    load_verifier_database_settings,
)
from quiz_app.load_test.manifest import load_manifest


class DbVerifierArgumentError(ValueError):
    """CLI入力値を保持せず、固定の契約違反だけを表す。"""


class RedactingArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> Never:
        raise DbVerifierArgumentError


def _build_parser() -> RedactingArgumentParser:
    parser = RedactingArgumentParser(description="SELECT-only負荷試験DB verifier")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--repository-root", required=True, type=Path)
    parser.add_argument("--database-url-file", required=True, type=Path)
    parser.add_argument("--ca-file", required=True, type=Path)
    parser.add_argument("--ca-sha256", required=True)
    parser.add_argument("--output-directory", required=True, type=Path)
    parser.add_argument("--generator-evidence", type=Path)
    return parser


def _require_artifact_isolation(arguments: argparse.Namespace) -> None:
    required_files = (
        arguments.input,
        arguments.manifest,
        arguments.database_url_file,
        arguments.ca_file,
    )
    optional_files = (arguments.generator_evidence,)
    files = required_files + tuple(path for path in optional_files if path is not None)
    try:
        resolved_files = tuple(path.resolve(strict=True) for path in files)
        output = arguments.output_directory.resolve(strict=True)
    except OSError:
        raise DbVerifierArgumentError from None
    if len(set(resolved_files)) != len(resolved_files):
        raise DbVerifierArgumentError
    for path in resolved_files:
        parent = path.parent
        if output == parent or output.is_relative_to(parent) or parent.is_relative_to(output):
            raise DbVerifierArgumentError


def _execute(arguments: argparse.Namespace) -> int:
    _require_artifact_isolation(arguments)
    manifest = load_manifest(arguments.manifest, arguments.repository_root)
    loaded = load_db_verifier_request(arguments.input)
    evidence_paths = (arguments.generator_evidence,)
    if isinstance(loaded.value, LockObservationInput):
        if evidence_paths != (None,):
            raise DbVerifierArgumentError
    elif None in evidence_paths:
        raise DbVerifierArgumentError
    settings = load_verifier_database_settings(
        arguments.database_url_file,
        arguments.ca_file,
        arguments.ca_sha256,
        arguments.repository_root,
    )
    engine = create_verifier_engine(settings)
    database = VerifierDatabase(engine, settings)
    try:
        result = run_db_verifier(
            manifest,
            loaded,
            database,
            arguments.output_directory,
            generator_evidence_path=arguments.generator_evidence,
        )
    finally:
        database.dispose()
    print(result.model_dump_json())
    return os.EX_OK if result.decision == "PASS" else os.EX_DATAERR


def main(argv: list[str] | None = None) -> int:
    """credentialやDB例外を表示せず、固定statusだけをstderrへ返す。"""

    logfire.configure(send_to_logfire=False, console=False)
    try:
        arguments = _build_parser().parse_args(argv)
        return _execute(arguments)
    except (DbVerifierArgumentError, DbVerifierError, ValidationError, ValueError):
        print('{"status":"refused","code":"invalid_contract"}', file=sys.stderr)
        return os.EX_CONFIG
    except DbVerifierDatabaseError:
        print('{"status":"failed","code":"database_dependency_error"}', file=sys.stderr)
        return os.EX_UNAVAILABLE
    except OSError:
        print('{"status":"failed","code":"local_io_error"}', file=sys.stderr)
        return os.EX_IOERR


if __name__ == "__main__":
    raise SystemExit(main())
