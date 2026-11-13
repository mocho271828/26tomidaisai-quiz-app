from __future__ import annotations

from io import BytesIO
from typing import TypeVar

import pytest
from botocore.response import StreamingBody
from pydantic import BaseModel
from test_load_test_aws import FakeAws

from quiz_app.load_test_aws.aws import AwsCallError
from quiz_app.load_test_aws.ownership import ResourceIdentity
from quiz_app.load_test_aws.responses import Stack
from validation.aws_review import destroy as destroy_module
from validation.aws_review.destroy_inventory import DestroyLedger, read_ledger
from validation.aws_review.destroy_residue import remove_storage_residue
from validation.aws_review.destroy_state import SavedState, preserve_ledger
from validation.aws_review.destroy_verify import (
    present,
    require_no_untracked_resources,
    verify_resources,
)
from validation.aws_review.models import ReviewTarget

ResponseT = TypeVar("ResponseT", bound=BaseModel)
TARGET = ReviewTarget(account_id="123456789012", demo_id="review")


def tags() -> list[dict[str, str]]:
    return [
        {"Key": "DemoId", "Value": TARGET.demo_id},
        {"Key": "Purpose", "Value": "quiz-rehearsal"},
    ]


def stack(kind: str, status: str = "CREATE_COMPLETE", suffix: str = "id") -> Stack:
    name = f"{TARGET.prefix}-{kind}"
    return Stack.model_validate(
        {
            "StackId": (
                f"arn:aws:cloudformation:ap-northeast-1:{TARGET.account_id}:stack/{name}/{suffix}"
            ),
            "StackName": name,
            "StackStatus": status,
            "Tags": tags(),
        }
    )


def ledger(*resources: tuple[str, str]) -> DestroyLedger:
    return DestroyLedger(
        resources=tuple(
            ResourceIdentity(kind=kind, identifier=identifier) for kind, identifier in resources
        )
    )


class DeletionAws(FakeAws):
    def __init__(self, *, fail: str | None = None) -> None:
        super().__init__()
        self.stacks = {kind: stack(kind) for kind in ("control", "workload", "edge")}
        self.fail = fail
        self.responses[("sts", "get_caller_identity")] = {
            "Account": TARGET.account_id,
            "Arn": f"arn:aws:iam::{TARGET.account_id}:role/review",
        }
        self.responses[("s3", "get_bucket_tagging")] = {"TagSet": tags()}
        self.responses[("s3", "get_object")] = AwsCallError("s3", "get_object", "NoSuchKey")

    def call(
        self, service: str, operation: str, response: type[ResponseT], **kwargs: object
    ) -> ResponseT:
        if service == "cloudformation":
            self.calls.append((service, operation, kwargs))
            selected = next(
                item for item in self.stacks.values() if item.stack_id == kwargs["StackName"]
            )
            kind = selected.name.removeprefix(f"{TARGET.prefix}-")
            if operation == "delete_stack":
                status = "DELETE_FAILED" if kind == self.fail else "DELETE_COMPLETE"
                self.stacks[kind] = selected.model_copy(update={"status": status})
                return response.model_validate({})
            return response.model_validate({"Stacks": [selected.model_dump(by_alias=True)]})
        if operation == "head_bucket":
            self.calls.append((service, operation, kwargs))
            if self.stacks["control"].status == "DELETE_COMPLETE":
                raise AwsCallError(service, operation, "404")
        return super().call(service, operation, response, **kwargs)


def test_full_teardown_empties_control_bucket_after_workload_then_checks_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = DeletionAws()
    inventory = DestroyLedger(
        stacks=tuple(api.stacks.values()),
        resources=(ResourceIdentity(kind="AWS::S3::Bucket", identifier="owned-bucket"),),
    )
    monkeypatch.setattr(destroy_module, "read_ledger", lambda _api, _target: inventory)
    destroy_module.destroy(api, TARGET)
    mutations = [
        (operation, arguments)
        for _, operation, arguments in api.calls
        if operation in {"delete_stack", "get_bucket_tagging", "head_bucket"}
    ]
    assert [operation for operation, _ in mutations] == [
        "get_bucket_tagging",
        "delete_stack",
        "delete_stack",
        "get_bucket_tagging",
        "delete_stack",
        "head_bucket",
    ]
    assert [
        arguments["StackName"] for operation, arguments in mutations if operation == "delete_stack"
    ] == [stack(kind).stack_id for kind in ("edge", "workload", "control")]
    assert any(operation == "list_multipart_uploads" for _, operation, _ in api.calls)
    operations = [operation for _, operation, _ in api.calls]
    assert operations.index("put_object") < operations.index("delete_stack")


def test_failed_edge_deletion_never_deletes_workload_or_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = DeletionAws(fail="edge")
    monkeypatch.setattr(
        destroy_module,
        "read_ledger",
        lambda _api, _target: DestroyLedger(stacks=tuple(api.stacks.values())),
    )
    with pytest.raises(ValueError, match="stack_failed"):
        destroy_module.destroy(api, TARGET)
    assert [
        arguments["StackName"]
        for _, operation, arguments in api.calls
        if operation == "delete_stack"
    ] == [stack("edge").stack_id]


def test_stack_success_with_live_repository_times_out_before_control_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = DeletionAws()
    inventory = DestroyLedger(
        stacks=tuple(api.stacks.values()),
        resources=(ResourceIdentity(kind="AWS::ECR::Repository", identifier="owned/api"),),
    )
    monkeypatch.setattr(destroy_module, "read_ledger", lambda _api, _target: inventory)
    clock = iter((0.0, 2.0))
    monkeypatch.setattr(destroy_module.time, "monotonic", lambda: next(clock))
    with pytest.raises(TimeoutError, match="incomplete"):
        destroy_module.destroy(api, TARGET, timeout_seconds=1)
    assert api.stacks["edge"].status == "DELETE_COMPLETE"
    assert api.stacks["workload"].status == "DELETE_COMPLETE"
    assert api.stacks["control"].status == "CREATE_COMPLETE"


def test_new_resource_type_requires_verifier_before_any_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = DeletionAws()
    inventory = ledger(("AWS::Lambda::Function", "unhandled"))
    monkeypatch.setattr(destroy_module, "read_ledger", lambda _api, _target: inventory)
    with pytest.raises(ValueError, match="unsupported_resource"):
        destroy_module.destroy(api, TARGET)
    assert not any(operation.startswith("delete") for _, operation, _ in api.calls)


def test_failed_cloudformation_deletion_can_be_retried_with_same_arn() -> None:
    api = DeletionAws()
    previous = stack("edge", "DELETE_FAILED")
    api.stacks["edge"] = previous
    destroy_module._delete_stack(api, TARGET, previous, float("inf"))
    assert api.stacks["edge"].status == "DELETE_COMPLETE"
    assert api.calls[0] == ("cloudformation", "delete_stack", {"StackName": previous.stack_id})


def test_in_progress_cloudformation_deletion_is_observed_without_duplicate_request() -> None:
    api = DeletionAws()
    api.stacks["edge"] = stack("edge", "DELETE_COMPLETE")
    destroy_module._delete_stack(api, TARGET, stack("edge", "DELETE_IN_PROGRESS"), float("inf"))
    assert [operation for _, operation, _ in api.calls] == ["describe_stacks"]


def test_account_mismatch_cannot_trigger_any_deletion() -> None:
    api = DeletionAws()
    api.responses[("sts", "get_caller_identity")] = {
        "Account": "999999999999",
        "Arn": "arn:aws:iam::999999999999:role/other",
    }
    with pytest.raises(ValueError, match="account_mismatch"):
        destroy_module.destroy(api, TARGET)
    assert all(operation == "get_caller_identity" for _, operation, _ in api.calls)


def test_ledger_keeps_deleted_attempt_and_recovers_root_volume_and_managed_secret() -> None:
    api = FakeAws()
    old = stack("workload", "DELETE_COMPLETE", "old")
    api.page_responses[("cloudformation", "list_stacks")] = [
        {"StackSummaries": [old.model_dump(by_alias=True)]},
    ]
    api.responses[("cloudformation", "describe_stacks")] = {
        "Stacks": [old.model_dump(by_alias=True)]
    }
    api.page_responses[("cloudformation", "list_stack_resources")] = [
        {
            "StackResourceSummaries": [
                {
                    "ResourceType": "AWS::EC2::Instance",
                    "PhysicalResourceId": "i-owned",
                    "ResourceStatus": "DELETE_COMPLETE",
                },
                {
                    "ResourceType": "AWS::RDS::DBInstance",
                    "PhysicalResourceId": "db-owned",
                    "ResourceStatus": "DELETE_COMPLETE",
                },
            ]
        }
    ]
    api.page_responses[("ec2", "describe_instances")] = [
        {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-owned",
                            "State": {"Name": "running"},
                            "Tags": tags(),
                            "BlockDeviceMappings": [{"Ebs": {"VolumeId": "vol-root"}}],
                        }
                    ]
                }
            ]
        }
    ]
    api.page_responses[("rds", "describe_db_instances")] = [
        {
            "DBInstances": [
                {
                    "DBInstanceIdentifier": "db-owned",
                    "DbiResourceId": "db-resource",
                    "MasterUserSecret": {"SecretArn": "arn:managed-secret"},
                }
            ]
        }
    ]
    observed = read_ledger(api, TARGET)
    assert observed.stacks == (old,)
    assert observed.identifiers("AWS::EC2::Volume") == {"vol-root"}
    assert observed.identifiers("AWS::SecretsManager::Secret") == {"arn:managed-secret"}
    assert observed.identifiers("RdsResourceId") == {"db-resource"}


def test_same_name_without_ownership_is_not_a_deletion_target() -> None:
    api = FakeAws()
    candidate = stack("workload").model_copy(update={"tags": ()})
    api.page_responses[("cloudformation", "list_stacks")] = [
        {"StackSummaries": [candidate.model_dump(by_alias=True)]}
    ]
    api.responses[("cloudformation", "describe_stacks")] = {
        "Stacks": [candidate.model_dump(by_alias=True)]
    }
    with pytest.raises(ValueError, match="ownership_mismatch"):
        read_ledger(api, TARGET)
    assert not any(operation.startswith("delete") for _, operation, _ in api.calls)


def test_eip_uses_public_ip_and_deleted_nat_is_not_billable_residue() -> None:
    api = FakeAws()
    api.responses[("ec2", "describe_addresses")] = {"Addresses": []}
    api.page_responses[("ec2", "describe_nat_gateways")] = [
        {"NatGateways": [{"NatGatewayId": "nat-owned", "State": "deleted"}]}
    ]
    observed = verify_resources(
        api,
        TARGET,
        ledger(("AWS::EC2::EIP", "192.0.2.1"), ("AWS::EC2::NatGateway", "nat-owned")),
        include_control=True,
    )
    assert observed == ()
    assert next(
        arguments for _, operation, arguments in api.calls if operation == "describe_addresses"
    ) == {
        "Filters": [{"Name": "public-ip", "Values": ["192.0.2.1"]}],
    }


def test_access_denied_is_not_absence() -> None:
    api = FakeAws()
    api.responses[("s3", "head_bucket")] = AwsCallError("s3", "head_bucket", "403")
    with pytest.raises(AwsCallError, match="403"):
        present(api, "s3", "head_bucket", ("404",), Bucket="owned-bucket")


@pytest.mark.parametrize("backup_tags", [[], tags()])
def test_retained_database_backup_requires_parent_and_immutable_id(
    backup_tags: list[dict[str, str]],
) -> None:
    api = FakeAws()
    api.page_responses[("rds", "describe_db_instance_automated_backups")] = [
        {
            "DBInstanceAutomatedBackups": [
                {
                    "DBInstanceIdentifier": "db-owned",
                    "DbiResourceId": "db-resource",
                    "Status": "retained",
                    "TagList": backup_tags,
                }
            ]
        }
    ]
    with pytest.raises(ValueError, match="backup_ownership_mismatch"):
        remove_storage_residue(api, TARGET, ledger(("AWS::RDS::DBInstance", "db-owned")))
    result = remove_storage_residue(
        api, TARGET, ledger(("AWS::RDS::DBInstance", "db-owned"), ("RdsResourceId", "db-resource"))
    )
    assert result == ("rds-backup:db-resource",)
    assert api.calls[-2][1] == "delete_db_instance_automated_backup"


def test_untracked_tagged_resource_fails_without_deleting_it() -> None:
    api = FakeAws()
    api.page_responses[("resourcegroupstaggingapi", "get_resources")] = [
        {
            "ResourceTagMappingList": [
                {
                    "ResourceARN": (
                        f"arn:aws:ec2:ap-northeast-1:{TARGET.account_id}:volume/vol-unknown"
                    ),
                    "Tags": tags(),
                }
            ]
        }
    ]
    with pytest.raises(ValueError, match="untracked_resource"):
        require_no_untracked_resources(api, TARGET, DestroyLedger())
    assert all(operation == "get_resources" for _, operation, _ in api.calls)


def test_no_resources_is_idempotent_and_only_checks_aws() -> None:
    api = DeletionAws()
    destroy_module.destroy(api, TARGET)
    assert not any(
        operation.startswith(("delete", "stop", "terminate")) for _, operation, _ in api.calls
    )


def test_retry_recovers_indirect_volume_from_saved_owned_control_bucket() -> None:
    api = DeletionAws()
    current = DestroyLedger(
        stacks=tuple(api.stacks.values()),
        resources=(ResourceIdentity(kind="AWS::S3::Bucket", identifier="owned-bucket"),),
    )
    previous = current.model_copy(
        update={
            "resources": (
                *current.resources,
                ResourceIdentity(kind="AWS::EC2::Volume", identifier="vol-recovered"),
                ResourceIdentity(kind="AWS::RDS::DBInstance", identifier="not-in-current-cfn"),
            )
        }
    )
    body = SavedState(target=TARGET, ledger=previous).model_dump_json().encode()
    stream = BytesIO(body)
    api.responses[("s3", "get_object")] = {"Body": StreamingBody(stream, len(body))}
    recovered = preserve_ledger(api, TARGET, current)
    assert recovered.identifiers("AWS::EC2::Volume") == {"vol-recovered"}
    assert recovered.identifiers("AWS::RDS::DBInstance") == set()
    assert stream.closed
    write = next(arguments for _, operation, arguments in api.calls if operation == "put_object")
    assert write["Key"] == "review-destroy-ledger.json"
    assert write["ExpectedBucketOwner"] == TARGET.account_id


def test_saved_ledger_for_another_stack_cannot_authorize_deletion() -> None:
    api = DeletionAws()
    current = DestroyLedger(
        stacks=(stack("control"),),
        resources=(ResourceIdentity(kind="AWS::S3::Bucket", identifier="owned-bucket"),),
    )
    previous = current.model_copy(update={"stacks": (stack("control", suffix="other"),)})
    body = SavedState(target=TARGET, ledger=previous).model_dump_json().encode()
    api.responses[("s3", "get_object")] = {"Body": StreamingBody(BytesIO(body), len(body))}
    with pytest.raises(ValueError, match="state_stack_mismatch"):
        preserve_ledger(api, TARGET, current)
    assert not any(operation == "put_object" for _, operation, _ in api.calls)


@pytest.mark.parametrize("code", ["ResourceNotFound", "AccessDenied", None])
def test_dashboard_absence_requires_explicit_not_found(code: str | None) -> None:
    api = FakeAws()
    name = f"{TARGET.prefix}-health"
    api.responses[("cloudwatch", "get_dashboard")] = (
        {} if code is None else AwsCallError("cloudwatch", "get_dashboard", code)
    )
    inventory = ledger(("AWS::CloudWatch::Dashboard", name))
    if code == "AccessDenied":
        with pytest.raises(AwsCallError, match="AccessDenied"):
            verify_resources(api, TARGET, inventory, include_control=False)
    else:
        assert verify_resources(api, TARGET, inventory, include_control=False) == (
            () if code else (f"AWS::CloudWatch::Dashboard:{name}",)
        )
    assert api.calls[0] == ("cloudwatch", "get_dashboard", {"DashboardName": name})


def test_dashboard_is_removed_with_edge_and_verified_before_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = DeletionAws()
    api.responses[("cloudwatch", "get_dashboard")] = AwsCallError(
        "cloudwatch", "get_dashboard", "ResourceNotFound"
    )
    inventory = DestroyLedger(
        stacks=tuple(api.stacks.values()),
        resources=(
            ResourceIdentity(
                kind="AWS::CloudWatch::Dashboard", identifier=f"{TARGET.prefix}-health"
            ),
        ),
    )
    monkeypatch.setattr(destroy_module, "read_ledger", lambda _api, _target: inventory)
    destroy_module.destroy(api, TARGET)
    assert any(operation == "get_dashboard" for _, operation, _ in api.calls)
    assert all(item.status == "DELETE_COMPLETE" for item in api.stacks.values())


def test_dashboard_outside_dedicated_name_blocks_destroy(monkeypatch: pytest.MonkeyPatch) -> None:
    api = DeletionAws()
    inventory = ledger(("AWS::CloudWatch::Dashboard", "tomidaisai-guest"))
    monkeypatch.setattr(destroy_module, "read_ledger", lambda _api, _target: inventory)
    with pytest.raises(ValueError, match="dashboard_identity_mismatch"):
        destroy_module.destroy(api, TARGET)
    assert not any(operation.startswith("delete") for _, operation, _ in api.calls)
