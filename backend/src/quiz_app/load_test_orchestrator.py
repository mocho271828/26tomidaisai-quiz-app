from __future__ import annotations

import argparse
import signal
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Never

import logfire
from botocore.exceptions import BotoCoreError
from pydantic import ValidationError

from quiz_app.load_test.collection import collection_deadline, publish_and_wait
from quiz_app.load_test.controller_contracts import ControllerRefusal
from quiz_app.load_test.controller_http import ControllerHttpError
from quiz_app.load_test.db_verifier_artifacts import (
    read_secure_external_file,
    runtime_repository_root,
)
from quiz_app.load_test.db_verifier_database import DbVerifierDatabaseError
from quiz_app.load_test.orchestrator import LoadTestOrchestrator
from quiz_app.load_test.orchestrator_contracts import (
    RunFiles,
    RunResult,
    cleanup_input,
    load_runner_configuration,
    log_failure,
)
from quiz_app.load_test.orchestrator_process import RunClock, RunInterrupted
from quiz_app.load_test_aws.aws import AwsCallError, BotoAwsApi
from quiz_app.load_test_aws.lifecycle import request_cleanup
from quiz_app.load_test_aws.models import AwsPreparationEvidence


class RedactingParser(argparse.ArgumentParser):
    def error(self, _message: str) -> Never:
        raise ValueError("orchestrator_arguments_invalid")


def main(argv: list[str] | None = None) -> int:
    """SSMの固定入口。成功・失敗・signalで同じAWS撤去を要求する。"""
    logfire.configure(send_to_logfire=False, console=False)
    parser = RedactingParser(description="固定80分のAWS負荷試験を自動実行する")
    parser.add_argument("--run-directory", type=Path, required=True)
    runner: LoadTestOrchestrator | None = None
    prepared: AwsPreparationEvidence | None = None
    result: RunResult | None = None
    aws = BotoAwsApi()
    code = 1
    previous_handlers = {}
    try:
        arguments = parser.parse_args(argv)
        files = RunFiles(arguments.run_directory, runtime_repository_root())
        # 後続の検証が失敗しても、既に読み取れたrunは必ず撤去する。
        raw, _ = read_secure_external_file(files.input("preparation.json"))
        prepared = AwsPreparationEvidence.model_validate_json(raw)
        clock = RunClock(prepared.expires_at - timedelta(seconds=30))
        for number in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[number] = signal.signal(number, clock.signal)
        manifest, bound = files.load_binding(now=datetime.now(UTC))
        if prepared != bound:
            raise ValueError("orchestrator_preparation_changed")
        config = load_runner_configuration(files, prepared, manifest)
        runner = LoadTestOrchestrator(files, manifest, prepared, config, aws, clock)
        result = runner.run()
        code = 0
    except RunInterrupted:
        if runner is not None:
            result = runner.result("INTERRUPTED")
        print('{"status":"interrupted"}', file=sys.stderr)
        code = 130
    except (
        ValueError,
        RuntimeError,
        OSError,
        BotoCoreError,
        ControllerRefusal,
        ControllerHttpError,
        DbVerifierDatabaseError,
        ValidationError,
    ) as error:
        # 下位のHTTP/DB/AWS adapterで詳細を捨て、秘密を含む入力は記録しない。
        log_failure("orchestrator_failed", error)
        if runner is not None:
            result = runner.result("FAIL")
        print('{"status":"failed","code":"orchestrator_failed"}', file=sys.stderr)
    finally:
        try:
            if runner is not None:
                runner.clock.stop.set()
                runner.generator.close()
                if result is not None:
                    collection_clock = RunClock(
                        collection_deadline(runner.manifest, runner.prepared, result.finished_at)
                    )
                    # 最初のsignal後も証跡は回収できる。次のsignalは回収待機を止める。
                    for number in (signal.SIGINT, signal.SIGTERM):
                        signal.signal(number, collection_clock.signal)
                    publish_and_wait(
                        aws, runner.files, runner.manifest, runner.prepared, collection_clock
                    )
        except (ValueError, RuntimeError, OSError, BotoCoreError) as error:
            log_failure("collection_failed_cleanup_required", error)
            print('{"status":"failed","code":"collection_failed"}', file=sys.stderr)
            code = 1
        finally:
            if prepared is not None:
                try:
                    execution = request_cleanup(aws, cleanup_input(prepared))
                    print('{"cleanup_execution":' + '"' + execution.arn + '"}')
                except (AwsCallError, BotoCoreError, ValueError) as error:
                    log_failure("cleanup_request_failed_deadline_remains_armed", error)
                    print('{"status":"failed","code":"cleanup_request_failed"}', file=sys.stderr)
                    code = 1
            for number, handler in previous_handlers.items():
                signal.signal(number, handler)
    if code == 0 and result is not None:
        print(result.model_dump_json())
    return code


if __name__ == "__main__":
    raise SystemExit(main())
