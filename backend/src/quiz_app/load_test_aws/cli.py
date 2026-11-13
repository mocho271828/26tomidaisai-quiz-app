from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import logfire
from botocore.exceptions import BotoCoreError
from pydantic import ValidationError

from quiz_app.aws_demo_paths import (
    RUNTIME_ENVIRONMENT_FILE_POLICY,
    read_validated_external_file,
    validate_external_regular_file,
)
from quiz_app.load_test_aws.aws import AwsCallError, BotoAwsApi
from quiz_app.load_test_aws.cleanup import cleanup_pass
from quiz_app.load_test_aws.lifecycle import preflight, prepare, request_cleanup, start
from quiz_app.load_test_aws.models import AwsPreparationEvidence, AwsRunPlan, SchemaReceipt
from quiz_app.load_test_aws.templates import template_body


def read_file(path: Path, *, maximum_bytes: int = 64 * 1024) -> bytes:
    root = Path(__file__).resolve().parents[4]
    validated = validate_external_regular_file(path, root, RUNTIME_ENVIRONMENT_FILE_POLICY)
    return read_validated_external_file(
        validated, maximum_bytes=maximum_bytes, unavailable_code="aws_load_test_input_unavailable"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="AWS-only disposable load-test control")
    parser.add_argument(
        "action", choices=("plan", "preflight", "prepare", "start", "cleanup", "status")
    )
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--profile")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--worker-package", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--preparation-evidence", type=Path)
    parser.add_argument("--schema-receipt", type=Path)
    args = parser.parse_args()
    logfire.configure(send_to_logfire=False, console=False)
    try:
        plan = AwsRunPlan.model_validate_json(read_file(args.plan))
        now = datetime.now(UTC)
        if args.action == "plan":
            print(
                json.dumps(
                    {
                        "status": "dry_run",
                        "estimated_total_jpy": str(plan.estimated_total_jpy),
                        "expires_at": plan.expires_at.isoformat(),
                        "cleanup_budget_at": plan.cleanup_budget_at.isoformat(),
                        "workload": json.loads(template_body("workload")),
                        "control": json.loads(template_body("control")),
                    }
                )
            )
            return 0
        if args.action in {"prepare", "start", "cleanup"} and not args.execute:
            raise ValueError("aws_load_test_execute_flag_required")
        manifest_bytes = b""
        if args.action in {"preflight", "prepare", "start"}:
            if args.manifest is None:
                raise ValueError("aws_load_test_manifest_required")
            manifest_bytes = read_file(args.manifest, maximum_bytes=1_000_000)
        api = BotoAwsApi(profile=args.profile)
        if args.action == "preflight":
            pricing = preflight(api, plan, now=now, manifest_bytes=manifest_bytes)
            print(
                json.dumps(
                    {
                        "status": "identity_and_creation_window_verified",
                        "pricing": pricing.model_dump(mode="json"),
                    }
                )
            )
        elif args.action == "prepare":
            if args.worker_package is None:
                raise ValueError("aws_load_test_worker_package_required")
            result = prepare(
                api,
                plan,
                read_file(args.worker_package, maximum_bytes=50 * 1024 * 1024),
                now=now,
                manifest_bytes=manifest_bytes,
            )
            print(result.model_dump_json())
        elif args.action == "start":
            if args.preparation_evidence is None or args.schema_receipt is None:
                raise ValueError("aws_load_test_human_schema_and_preparation_required")
            result = start(
                api,
                plan,
                AwsPreparationEvidence.model_validate_json(read_file(args.preparation_evidence)),
                SchemaReceipt.model_validate_json(read_file(args.schema_receipt)),
                now=now,
                manifest_bytes=manifest_bytes,
            )
            print(result.model_dump_json())
        elif args.action == "cleanup":
            print(request_cleanup(api, plan.cleanup_input()).model_dump_json())
        else:
            print(cleanup_pass(api, plan.cleanup_input(), delete=False, now=now).model_dump_json())
    except (AwsCallError, BotoCoreError, OSError, ValueError, ValidationError) as error:
        # 外部設定値・認証値を含み得るValidationError本文をCLIへ流さない。
        code = type(error).__name__
        if isinstance(error, AwsCallError) or re.fullmatch(r"aws_load_test_[a-z0-9_]+", str(error)):
            code = str(error)
        print(json.dumps({"status": "refused", "code": code}))
        return 1
    return 0
