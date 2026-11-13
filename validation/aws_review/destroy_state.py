from __future__ import annotations

from typing import Literal

from botocore.response import StreamingBody
from pydantic import BaseModel, ConfigDict, Field, field_validator

from quiz_app.load_test_aws.aws import ApiModel, AwsApi, AwsCallError, EmptyResponse
from quiz_app.load_test_aws.storage import BucketTags
from validation.aws_review.destroy_inventory import DestroyLedger, owned, require_stack
from validation.aws_review.models import ReviewTarget

STATE_KEY = "review-destroy-ledger.json"
_MAX_STATE_BYTES = 1_048_576


class SavedState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    schema_version: Literal["aws-review-destroy-v1"] = "aws-review-destroy-v1"
    target: ReviewTarget
    ledger: DestroyLedger


class StateObject(ApiModel):
    body: bytes = Field(alias="Body", max_length=_MAX_STATE_BYTES)

    @field_validator("body", mode="before")
    @classmethod
    def read_body(cls, value: object) -> bytes:
        if not isinstance(value, StreamingBody):
            raise ValueError("aws_review_destroy_state_body_invalid")
        try:
            return value.read(_MAX_STATE_BYTES + 1)
        finally:
            value.close()


def preserve_ledger(api: AwsApi, target: ReviewTarget, ledger: DestroyLedger) -> DestroyLedger:
    """EC2消失後もEBSとDB由来のIDを照合できるよう、所有bucketに非秘密台帳を残す。"""
    identity = ReviewTarget(account_id=target.account_id, demo_id=target.demo_id)
    buckets: list[str] = []
    resources = set(ledger.resources)
    stack_ids = {stack.stack_id for stack in ledger.stacks}
    recoverable = {"AWS::EC2::Volume", "RdsResourceId", "AWS::SecretsManager::Secret"}
    for bucket in sorted(ledger.identifiers("AWS::S3::Bucket")):
        try:
            tags = api.call(
                "s3",
                "get_bucket_tagging",
                BucketTags,
                Bucket=bucket,
                ExpectedBucketOwner=target.account_id,
            )
        except AwsCallError as error:
            if error.code == "NoSuchBucket":
                continue
            raise
        if not owned(tags.tags, target):
            raise ValueError("aws_review_destroy_bucket_ownership_mismatch")
        buckets.append(bucket)
        try:
            state_object = api.call(
                "s3",
                "get_object",
                StateObject,
                Bucket=bucket,
                Key=STATE_KEY,
                ExpectedBucketOwner=target.account_id,
            )
        except AwsCallError as error:
            if error.code == "NoSuchKey":
                continue
            raise
        state = SavedState.model_validate_json(state_object.body)
        if state.target != identity or not state.ledger.stacks:
            raise ValueError("aws_review_destroy_state_target_mismatch")
        for previous in state.ledger.stacks:
            require_stack(previous, target)
            if previous.stack_id not in stack_ids:
                raise ValueError("aws_review_destroy_state_stack_mismatch")
        # CFNに直接載るIDは現在の履歴からだけ採用し、保存台帳からは間接資源だけ復元する。
        resources.update(item for item in state.ledger.resources if item.kind in recoverable)
    merged = DestroyLedger(
        stacks=ledger.stacks,
        resources=tuple(sorted(resources, key=lambda item: (item.kind, item.identifier))),
    )
    body = SavedState(target=identity, ledger=merged).model_dump_json().encode()
    if len(body) > _MAX_STATE_BYTES:
        raise ValueError("aws_review_destroy_state_too_large")
    for bucket in buckets:
        api.call(
            "s3",
            "put_object",
            EmptyResponse,
            Bucket=bucket,
            Key=STATE_KEY,
            Body=body,
            ContentType="application/json",
            ServerSideEncryption="AES256",
            ExpectedBucketOwner=target.account_id,
        )
    return merged
