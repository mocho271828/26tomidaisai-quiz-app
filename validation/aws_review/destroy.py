from __future__ import annotations

import time

import logfire

from quiz_app.load_test_aws.aws import AwsApi, AwsCallError, EmptyResponse, require_account
from quiz_app.load_test_aws.responses import Instances, Stack, Stacks
from quiz_app.load_test_aws.storage import BucketTags, empty_bucket
from validation.aws_review.destroy_inventory import DestroyLedger, owned, read_ledger, require_stack
from validation.aws_review.destroy_residue import remove_storage_residue
from validation.aws_review.destroy_state import preserve_ledger
from validation.aws_review.destroy_verify import require_no_untracked_resources, verify_resources
from validation.aws_review.diagnostics import progress
from validation.aws_review.models import ReviewTarget
from validation.aws_review.resource_progress import StackProgress

_SUPPORTED_KINDS = frozenset(
    {
        "AWS::CloudFront::Distribution",
        "AWS::CloudFront::VpcOrigin",
        "AWS::CloudWatch::Dashboard",
        "AWS::EC2::Instance",
        "AWS::EC2::Volume",
        "AWS::EC2::EIP",
        "AWS::EC2::NatGateway",
        "AWS::EC2::VPC",
        "AWS::EC2::InternetGateway",
        "AWS::EC2::VPCGatewayAttachment",
        "AWS::EC2::Subnet",
        "AWS::EC2::RouteTable",
        "AWS::EC2::SubnetRouteTableAssociation",
        "AWS::EC2::Route",
        "AWS::EC2::SecurityGroup",
        "AWS::EC2::SecurityGroupIngress",
        "AWS::EC2::SecurityGroupEgress",
        "AWS::RDS::DBInstance",
        "AWS::RDS::DBSubnetGroup",
        "RdsResourceId",
        "AWS::ElastiCache::ReplicationGroup",
        "AWS::ElastiCache::SubnetGroup",
        "AWS::SecretsManager::Secret",
        "AWS::ECR::Repository",
        "AWS::Logs::LogGroup",
        "AWS::IAM::Role",
        "AWS::IAM::InstanceProfile",
        "AWS::S3::Bucket",
        "AWS::S3::BucketPolicy",
        "AWS::StepFunctions::StateMachine",
        "AWS::Scheduler::Schedule",
    }
)


def _pause(deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("aws_review_destroy_incomplete")
    time.sleep(min(30, remaining))


def _delete_stack(api: AwsApi, target: ReviewTarget, stack: Stack, deadline: float) -> None:
    require_stack(stack, target)
    if stack.status == "DELETE_COMPLETE":
        return
    kind = next(
        kind for kind in ("edge", "workload", "control") if stack.name == f"{target.prefix}-{kind}"
    )
    reporter = StackProgress(kind, "destroy_progress")
    reporter.report("WAITING")
    if stack.status != "DELETE_IN_PROGRESS":
        api.call("cloudformation", "delete_stack", EmptyResponse, StackName=stack.stack_id)
    while True:
        # 削除済みARNの履歴を照会する。ValidationError全般を不存在と解釈しない。
        response = api.call("cloudformation", "describe_stacks", Stacks, StackName=stack.stack_id)
        if len(response.stacks) != 1 or response.stacks[0].stack_id != stack.stack_id:
            raise ValueError("aws_review_destroy_stack_identity_mismatch")
        observed = response.stacks[0]
        require_stack(observed, target)
        reporter.report(observed.status)
        if observed.status == "DELETE_COMPLETE":
            return
        if observed.status == "DELETE_FAILED":
            raise ValueError("aws_review_destroy_stack_failed")
        if observed.status != "DELETE_IN_PROGRESS":
            raise ValueError("aws_review_destroy_stack_unexpected_state")
        reporter.resources(api, observed.stack_id)
        _pause(deadline)


def _stop_instances(api: AwsApi, target: ReviewTarget, ledger: DestroyLedger) -> None:
    identifiers = ledger.identifiers("AWS::EC2::Instance")
    if not identifiers:
        return
    for page in api.pages(
        "ec2",
        "describe_instances",
        Instances,
        Filters=[{"Name": "instance-id", "Values": sorted(identifiers)}],
    ):
        for reservation in page.reservations:
            for instance in reservation.instances:
                if instance.state.name in {"terminated", "shutting-down", "stopped", "stopping"}:
                    continue
                if instance.identifier not in identifiers or not owned(instance.tags, target):
                    raise ValueError("aws_review_destroy_instance_ownership_mismatch")
                if instance.state.name == "pending":
                    # 起動途中はStopInstances不可。専用EC2だけ終了し、CFNに残りを回収させる。
                    api.call(
                        "ec2",
                        "terminate_instances",
                        EmptyResponse,
                        InstanceIds=[instance.identifier],
                    )
                else:
                    api.call(
                        "ec2", "stop_instances", EmptyResponse, InstanceIds=[instance.identifier]
                    )


def _empty_buckets(api: AwsApi, target: ReviewTarget, ledger: DestroyLedger) -> None:
    for bucket in ledger.identifiers("AWS::S3::Bucket"):
        try:
            tags = api.call(
                "s3",
                "get_bucket_tagging",
                BucketTags,
                Bucket=bucket,
                ExpectedBucketOwner=target.account_id,
            )
            if not owned(tags.tags, target):
                raise ValueError("aws_review_destroy_bucket_ownership_mismatch")
            empty_bucket(api, bucket)
        except AwsCallError as error:
            if error.code != "NoSuchBucket":
                raise


def _verify_until_absent(
    api: AwsApi,
    target: ReviewTarget,
    ledger: DestroyLedger,
    deadline: float,
    *,
    include_control: bool,
) -> None:
    progress("destroy_verify")
    while True:
        residuals = (
            *remove_storage_residue(api, target, ledger),
            *verify_resources(api, target, ledger, include_control=include_control),
        )
        logfire.info(
            "AWS review cleanup verification", residual_count=len(residuals), residuals=residuals
        )
        if not residuals:
            return
        _pause(deadline)


def destroy(api: AwsApi, target: ReviewTarget, *, timeout_seconds: int = 7200) -> None:
    """専用環境のDB・回答・backupを含めて削除し、サービス別の不存在まで確認する。"""
    if timeout_seconds <= 0:
        raise ValueError("aws_review_destroy_timeout_invalid")
    progress("destroy_start")
    deadline = time.monotonic() + timeout_seconds
    require_account(api, target.account_id)
    ledger = read_ledger(api, target)
    if any(resource.kind not in _SUPPORTED_KINDS for resource in ledger.resources):
        raise ValueError("aws_review_destroy_unsupported_resource")
    if ledger.identifiers("AWS::CloudWatch::Dashboard") - {f"{target.prefix}-health"}:
        raise ValueError("aws_review_destroy_dashboard_identity_mismatch")
    ledger = preserve_ledger(api, target, ledger)
    logfire.info("AWS review cleanup started", stack_count=len(ledger.stacks))
    _stop_instances(api, target, ledger)
    for kind in ("edge", "workload"):
        for stack in ledger.stacks:
            if stack.name == f"{target.prefix}-{kind}":
                _delete_stack(api, target, stack, deadline)
    _verify_until_absent(api, target, ledger, deadline, include_control=False)
    _empty_buckets(api, target, ledger)
    for stack in ledger.stacks:
        if stack.name == f"{target.prefix}-control":
            _delete_stack(api, target, stack, deadline)
    _verify_until_absent(api, target, ledger, deadline, include_control=True)
    require_no_untracked_resources(api, target, ledger)
    logfire.info("AWS review cleanup completed", stack_count=len(ledger.stacks))
    progress("destroy_complete")
