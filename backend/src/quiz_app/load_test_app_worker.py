from __future__ import annotations

import signal
import sys
from datetime import timedelta
from pathlib import Path

import logfire
from botocore.exceptions import BotoCoreError

from quiz_app.load_test.db_verifier_artifacts import (
    read_secure_external_file,
    runtime_repository_root,
)
from quiz_app.load_test.orchestrator_app import run_app_worker
from quiz_app.load_test.orchestrator_contracts import RunFiles, cleanup_input, log_failure
from quiz_app.load_test.orchestrator_process import RunClock
from quiz_app.load_test_aws.aws import BotoAwsApi
from quiz_app.load_test_aws.lifecycle import request_cleanup
from quiz_app.load_test_aws.models import AwsPreparationEvidence
from quiz_app.load_test_orchestrator import RedactingParser


def main(argv: list[str] | None = None) -> int:
    logfire.configure(send_to_logfire=False, console=False)
    parser = RedactingParser(description="アプリhostの固定障害操作とメトリクス回収")
    parser.add_argument("--run-directory", required=True, type=Path)
    parser.add_argument("--action", choices=("faults", "export"), required=True)
    prepared = None
    aws = BotoAwsApi()
    previous_handlers = {}
    try:
        arguments = parser.parse_args(argv)
        files = RunFiles(arguments.run_directory, runtime_repository_root())
        raw, _ = read_secure_external_file(files.input("preparation.json"))
        prepared = AwsPreparationEvidence.model_validate_json(raw)
        clock = RunClock(prepared.expires_at - timedelta(seconds=30))
        for number in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[number] = signal.signal(number, clock.signal)
        run_app_worker(files, arguments.action, aws, clock)
        print('{"status":"completed"}')
        return 0
    except (ValueError, RuntimeError, OSError, BotoCoreError) as error:
        log_failure("app_worker_failed", error)
        if prepared is not None:
            request_cleanup(aws, cleanup_input(prepared))
        print('{"status":"failed","code":"app_worker_failed"}', file=sys.stderr)
        return 1
    finally:
        for number, handler in previous_handlers.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(main())
