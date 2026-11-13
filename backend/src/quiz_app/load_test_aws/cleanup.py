from __future__ import annotations

import os
from datetime import UTC, datetime

import logfire

from quiz_app.load_test_aws.aws import AwsApi, BotoAwsApi, EmptyResponse, require_account
from quiz_app.load_test_aws.inventory import (
    find_stack,
    owned,
    prepare_stack_deletion,
    scan_residuals,
)
from quiz_app.load_test_aws.models import CleanupInput, CleanupResult, Residual
from quiz_app.load_test_aws.ownership import read_ledger
from quiz_app.load_test_aws.responses import Instances
from quiz_app.load_test_aws.storage import scan_active_control, scan_control_storage


def cleanup_pass(
    api: AwsApi, run: CleanupInput, *, delete: bool, now: datetime, include_control: bool = True
) -> CleanupResult:
    """有限の一巡だけを実行し、再試行と待機は独立State Machineに任せる。"""
    require_account(api, run.account_id)
    ledger = read_ledger(api, run)
    stack = find_stack(api, run.workload_stack)
    if stack is not None and not owned(stack.tags, run):
        raise ValueError("aws_load_test_stack_ownership_mismatch")
    if delete and stack is not None and stack.status != "DELETE_IN_PROGRESS":
        # hostのfault timer/runnerをAWS側で停止し、証跡回収を待たず削除へ進む。
        for page in api.pages(
            "ec2",
            "describe_instances",
            Instances,
            Filters=[{"Name": "tag:RunId", "Values": [str(run.run_id)]}],
        ):
            for reservation in page.reservations:
                for instance in reservation.instances:
                    if instance.state.name not in {
                        "terminated",
                        "shutting-down",
                    } and ledger.permits(
                        "AWS::EC2::Instance", instance.identifier, instance.tags, run
                    ):
                        api.call(
                            "ec2",
                            "terminate_instances",
                            EmptyResponse,
                            InstanceIds=[instance.identifier],
                        )
        prepare_stack_deletion(api, run, stack)
        api.call("cloudformation", "delete_stack", EmptyResponse, StackName=stack.stack_id)
    # CloudFormationと同時に依存資源を直接消さない。stackが消えた後の漏れだけを清掃する。
    residuals = list(scan_residuals(api, run, delete=delete and stack is None, ledger=ledger))
    if include_control:
        residuals.extend(scan_control_storage(api, run))
        residuals.extend(scan_active_control(api, run))
    if stack is not None:
        residuals.append(
            Residual(kind="cloudformation-stack", identifier=stack.stack_id, state=stack.status)
        )
    return CleanupResult(
        run_id=run.run_id,
        done=not residuals,
        observed_at=now,
        residuals=tuple(residuals),
        stack_status=stack.status if stack else "ABSENT",
    )


def handler(event: object, _context: object) -> dict[str, object]:
    """AWS入力と環境に束縛したrunだけを消すLambda entrypoint。"""
    logfire.configure(send_to_logfire=False, console=logfire.ConsoleOptions(verbose=False))
    run = CleanupInput.model_validate(event)
    if str(run.run_id) != os.environ.get("LOAD_TEST_RUN_ID") or run.account_id != os.environ.get(
        "LOAD_TEST_ACCOUNT_ID"
    ):
        raise ValueError("aws_load_test_worker_identity_mismatch")
    result = cleanup_pass(
        BotoAwsApi(), run, delete=True, now=datetime.now(UTC), include_control=False
    )
    logfire.info(
        "AWS cleanup completed pass",
        run_id=str(run.run_id),
        remaining=len(result.residuals),
        done=result.done,
    )
    return result.model_dump(mode="json")
