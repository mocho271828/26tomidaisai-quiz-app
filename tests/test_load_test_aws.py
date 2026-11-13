from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TypeVar
from uuid import UUID

import pytest
from pydantic import BaseModel, ValidationError

from quiz_app.load_test_aws.aws import AwsCallError
from quiz_app.load_test_aws.cleanup import cleanup_pass
from quiz_app.load_test_aws.inventory import empty_bucket, scan_databases, scan_ec2
from quiz_app.load_test_aws.lifecycle import request_cleanup, require_deadline
from quiz_app.load_test_aws.models import (
    AwsPreparationEvidence,
    AwsRunPlan,
    deadline_target,
)
from quiz_app.load_test_aws.responses import Stack
from quiz_app.load_test_aws.templates import parameters, template_body

ResponseT = TypeVar("ResponseT", bound=BaseModel)
NOW = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
RUN_ID = UUID("0a758be1-15dd-4e1a-a4c6-d32d1f41a701")
ACCOUNT = "123456789012"


def plan() -> AwsRunPlan:
    return AwsRunPlan(
        run_id=RUN_ID,
        account_id=ACCOUNT,
        source_commit="a" * 40,
        manifest_sha256="b" * 64,
        created_at=NOW,
        notification_email="owner@example.net",
        ami_id="ami-0123456789abcdef0",
        availability_zone="ap-northeast-1a",
        secondary_availability_zone="ap-northeast-1c",
        worker_package_sha256="c" * 64,
        schema_revision="20260804_0002",
        cloudformation_role_arn=f"arn:aws:iam::{ACCOUNT}:role/test-cfn",
    )


def ownership() -> list[dict[str, str]]:
    return [
        {"Key": "Project", "Value": "gs-app-tomidaisai"},
        {"Key": "RunId", "Value": str(RUN_ID)},
    ]


class FakeAws:
    def __init__(self) -> None:
        self.responses: dict[tuple[str, str], object] = {
            ("lambda", "get_function_configuration"): AwsCallError(
                "lambda", "get_function_configuration", "ResourceNotFoundException"
            ),
            ("scheduler", "get_schedule"): AwsCallError(
                "scheduler", "get_schedule", "ResourceNotFoundException"
            ),
            ("sqs", "get_queue_url"): AwsCallError("sqs", "get_queue_url", "QueueDoesNotExist"),
        }
        self.page_responses: dict[tuple[str, str], list[object]] = {}
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def call(
        self, service: str, operation: str, response: type[ResponseT], **kwargs: object
    ) -> ResponseT:
        self.calls.append((service, operation, kwargs))
        raw = self.responses.get((service, operation), {})
        if isinstance(raw, AwsCallError):
            raise raw
        return response.model_validate(raw)

    def pages(
        self, service: str, operation: str, response: type[ResponseT], **kwargs: object
    ) -> Iterator[ResponseT]:
        self.calls.append((service, operation, kwargs))
        for raw in self.page_responses.get((service, operation), []):
            yield response.model_validate(raw)


def test_budget_is_deterministic_and_includes_worst_case_credit() -> None:
    configuration = plan()
    assert configuration.estimated_total_jpy == Decimal("944.63")
    assert configuration.expires_at == NOW + timedelta(hours=3)
    assert configuration.cleanup_budget_at == NOW + timedelta(hours=4)
    configuration.require_creation_window(NOW + timedelta(minutes=80))
    with pytest.raises(ValueError, match="remaining_time"):
        configuration.require_creation_window(NOW + timedelta(minutes=90))


def test_plan_rejects_account_or_timezone_drift() -> None:
    original = plan().model_dump()
    with pytest.raises(ValidationError):
        AwsRunPlan.model_validate(
            {
                **original,
                "cloudformation_role_arn": "arn:aws:iam::999999999999:role/other",
            }
        )
    with pytest.raises(ValidationError):
        AwsRunPlan.model_validate({**original, "created_at": NOW.replace(tzinfo=None)})


def test_templates_keep_app_private_and_all_state_disposable() -> None:
    workload = json.loads(template_body("workload"))
    resources = workload["Resources"]
    for repository in ("App", "Gateway"):
        assert workload["Outputs"][f"{repository}RepositoryUri"]["Value"] == {
            "Fn::GetAtt": [f"{repository}Repository", "RepositoryUri"]
        }
    assert resources["Database"]["DeletionPolicy"] == "Delete"
    assert resources["Database"]["Properties"]["DeleteAutomatedBackups"] is True
    assert resources["Database"]["Properties"]["PubliclyAccessible"] is False
    for host in ("App", "Runner"):
        mapping = resources[host + "Instance"]["Properties"]["BlockDeviceMappings"]
        assert len(mapping) == 1
        assert mapping[0]["DeviceName"] == "/dev/sda1"
        assert mapping[0]["Ebs"]["DeleteOnTermination"] is True
    assert resources["AppRepository"]["Properties"]["EmptyOnDelete"] is True
    ingress = resources["AppSecurityGroup"]["Properties"]["SecurityGroupIngress"]
    assert ingress == [
        {
            "IpProtocol": "tcp",
            "FromPort": 443,
            "ToPort": 443,
            "SourceSecurityGroupId": {"Ref": "RunnerSecurityGroup"},
        }
    ]
    assert all(
        resource["Type"] not in {"AWS::EC2::NatGateway", "AWS::Route53::HostedZone"}
        for resource in resources.values()
    )


def test_empty_bucket_deletes_old_versions_markers_and_multipart() -> None:
    api = FakeAws()
    api.page_responses[("s3", "list_object_versions")] = [
        {
            "Versions": [{"Key": "report", "VersionId": "v1"}],
            "DeleteMarkers": [{"Key": "report", "VersionId": "v2"}],
        }
    ]
    api.page_responses[("s3", "list_multipart_uploads")] = [
        {"Uploads": [{"Key": "partial", "UploadId": "upload"}]}
    ]
    empty_bucket(api, "owned-bucket")
    deletion = next(kwargs for _, operation, kwargs in api.calls if operation == "delete_objects")
    assert deletion["Delete"] == {
        "Objects": [
            {"Key": "report", "VersionId": "v1"},
            {"Key": "report", "VersionId": "v2"},
        ],
        "Quiet": True,
    }
    assert any(operation == "abort_multipart_upload" for _, operation, _ in api.calls)


def test_partial_s3_delete_is_never_reported_as_success() -> None:
    api = FakeAws()
    api.page_responses[("s3", "list_object_versions")] = [
        {"Versions": [{"Key": "a", "VersionId": "v"}]}
    ]
    api.responses[("s3", "delete_objects")] = {"Errors": [{"Code": "AccessDenied"}]}
    with pytest.raises(ValueError, match="partial_delete_failed"):
        empty_bucket(api, "owned-bucket")


def test_read_only_scan_never_mutates_and_keeps_async_deletion_pending() -> None:
    api = FakeAws()
    api.responses[("sts", "get_caller_identity")] = {
        "Account": ACCOUNT,
        "Arn": "arn:aws:iam::123456789012:role/test",
    }
    api.responses[("ec2", "describe_addresses")] = {"Addresses": []}
    api.page_responses[("rds", "describe_db_instances")] = [
        {
            "DBInstances": [
                {
                    "DBInstanceIdentifier": plan().prefix + "-postgres",
                    "DBInstanceArn": "arn:db",
                    "DBInstanceStatus": "deleting",
                    "TagList": ownership(),
                }
            ]
        }
    ]
    report = cleanup_pass(api, plan().cleanup_input(), delete=False, now=NOW)
    assert report.done is False
    assert report.residuals[0].state == "deleting"
    assert not any(
        operation.startswith(("delete", "terminate", "release", "disassociate"))
        for _, operation, _ in api.calls
    )


def test_foreign_account_refuses_before_any_inventory_or_delete() -> None:
    api = FakeAws()
    api.responses[("sts", "get_caller_identity")] = {
        "Account": "999999999999",
        "Arn": "arn:foreign",
    }
    with pytest.raises(ValueError, match="account_mismatch"):
        cleanup_pass(api, plan().cleanup_input(), delete=True, now=NOW)
    assert [operation for _, operation, _ in api.calls] == ["get_caller_identity"]


def test_foreign_tagged_database_is_not_deleted() -> None:
    api = FakeAws()
    api.page_responses[("rds", "describe_db_instances")] = [
        {
            "DBInstances": [
                {
                    "DBInstanceIdentifier": "production",
                    "DBInstanceArn": "arn:production",
                    "DBInstanceStatus": "available",
                    "TagList": [{"Key": "RunId", "Value": str(RUN_ID)}],
                }
            ]
        }
    ]
    assert scan_databases(api, plan().cleanup_input(), delete=True) == []
    assert not any(operation.startswith("delete") for _, operation, _ in api.calls)


def test_terminated_instance_is_not_a_charged_residual() -> None:
    api = FakeAws()
    api.responses[("ec2", "describe_addresses")] = {"Addresses": []}
    api.page_responses[("ec2", "describe_instances")] = [
        {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-0123456789abcdef0",
                            "State": {"Name": "terminated"},
                            "Tags": ownership(),
                        }
                    ]
                }
            ]
        }
    ]
    assert scan_ec2(api, plan().cleanup_input(), delete=True) == []


def test_cleanup_request_has_no_local_sleep_or_resource_deletion() -> None:
    api = FakeAws()
    api.responses[("sts", "get_caller_identity")] = {
        "Account": ACCOUNT,
        "Arn": "arn:caller",
    }
    api.responses[("stepfunctions", "start_execution")] = {
        "executionArn": "arn:execution",
        "startDate": NOW,
    }
    execution = request_cleanup(api, plan().cleanup_input())
    assert execution.arn == "arn:execution"
    service, operation, kwargs = api.calls[-1]
    assert (service, operation) == ("stepfunctions", "start_execution")
    assert json.loads(str(kwargs["input"])) == {
        "run": plan().cleanup_input().model_dump(mode="json")
    }


def test_preparation_requires_independent_private_hosts() -> None:
    configuration = plan()
    values = {
        "run_id": RUN_ID,
        "account_id": ACCOUNT,
        "source_commit": "a" * 40,
        "manifest_sha256": "b" * 64,
        "app_instance_id": "i-0123456789abcdef0",
        "runner_instance_id": "i-1123456789abcdef0",
        "app_private_ip": "10.77.1.10",
        "runner_private_ip": "10.77.1.11",
        "public_origin": "https://10.77.1.10",
        "ca_sha256": "d" * 64,
        "prepared_at": NOW,
        "expires_at": configuration.expires_at,
    }
    assert AwsPreparationEvidence.model_validate(values).run_id == RUN_ID
    with pytest.raises(ValidationError):
        AwsPreparationEvidence.model_validate({**values, "runner_private_ip": "10.77.1.10"})
    with pytest.raises(ValidationError):
        AwsPreparationEvidence.model_validate({**values, "public_origin": "http://10.77.1.10"})


def test_notification_address_is_not_serialized_to_plan_output() -> None:
    assert "owner@example.net" not in plan().model_dump_json()
    control_parameters = parameters(plan(), "control")
    assert (
        next(
            item["ParameterValue"]
            for item in control_parameters
            if item["ParameterKey"] == "NotificationEmail"
        )
        == "owner@example.net"
    )


def test_deadline_without_self_deletion_fails_before_start() -> None:
    configuration = plan()
    api = FakeAws()
    state_machine = "arn:state-machine"
    api.responses[("scheduler", "get_schedule")] = {
        "State": "ENABLED",
        "ScheduleExpression": configuration.expires_at.strftime("at(%Y-%m-%dT%H:%M:%S)"),
        "ScheduleExpressionTimezone": "UTC",
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "Target": {
            "Arn": "arn:aws:scheduler:::aws-sdk:sfn:startExecution",
            "Input": json.dumps(deadline_target(configuration.cleanup_input())),
            "RoleArn": "arn:scheduler-role",
            "DeadLetterConfig": {"Arn": "arn:deadline-queue"},
            "RetryPolicy": {"MaximumEventAgeInSeconds": 3600, "MaximumRetryAttempts": 20},
        },
        "ActionAfterCompletion": "NONE",
    }
    control = Stack(
        StackId="arn:stack",
        StackName=configuration.control_stack,
        StackStatus="CREATE_COMPLETE",
        Outputs=[
            {"OutputKey": "StateMachineArn", "OutputValue": state_machine},
            {"OutputKey": "SchedulerRoleArn", "OutputValue": "arn:scheduler-role"},
            {"OutputKey": "DeadlineQueueArn", "OutputValue": "arn:deadline-queue"},
        ],
    )
    with pytest.raises(ValueError, match="auto_delete_missing"):
        require_deadline(api, configuration, control)
    assert len(api.calls) == 1


def test_dedicated_role_policy_uses_only_run_resources_and_finite_actions() -> None:
    from quiz_app.load_test_aws.bootstrap import execution_policy_documents

    configuration = AwsRunPlan.model_validate(
        {**plan().model_dump(), "cloudformation_role_arn": None}
    )
    documents = execution_policy_documents(configuration)
    assert 1 <= len(documents) <= 10
    assert all(len(document) <= 6144 for document in documents)
    statements = [statement for body in documents for statement in json.loads(body)["Statement"]]
    assert all(
        not action.endswith(":*") for statement in statements for action in statement["Action"]
    )
    passing = next(statement for statement in statements if statement["Sid"] == "PassRunRoles")
    assert passing["Resource"] == f"arn:aws:iam::{ACCOUNT}:role/{configuration.prefix}-*"
    assert "iam:PassedToService" in passing["Condition"]["StringEquals"]
    ec2_mutation = next(
        statement for statement in statements if statement["Sid"] == "ManageOwnedNetwork"
    )
    assert ec2_mutation["Condition"]["StringEquals"]["ec2:ResourceTag/RunId"] == str(RUN_ID)


def test_deadline_and_local_cleanup_have_identical_execution_identity() -> None:
    from quiz_app.load_test_aws.models import execution_input

    run = plan().cleanup_input()
    target = deadline_target(run)
    assert target["Name"] == "cleanup"
    assert target["Input"] == execution_input(run)
    parameters_by_name = {
        item["ParameterKey"]: item["ParameterValue"] for item in parameters(plan(), "control")
    }
    assert json.loads(parameters_by_name["DeadlineTargetInput"]) == target


@pytest.mark.parametrize("run_id", [RUN_ID, UUID("a1234567-1234-4234-8234-123456789012")])
def test_cfn_update_permission_is_limited_to_exact_run_cleanup_machine(run_id: UUID) -> None:
    from quiz_app.load_test_aws.bootstrap import execution_policy_documents

    configuration = AwsRunPlan.model_validate({**plan().model_dump(), "run_id": run_id})
    statements = [
        statement
        for body in execution_policy_documents(configuration)
        for statement in json.loads(body)["Statement"]
        if "states:UpdateStateMachine" in statement["Action"]
    ]
    assert statements == [
        {
            "Sid": "UpdateRunCleanupStateMachine",
            "Effect": "Allow",
            "Action": ["states:UpdateStateMachine"],
            "Resource": (
                f"arn:aws:states:{configuration.region}:{configuration.account_id}:"
                f"stateMachine:{configuration.prefix}-cleanup"
            ),
        }
    ]


def test_s3_deployment_roles_cannot_read_other_hosts_secrets() -> None:
    resources = json.loads(template_body("workload"))["Resources"]
    for host in ("App", "Runner"):
        role = resources[f"{host}Role"]["Properties"]
        statements = role["Policies"][0]["PolicyDocument"]["Statement"]
        deployment = [
            statement for statement in statements if "deployment/" in json.dumps(statement)
        ]
        assert len(deployment) == 1
        assert deployment[0]["Resource"]["Fn::Sub"].endswith(f"/deployment/{host.lower()}/*")
    assert resources["RedisAppUser"]["Properties"]["UserName"] == "default"
    assert resources["RedisMonitorUser"]["Properties"]["UserName"] == "metrics"
    assert "AuthToken" not in resources["Redis"]["Properties"]


def test_runtime_hosts_cannot_forge_local_collection_acknowledgment() -> None:
    resources = json.loads(template_body("workload"))["Resources"]
    for role, destination in (
        ("App", "evidence/app-worker/*"),
        ("Runner", "evidence/runner/archive.tar.gz"),
    ):
        statements = resources[f"{role}Role"]["Properties"]["Policies"][0]["PolicyDocument"][
            "Statement"
        ]
        writes = [statement for statement in statements if "s3:PutObject" in statement["Action"]]
        assert len(writes) == 1
        assert writes[0]["Resource"] == {"Fn::Sub": f"arn:aws:s3:::${{Artifacts}}/{destination}"}
        lists = [statement for statement in statements if "s3:ListBucket" in statement["Action"]]
        if role == "App":
            assert lists == []
        else:
            assert lists == [
                {
                    "Effect": "Allow",
                    "Action": ["s3:ListBucket"],
                    "Resource": {"Fn::GetAtt": ["Artifacts", "Arn"]},
                    "Condition": {"StringEquals": {"s3:prefix": "evidence/runner/collected.json"}},
                }
            ]


def test_control_remains_pending_when_worker_can_run() -> None:
    from quiz_app.load_test_aws.storage import scan_active_control

    api = FakeAws()
    api.responses[("lambda", "get_function_configuration")] = {"State": "Active"}
    observed = scan_active_control(api, plan().cleanup_input())
    assert [item.kind for item in observed] == ["cleanup-worker-code"]


def test_tag_only_ami_is_not_deleted_without_stack_ownership() -> None:
    api = FakeAws()
    api.responses[("ec2", "describe_addresses")] = {"Addresses": []}
    api.page_responses[("ec2", "describe_images")] = [
        {
            "Images": [
                {"ImageId": "ami-0123456789abcdef0", "State": "available", "Tags": ownership()}
            ]
        }
    ]
    observed = scan_ec2(api, plan().cleanup_input(), delete=True)
    assert observed[0].kind == "ami"
    assert not any(operation == "deregister_image" for _, operation, _ in api.calls)


def test_owned_stack_resource_can_terminate_but_tag_only_instance_cannot() -> None:
    from quiz_app.load_test_aws.ownership import ResourceIdentity, RunLedger

    api = FakeAws()
    api.responses[("ec2", "describe_addresses")] = {"Addresses": []}
    api.page_responses[("ec2", "describe_instances")] = [
        {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-0123456789abcdef0",
                            "State": {"Name": "running"},
                            "Tags": ownership(),
                        },
                        {
                            "InstanceId": "i-1123456789abcdef0",
                            "State": {"Name": "running"},
                            "Tags": ownership(),
                        },
                    ]
                }
            ]
        }
    ]
    ledger = RunLedger(
        resources=(ResourceIdentity(kind="AWS::EC2::Instance", identifier="i-0123456789abcdef0"),)
    )
    scan_ec2(api, plan().cleanup_input(), delete=True, ledger=ledger)
    deletions = [
        parameters["InstanceIds"]
        for _, operation, parameters in api.calls
        if operation == "terminate_instances"
    ]
    assert deletions == [["i-0123456789abcdef0"]]


def test_network_scan_is_limited_to_stack_vpc_ids() -> None:
    from quiz_app.load_test_aws.networks import scan_networks
    from quiz_app.load_test_aws.ownership import ResourceIdentity, RunLedger

    api = FakeAws()
    api.responses[("ec2", "describe_vpcs")] = {"Vpcs": [{"VpcId": "vpc-1234"}]}
    api.responses[("ec2", "describe_internet_gateways")] = {"InternetGateways": []}
    ledger = RunLedger(resources=(ResourceIdentity(kind="AWS::EC2::VPC", identifier="vpc-1234"),))
    assert [
        item.kind for item in scan_networks(api, plan().cleanup_input(), ledger, delete=False)
    ] == ["vpc"]
    for _, operation, request in api.calls:
        assert not operation.startswith("delete")
        assert request["Filters"][0]["Values"] == ["vpc-1234"]


def test_completed_cleanup_is_idempotent_and_checks_exact_input() -> None:
    from quiz_app.load_test_aws.models import execution_input

    api = FakeAws()
    api.responses[("sts", "get_caller_identity")] = {"Account": ACCOUNT, "Arn": "arn:caller"}
    api.responses[("stepfunctions", "start_execution")] = AwsCallError(
        "stepfunctions", "start_execution", "ExecutionAlreadyExists"
    )
    run = plan().cleanup_input()
    arn = f"arn:aws:states:{run.region}:{ACCOUNT}:execution:{run.prefix}-cleanup:cleanup"
    api.responses[("stepfunctions", "describe_execution")] = {
        "executionArn": arn,
        "startDate": NOW,
        "status": "SUCCEEDED",
        "input": execution_input(run),
    }
    assert request_cleanup(api, run).status == "SUCCEEDED"
    api.responses[("stepfunctions", "describe_execution")] = {
        "executionArn": arn,
        "startDate": NOW,
        "status": "SUCCEEDED",
        "input": "{}",
    }
    with pytest.raises(ValueError, match="input_mismatch"):
        request_cleanup(api, run)


def test_known_instance_without_tags_is_a_residual_not_absent() -> None:
    from quiz_app.load_test_aws.identity_scan import scan_changed_identity
    from quiz_app.load_test_aws.ownership import ResourceIdentity, RunLedger

    api = FakeAws()
    identifier = "i-0123456789abcdef0"
    api.page_responses[("ec2", "describe_instances")] = [
        {
            "Reservations": [
                {
                    "Instances": [
                        {"InstanceId": identifier, "State": {"Name": "running"}, "Tags": []}
                    ]
                }
            ]
        }
    ]
    ledger = RunLedger(
        resources=(ResourceIdentity(kind="AWS::EC2::Instance", identifier=identifier),)
    )
    result = scan_changed_identity(api, plan().cleanup_input(), ledger)
    assert result[0].state == "ownership-unverified"
    assert api.calls[0][2]["Filters"] == [{"Name": "instance-id", "Values": [identifier]}]


def test_known_database_without_tags_is_a_residual_not_absent() -> None:
    from quiz_app.load_test_aws.ownership import ResourceIdentity, RunLedger

    api = FakeAws()
    identifier = plan().prefix + "-postgres"
    api.page_responses[("rds", "describe_db_instances")] = [
        {
            "DBInstances": [
                {
                    "DBInstanceIdentifier": identifier,
                    "DBInstanceArn": "arn:db",
                    "DBInstanceStatus": "available",
                    "TagList": [],
                }
            ]
        }
    ]
    ledger = RunLedger(
        resources=(ResourceIdentity(kind="AWS::RDS::DBInstance", identifier=identifier),)
    )
    result = scan_databases(api, plan().cleanup_input(), delete=True, ledger=ledger)
    assert result[0].state == "ownership-unverified"
    assert not any(operation.startswith("delete") for _, operation, _ in api.calls)


def test_log_prefix_alone_does_not_authorize_deletion() -> None:
    from quiz_app.load_test_aws.storage import scan_storage

    api = FakeAws()
    api.page_responses[("logs", "describe_log_groups")] = [
        {"logGroups": [{"logGroupName": f"/gs-loadtest/{plan().prefix}/unknown"}]}
    ]
    result = scan_storage(api, plan().cleanup_input(), delete=True)
    assert result[0].kind == "log-group"
    assert not any(operation == "delete_log_group" for _, operation, _ in api.calls)


def test_vpc_membership_alone_does_not_authorize_eni_deletion() -> None:
    from quiz_app.load_test_aws.networks import scan_networks
    from quiz_app.load_test_aws.ownership import ResourceIdentity, RunLedger

    api = FakeAws()
    api.responses[("ec2", "describe_vpcs")] = {"Vpcs": [{"VpcId": "vpc-1234"}]}
    api.responses[("ec2", "describe_internet_gateways")] = {"InternetGateways": []}
    api.page_responses[("ec2", "describe_network_interfaces")] = [
        {
            "NetworkInterfaces": [
                {
                    "NetworkInterfaceId": "eni-1234",
                    "Status": "available",
                    "RequesterManaged": False,
                    "TagSet": ownership(),
                }
            ]
        }
    ]
    ledger = RunLedger(resources=(ResourceIdentity(kind="AWS::EC2::VPC", identifier="vpc-1234"),))
    result = scan_networks(api, plan().cleanup_input(), ledger, delete=True)
    assert any(item.kind == "network-interface" for item in result)
    assert not any(operation.startswith("delete") for _, operation, _ in api.calls)


def test_ssm_workers_run_with_fixed_nonroot_identity() -> None:
    resources = json.loads(template_body("control"))["Resources"]
    for name in ("RunDocument", "AppWorkerDocument"):
        command = resources[name]["Properties"]["Content"]["mainSteps"][0]["inputs"]["runCommand"][
            0
        ]["Fn::Sub"]
        assert command.startswith("/usr/sbin/runuser -u gsloadtest -- /usr/bin/env ")
        assert command.count("/usr/sbin/runuser") == 1
        assert "XDG_RUNTIME_DIR=/run/user/1001" in command
        assert "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus" in command


@pytest.mark.parametrize("document_name", ["RunDocument", "AppWorkerDocument"])
@pytest.mark.parametrize("started_after_minutes", [0, 30, 80])
def test_ssm_execution_timeout_covers_early_start_until_absolute_deadline(
    document_name: str, started_after_minutes: int
) -> None:
    resources = json.loads(template_body("control"))["Resources"]
    inputs = resources[document_name]["Properties"]["Content"]["mainSteps"][0]["inputs"]
    timeout = timedelta(seconds=int(inputs["timeoutSeconds"]))
    configuration = plan()
    started_at = configuration.created_at + timedelta(minutes=started_after_minutes)
    scheduled_start = configuration.created_at + timedelta(minutes=85)
    collection_limit = scheduled_start + timedelta(minutes=90)
    runtime_limit = configuration.expires_at - timedelta(seconds=30)
    # SSMはT0前の待機も実行時間に含むため、80分の試験時間だけでは不足する。
    assert started_at + timeout > collection_limit
    assert started_at + timeout >= runtime_limit


def test_sns_topic_policy_statement_ids_are_explicit_and_unique() -> None:
    resources = json.loads(template_body("control"))["Resources"]
    statements = resources["TopicPolicy"]["Properties"]["PolicyDocument"]["Statement"]
    identifiers = [statement.get("Sid") for statement in statements]
    assert identifiers
    assert all(isinstance(identifier, str) and identifier.strip() for identifier in identifiers)
    assert len(set(identifiers)) == len(identifiers)


def test_preparation_manifest_requires_pinned_images_and_exact_binding() -> None:
    from test_load_test_manifest import _manifest

    from quiz_app.load_test.controller_contracts import manifest_context
    from quiz_app.load_test_aws.lifecycle import require_manifest

    manifest = _manifest(run_id=RUN_ID, public_origin="https://10.77.1.10")
    body = manifest.model_dump_json().encode()
    configuration = AwsRunPlan.model_validate(
        {
            **plan().model_dump(),
            "manifest_sha256": manifest_context(manifest).manifest_sha256,
            "schema_revision": manifest.schema_revision,
        }
    )
    require_manifest(configuration, body)
    assert require_manifest(configuration, manifest.model_dump_json(indent=2).encode()) == manifest
    assert (
        require_manifest(configuration, json.dumps(json.loads(body), sort_keys=True).encode())
        == manifest
    )
    with pytest.raises(ValueError, match="binding_mismatch"):
        require_manifest(configuration.model_copy(update={"source_commit": "d" * 40}), body)
    payload = json.loads(body)
    payload["artifacts"]["api_image"] = "repository/api:latest"
    invalid_body = json.dumps(payload).encode()
    with pytest.raises(ValidationError):
        require_manifest(configuration, invalid_body)


@pytest.mark.parametrize("offset_seconds", [-1, 0, 30, 5_401, 6_000])
def test_manifest_start_outside_preparation_budget_refuses_before_aws(
    offset_seconds: int,
) -> None:
    from test_load_test_manifest import _manifest

    from quiz_app.load_test.controller_contracts import manifest_context
    from quiz_app.load_test_aws.lifecycle import preflight

    manifest = _manifest(
        run_id=RUN_ID,
        public_origin="https://10.77.1.10",
        created_at=NOW - timedelta(hours=1),
        scheduled_start_at=NOW + timedelta(seconds=offset_seconds),
    )
    configuration = plan().model_copy(
        update={
            "manifest_sha256": manifest_context(manifest).manifest_sha256,
            "schema_revision": manifest.schema_revision,
        }
    )
    api = FakeAws()
    with pytest.raises(ValueError, match="manifest_start_window_invalid"):
        preflight(api, configuration, now=NOW, manifest_bytes=manifest.model_dump_json().encode())
    assert api.calls == []


def test_manifest_rejection_happens_before_aws_identity_or_pricing() -> None:
    from quiz_app.load_test_aws.lifecycle import preflight

    api = FakeAws()
    with pytest.raises(ValidationError):
        preflight(api, plan(), now=NOW, manifest_bytes=b"{}")
    assert api.calls == []
