from __future__ import annotations

import pytest
from test_load_test_aws import FakeAws, ownership, plan

from quiz_app.load_test_aws.aws import AwsCallError
from quiz_app.load_test_aws.identity_scan import scan_changed_identity
from quiz_app.load_test_aws.inventory import scan_databases
from quiz_app.load_test_aws.ownership import ResourceIdentity, RunLedger, read_ledger
from quiz_app.load_test_aws.storage import scan_control_storage


@pytest.mark.parametrize("alarm_type", ["MetricAlarms", "CompositeAlarms", "LogAlarms"])
def test_status_cannot_ignore_control_queue_or_any_alarm_type(alarm_type: str) -> None:
    api = FakeAws()
    run = plan().cleanup_input()
    queue_url = f"https://sqs.{run.region}.amazonaws.com/{run.account_id}/{run.prefix}-deadline-dlq"
    api.responses[("sqs", "get_queue_url")] = {"QueueUrl": queue_url}
    api.responses[("cloudwatch", "describe_alarms")] = {
        alarm_type: [{"AlarmName": f"{run.prefix}-deadline-dlq"}]
    }
    residuals = scan_control_storage(api, run)
    assert {item.kind for item in residuals} == {"control-alarm", "control-dlq"}
    alarm_arguments = next(
        arguments for _, operation, arguments in api.calls if operation == "describe_alarms"
    )
    assert alarm_arguments == {
        "AlarmNames": [f"{run.prefix}-deadline-dlq"],
        "AlarmTypes": ["MetricAlarm", "CompositeAlarm", "LogAlarm"],
    }
    assert api.calls[-1][2] == {
        "QueueName": f"{run.prefix}-deadline-dlq",
        "QueueOwnerAWSAccountId": run.account_id,
    }
    api.responses[("sqs", "get_queue_url")] = AwsCallError("sqs", "get_queue_url", "AccessDenied")
    with pytest.raises(AwsCallError, match="AccessDenied"):
        scan_control_storage(api, run)


def test_status_cannot_accept_partial_alarm_inventory_as_absent() -> None:
    api = FakeAws()
    api.responses[("cloudwatch", "describe_alarms")] = {"NextToken": "unexpected-page"}
    with pytest.raises(ValueError, match="alarm_inventory_incomplete"):
        scan_control_storage(api, plan().cleanup_input())


def test_root_volume_identity_is_recovered_from_known_instance_attachment() -> None:
    api = FakeAws()
    run = plan().cleanup_input()
    stack_id = f"arn:aws:cloudformation:{run.region}:{run.account_id}:stack/{run.workload_stack}/id"
    api.page_responses[("cloudformation", "list_stacks")] = [
        {
            "StackSummaries": [
                {
                    "StackId": stack_id,
                    "StackName": run.workload_stack,
                    "StackStatus": "DELETE_IN_PROGRESS",
                }
            ]
        }
    ]
    api.page_responses[("cloudformation", "list_stack_resources")] = [
        {
            "StackResourceSummaries": [
                {
                    "PhysicalResourceId": "i-0123456789abcdef0",
                    "ResourceType": "AWS::EC2::Instance",
                    "ResourceStatus": "DELETE_IN_PROGRESS",
                }
            ]
        }
    ]
    api.page_responses[("ec2", "describe_instances")] = [
        {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-0123456789abcdef0",
                            "State": {"Name": "shutting-down"},
                            "Tags": ownership(),
                            "BlockDeviceMappings": [
                                {"DeviceName": "/dev/sda1", "Ebs": {"VolumeId": "vol-root"}}
                            ],
                        }
                    ]
                }
            ]
        }
    ]
    ledger = read_ledger(api, run)
    assert ledger.identifiers("AWS::EC2::Volume") == frozenset({"vol-root"})
    assert api.calls[-1][2]["Filters"] == [
        {"Name": "instance-id", "Values": ["i-0123456789abcdef0"]}
    ]

    api.page_responses[("ec2", "describe_instances")] = []
    api.page_responses[("ec2", "describe_volumes")] = [
        {"Volumes": [{"VolumeId": "vol-root", "State": "available", "Tags": []}]}
    ]
    observed = scan_changed_identity(api, run, ledger)
    assert any(item.identifier == "vol-root" for item in observed)
    assert all(item.state == "ownership-unverified" for item in observed)
    assert not any(operation.startswith("delete") for _, operation, _ in api.calls)


def test_reserved_stack_tag_keeps_untagged_indirect_volume_as_residual() -> None:
    api = FakeAws()
    run = plan().cleanup_input()
    stack_id = "arn:aws:cloudformation:ap-northeast-1:123456789012:stack/run/id"
    api.page_responses[("ec2", "describe_volumes")] = [
        {
            "Volumes": [
                {
                    "VolumeId": "vol-orphaned-root",
                    "State": "available",
                    "Tags": [
                        {"Key": "aws:cloudformation:stack-id", "Value": stack_id},
                        {"Key": "aws:cloudformation:stack-name", "Value": run.workload_stack},
                    ],
                }
            ]
        }
    ]
    observed = scan_changed_identity(api, run, RunLedger(stack_ids=frozenset({stack_id})))
    assert [(item.kind, item.identifier, item.state) for item in observed] == [
        ("ebs-volume", "vol-orphaned-root", "ownership-unverified")
    ]
    assert api.calls == [
        (
            "ec2",
            "describe_volumes",
            {"Filters": [{"Name": "tag:aws:cloudformation:stack-id", "Values": [stack_id]}]},
        )
    ]


@pytest.mark.parametrize("has_tags", [False, True])
def test_retained_automated_backup_requires_real_response_tags_and_db_ledger(
    has_tags: bool,
) -> None:
    api = FakeAws()
    run = plan().cleanup_input()
    database_id = f"{run.prefix}-postgres"
    api.page_responses[("rds", "describe_db_instance_automated_backups")] = [
        {
            "DBInstanceAutomatedBackups": [
                {
                    "DBInstanceIdentifier": database_id,
                    "DbiResourceId": "db-owned",
                    "Status": "retained",
                    "TagList": ownership() if has_tags else [],
                }
            ]
        }
    ]
    ledger = RunLedger(
        resources=(ResourceIdentity(kind="AWS::RDS::DBInstance", identifier=database_id),)
    )
    if has_tags:
        observed = scan_databases(api, run, delete=True, ledger=ledger)
        assert observed[0].kind == "rds-automated-backup"
        assert [call for call in api.calls if call[1].startswith("delete")] == [
            ("rds", "delete_db_instance_automated_backup", {"DbiResourceId": "db-owned"})
        ]
    else:
        with pytest.raises(ValueError, match="backup_ownership_mismatch"):
            scan_databases(api, run, delete=True, ledger=ledger)
        assert not any(operation.startswith("delete") for _, operation, _ in api.calls)


def test_retained_backup_without_parent_ledger_is_never_deleted() -> None:
    api = FakeAws()
    run = plan().cleanup_input()
    api.page_responses[("rds", "describe_db_instance_automated_backups")] = [
        {
            "DBInstanceAutomatedBackups": [
                {
                    "DBInstanceIdentifier": f"{run.prefix}-postgres",
                    "DbiResourceId": "db-unverified",
                    "Status": "retained",
                    "TagList": ownership(),
                }
            ]
        }
    ]
    assert len(scan_databases(api, run, delete=True)) == 1
    assert not any(operation.startswith("delete") for _, operation, _ in api.calls)
