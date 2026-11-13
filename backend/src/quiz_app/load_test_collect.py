from __future__ import annotations

import argparse
import json
import signal
import sys
from datetime import timedelta
from pathlib import Path
from typing import Never

import logfire
from botocore.exceptions import BotoCoreError

from quiz_app.load_test.collection import collect_locally
from quiz_app.load_test.db_verifier_artifacts import (
    read_secure_external_file,
    runtime_repository_root,
)
from quiz_app.load_test.manifest import load_manifest
from quiz_app.load_test.orchestrator_contracts import log_failure
from quiz_app.load_test.orchestrator_process import RunClock, RunInterrupted
from quiz_app.load_test_aws.aws import BotoAwsApi
from quiz_app.load_test_aws.models import AwsPreparationEvidence


class RedactingParser(argparse.ArgumentParser):
    def error(self, _message: str) -> Never:
        raise ValueError("collection_arguments_invalid")


def main(argv: list[str] | None = None) -> int:
    """ローカル回収の固定入口。全件検証できなければACKもPASSも返さない。"""
    logfire.configure(send_to_logfire=False, console=False)
    parser = RedactingParser(description="AWS負荷試験の証跡を回収し、撤去待機へACKする")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--preparation-evidence", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--profile")
    handlers = {}
    try:
        args = parser.parse_args(argv)
        repository = runtime_repository_root()
        manifest = load_manifest(args.manifest, repository)
        prepared = AwsPreparationEvidence.model_validate_json(
            read_secure_external_file(args.preparation_evidence)[0]
        )
        clock = RunClock(
            min(
                manifest.scheduled_start_at + timedelta(seconds=5400),
                prepared.expires_at - timedelta(seconds=30),
            )
        )
        for number in (signal.SIGINT, signal.SIGTERM):
            handlers[number] = signal.signal(number, clock.signal)
        result = collect_locally(
            BotoAwsApi(profile=args.profile),
            manifest,
            prepared,
            args.output_directory,
            repository,
            clock,
        )
        print(
            json.dumps(
                {
                    "status": "collected",
                    "test_decision": result.decision,
                    "run_id": str(result.run_id),
                    "cleanup_status": "not_verified",
                },
                separators=(",", ":"),
            )
        )
        return 0 if result.decision == "PASS" else 1
    except RunInterrupted:
        print('{"status":"interrupted","code":"collection_interrupted"}', file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError, BotoCoreError) as error:
        log_failure("collection_failed", error)
        print('{"status":"failed","code":"collection_failed"}', file=sys.stderr)
        return 1
    finally:
        for number, handler in handlers.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(main())
