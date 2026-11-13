from __future__ import annotations

from datetime import timedelta

import pytest
from test_load_test_aws import ACCOUNT, NOW, FakeAws, ownership, plan

from quiz_app.load_test_aws.aws import AwsCallError
from quiz_app.load_test_aws.cleanup import cleanup_pass


def api_with_deleted_stack(kind: str, identifier: str) -> FakeAws:
    api = FakeAws()
    api.responses[("sts", "get_caller_identity")] = {
        "Account": ACCOUNT,
        "Arn": f"arn:aws:iam::{ACCOUNT}:role/cleanup",
    }
    api.responses[("ec2", "describe_addresses")] = {"Addresses": []}
    api.page_responses[("cloudformation", "list_stacks")] = [
        {
            "StackSummaries": [
                {
                    "StackId": "arn:deleted-workload",
                    "StackName": plan().workload_stack,
                    "StackStatus": "DELETE_COMPLETE",
                }
            ]
        }
    ]
    api.page_responses[("cloudformation", "list_stack_resources")] = [
        {
            "StackResourceSummaries": [
                {
                    "PhysicalResourceId": identifier,
                    "ResourceType": kind,
                    "ResourceStatus": "DELETE_COMPLETE",
                }
            ]
        }
    ]
    return api


def repository_api() -> tuple[FakeAws, str]:
    name = f"{plan().prefix}/app"
    api = api_with_deleted_stack("AWS::ECR::Repository", name)
    api.page_responses[("ecr", "describe_repositories")] = [
        {
            "repositories": [
                {
                    "repositoryName": name,
                    "repositoryArn": f"arn:aws:ecr:ap-northeast-1:{ACCOUNT}:repository/{name}",
                }
            ]
        }
    ]
    api.responses[("ecr", "list_tags_for_resource")] = {"tags": ownership()}
    return api, name


@pytest.mark.parametrize("operation", ["list_tags_for_resource", "delete_repository"])
def test_concurrent_repository_removal_fails_explicitly_then_fresh_pass_converges(
    operation: str,
) -> None:
    api, name = repository_api()
    failure = AwsCallError("ecr", operation, "RepositoryNotFoundException")
    api.responses[("ecr", operation)] = failure
    run = plan().cleanup_input()

    # 別の実行が一覧取得後に消した応答を、当該一巡の成功へ変換しない。
    with pytest.raises(AwsCallError) as caught:
        cleanup_pass(api, run, delete=True, now=NOW, include_control=False)
    assert caught.value is failure
    assert api.calls[-1][:2] == ("ecr", operation)
    if operation == "delete_repository":
        assert api.calls[-1][2] == {"repositoryName": name, "force": True}
    else:
        assert not any(action == "delete_repository" for _, action, _ in api.calls)

    boundary = len(api.calls)
    api.page_responses[("ecr", "describe_repositories")] = [{"repositories": []}]
    report = cleanup_pass(
        api, run, delete=True, now=NOW + timedelta(minutes=1), include_control=False
    )
    assert report.done is True
    assert report.residuals == ()
    assert report.stack_status == "ABSENT"
    fresh_calls = api.calls[boundary:]
    assert any(action == "list_stack_resources" for _, action, _ in fresh_calls)
    assert any(action == "describe_repositories" for _, action, _ in fresh_calls)
    assert not any(action == operation for _, action, _ in fresh_calls)


def test_concurrent_multipart_abort_requires_fresh_absence_after_bucket_deletion() -> None:
    bucket = f"{plan().prefix}-{ACCOUNT}"
    api = api_with_deleted_stack("AWS::S3::Bucket", bucket)
    api.page_responses[("s3", "list_buckets")] = [{"Buckets": [{"Name": bucket}]}]
    api.responses[("s3", "get_bucket_tagging")] = {"TagSet": ownership()}
    api.page_responses[("s3", "list_multipart_uploads")] = [
        {"Uploads": [{"Key": "evidence/archive", "UploadId": "removed-upload"}]}
    ]
    failure = AwsCallError("s3", "abort_multipart_upload", "NoSuchUpload")
    api.responses[("s3", "abort_multipart_upload")] = failure
    run = plan().cleanup_input()

    with pytest.raises(AwsCallError) as caught:
        cleanup_pass(api, run, delete=True, now=NOW, include_control=False)
    assert caught.value is failure
    assert api.calls[-1] == (
        "s3",
        "abort_multipart_upload",
        {"Bucket": bucket, "Key": "evidence/archive", "UploadId": "removed-upload"},
    )
    assert not any(action == "delete_bucket" for _, action, _ in api.calls)

    api.page_responses[("s3", "list_multipart_uploads")] = [{"Uploads": []}]
    pending = cleanup_pass(
        api, run, delete=True, now=NOW + timedelta(minutes=1), include_control=False
    )
    assert pending.done is False
    assert [(item.kind, item.identifier) for item in pending.residuals] == [("s3-bucket", bucket)]
    assert api.calls.count(("s3", "delete_bucket", {"Bucket": bucket})) == 1

    api.page_responses[("s3", "list_buckets")] = [{"Buckets": []}]
    report = cleanup_pass(
        api, run, delete=True, now=NOW + timedelta(minutes=2), include_control=False
    )
    assert report.done is True
    assert report.residuals == ()
    assert api.calls.count(("s3", "delete_bucket", {"Bucket": bucket})) == 1


@pytest.mark.parametrize("operation", ["list_tags_for_resource", "delete_repository"])
def test_permission_denial_is_not_treated_as_concurrent_removal(operation: str) -> None:
    api, _ = repository_api()
    failure = AwsCallError("ecr", operation, "AccessDeniedException")
    api.responses[("ecr", operation)] = failure
    for minute in (0, 1):
        with pytest.raises(AwsCallError) as caught:
            cleanup_pass(
                api,
                plan().cleanup_input(),
                delete=True,
                now=NOW + timedelta(minutes=minute),
                include_control=False,
            )
        assert caught.value is failure


def test_foreign_ownership_after_competing_pass_remains_refused() -> None:
    api, _ = repository_api()
    # 固定IDが台帳にあっても、他runの所有tagを再試行で上書き・黙認しない。
    api.responses[("ecr", "list_tags_for_resource")] = {
        "tags": [
            {"Key": tag["Key"], "Value": "foreign-run" if tag["Key"] == "RunId" else tag["Value"]}
            for tag in ownership()
        ]
    }
    for minute in (0, 1):
        with pytest.raises(ValueError, match="repository_ownership_mismatch"):
            cleanup_pass(
                api,
                plan().cleanup_input(),
                delete=True,
                now=NOW + timedelta(minutes=minute),
                include_control=False,
            )
    assert not any(action == "delete_repository" for _, action, _ in api.calls)
