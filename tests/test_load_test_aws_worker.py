from __future__ import annotations

import base64
from typing import cast

import pytest
from test_load_test_aws import NOW, FakeAws, plan
from test_load_test_aws_alarms import _alarm, _prepare_api

from quiz_app.load_test_aws import lifecycle
from quiz_app.load_test_aws.aws import AwsCallError
from quiz_app.load_test_aws.models import AwsPreparationEvidence, SchemaReceipt
from quiz_app.load_test_aws.responses import Stack
from quiz_app.load_test_aws.templates import tags


@pytest.mark.parametrize("kind", ["control", "workload"])
@pytest.mark.parametrize("status", ["CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_IN_PROGRESS"])
def test_only_control_can_be_ready_after_definition_update(kind: str, status: str) -> None:
    configuration = plan()
    stack = Stack(
        StackId="arn:stack",
        StackName=getattr(configuration, f"{kind}_stack"),
        StackStatus=status,
        Tags=tags(configuration),
    )
    if status == "CREATE_COMPLETE" or (kind == "control" and status == "UPDATE_COMPLETE"):
        lifecycle.require_stack_ready(stack, configuration)
    else:
        with pytest.raises(ValueError, match="aws_load_test_stack_not_ready"):
            lifecycle.require_stack_ready(stack, configuration)


def worker_configuration() -> dict[str, object]:
    return {
        "FunctionName": f"{plan().prefix}-cleanup",
        "State": "Active",
        "CodeSha256": base64.b64encode(bytes.fromhex(plan().worker_package_sha256)).decode(),
        "Role": "arn:worker-role",
        "Handler": "quiz_app.load_test_aws.cleanup.handler",
        "Timeout": 120,
        "Version": "$LATEST",
        "Runtime": "python3.12",
        "PackageType": "Zip",
        "Architectures": ["x86_64"],
    }


def worker_api(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeAws, Stack]:
    api = _prepare_api(monkeypatch)
    control = lifecycle.find_stack(api, plan().control_stack)
    assert control is not None
    api.responses[("lambda", "get_function_configuration")] = worker_configuration()
    api.responses[("lambda", "get_function_concurrency")] = {}
    api.responses[("lambda", "get_account_settings")] = {
        "AccountLimit": {"UnreservedConcurrentExecutions": 10}
    }
    api.responses[("lambda", "list_provisioned_concurrency_configs")] = {
        "ProvisionedConcurrencyConfigs": []
    }
    api.responses[("cloudwatch", "describe_alarms")] = {"MetricAlarms": [_alarm()]}
    api.responses[("cloudformation", "create_stack")] = {"StackId": "arn:workload"}
    monkeypatch.setattr(
        lifecycle, "find_stack", lambda _, name: control if name == plan().control_stack else None
    )
    return api, control


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("FunctionName", "another-worker"),
        ("State", "Pending"),
        ("CodeSha256", "different-package"),
        ("Role", "arn:another-role"),
        ("Handler", "another.handler"),
        ("Timeout", 119),
        ("Timeout", 900),
        ("Timeout", "120"),
        ("Version", "1"),
        ("Runtime", "python3.13"),
        ("PackageType", "Image"),
        ("Architectures", ["arm64"]),
        ("Layers", [{"Arn": "arn:layer"}]),
        ("SnapStart", {"ApplyOn": "PublishedVersions"}),
        ("DurableConfig", {}),
        ("CapacityProviderConfig", {}),
    ],
)
def test_prepare_refuses_worker_drift_before_workload_creation(
    monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    api, _ = worker_api(monkeypatch)
    api.responses[("lambda", "get_function_configuration")] = {
        **worker_configuration(),
        field: value,
    }
    with pytest.raises(ValueError):
        lifecycle.prepare(api, plan(), b"worker", now=NOW, manifest_bytes=b"manifest")
    assert not any(
        operation in {"create_stack", "put_function_concurrency"} for _, operation, _ in api.calls
    )


@pytest.mark.parametrize("missing", ["Timeout", "Version"])
def test_prepare_cannot_assume_missing_configuration(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    api, _ = worker_api(monkeypatch)
    configuration = worker_configuration()
    configuration.pop(missing)
    api.responses[("lambda", "get_function_configuration")] = configuration
    with pytest.raises(ValueError):
        lifecycle.prepare(api, plan(), b"worker", now=NOW, manifest_bytes=b"manifest")
    assert not any(operation == "create_stack" for _, operation, _ in api.calls)


@pytest.mark.parametrize("reserved", [0, 1, 2, -1, "1", True])
def test_prepare_refuses_reservation_without_mutating_it(
    monkeypatch: pytest.MonkeyPatch, reserved: object
) -> None:
    api, _ = worker_api(monkeypatch)
    api.responses[("lambda", "get_function_concurrency")] = {
        "ReservedConcurrentExecutions": reserved
    }
    with pytest.raises(ValueError):
        lifecycle.prepare(api, plan(), b"worker", now=NOW, manifest_bytes=b"manifest")
    assert not any(
        operation in {"create_stack", "put_function_concurrency", "delete_function_concurrency"}
        for _, operation, _ in api.calls
    )


@pytest.mark.parametrize("unreserved", [1, 10, 1000])
def test_prepare_requires_positive_unreserved_pool_without_reserving_it(
    monkeypatch: pytest.MonkeyPatch, unreserved: int
) -> None:
    api, _ = worker_api(monkeypatch)
    api.responses[("lambda", "get_account_settings")] = {
        "AccountLimit": {"UnreservedConcurrentExecutions": unreserved}
    }
    result = lifecycle.prepare(api, plan(), b"worker", now=NOW, manifest_bytes=b"manifest")
    assert result.status == "workload_creating"
    operations = [operation for _, operation, _ in api.calls]
    assert operations[-2:] == ["get_account_settings", "create_stack"]
    assert "put_function_concurrency" not in operations
    assert "delete_function_concurrency" not in operations


@pytest.mark.parametrize(
    "observed",
    [
        {},
        {"AccountLimit": {}},
        {"AccountLimit": None},
        *(
            {"AccountLimit": {"UnreservedConcurrentExecutions": value}}
            for value in (None, 0, -1, "10", True, 1.5)
        ),
        AwsCallError("lambda", "get_account_settings", "AccessDenied"),
        AwsCallError("lambda", "get_account_settings", "ServiceException"),
    ],
)
def test_prepare_refuses_missing_exhausted_or_unreadable_unreserved_pool(
    monkeypatch: pytest.MonkeyPatch, observed: object
) -> None:
    api, _ = worker_api(monkeypatch)
    api.responses[("lambda", "get_account_settings")] = observed
    with pytest.raises((ValueError, AwsCallError)):
        lifecycle.prepare(api, plan(), b"worker", now=NOW, manifest_bytes=b"manifest")
    assert not any(
        operation in {"create_stack", "put_function_concurrency", "delete_function_concurrency"}
        for _, operation, _ in api.calls
    )


def preparation() -> AwsPreparationEvidence:
    configuration = plan()
    return AwsPreparationEvidence(
        run_id=configuration.run_id,
        account_id=configuration.account_id,
        source_commit=configuration.source_commit,
        manifest_sha256=configuration.manifest_sha256,
        app_instance_id="i-0123456789abcdef0",
        runner_instance_id="i-1123456789abcdef0",
        app_private_ip="10.77.1.10",
        runner_private_ip="10.77.1.11",
        public_origin="https://10.77.1.10",
        ca_sha256="d" * 64,
        prepared_at=NOW,
        expires_at=configuration.expires_at,
    )


@pytest.mark.parametrize(
    "drift",
    [
        None,
        "timeout",
        "package",
        "role",
        "closed",
        "reserved",
        "multiple",
        "read_denied",
        "no_unreserved",
        "account_read_denied",
    ],
)
def test_start_rechecks_worker_and_never_repairs_drift(
    monkeypatch: pytest.MonkeyPatch, drift: str | None
) -> None:
    api, control = worker_api(monkeypatch)
    prepared = preparation()
    control = Stack.model_validate(
        {
            **control.model_dump(by_alias=True),
            "Outputs": [
                *[item.model_dump(by_alias=True) for item in control.outputs],
                {"OutputKey": "RunDocumentName", "OutputValue": plan().prefix + "-run"},
            ],
        }
    )
    workload = Stack(
        StackId="arn:workload",
        StackName=plan().workload_stack,
        StackStatus="CREATE_COMPLETE",
        Tags=tags(plan()),
        Outputs=[
            {"OutputKey": "RunnerInstanceId", "OutputValue": prepared.runner_instance_id},
            {"OutputKey": "AppInstanceId", "OutputValue": prepared.app_instance_id},
            {"OutputKey": "RunnerPrivateIp", "OutputValue": str(prepared.runner_private_ip)},
            {"OutputKey": "AppPrivateIp", "OutputValue": str(prepared.app_private_ip)},
        ],
    )
    monkeypatch.setattr(
        lifecycle,
        "find_stack",
        lambda _, name: control if name == plan().control_stack else workload,
    )
    api.responses[("ssm", "send_command")] = {"Command": {"CommandId": "command-id"}}
    if drift in {"timeout", "package", "role"}:
        field, value = {
            "timeout": ("Timeout", 900),
            "package": ("CodeSha256", "drift"),
            "role": ("Role", "drift"),
        }[drift]
        cast(dict[str, object], api.responses[("lambda", "get_function_configuration")])[field] = (
            value
        )
    elif drift in {"closed", "reserved", "multiple"}:
        api.responses[("lambda", "get_function_concurrency")] = {
            "ReservedConcurrentExecutions": {"closed": 0, "reserved": 1, "multiple": 2}[drift]
        }
    elif drift == "read_denied":
        api.responses[("lambda", "get_function_configuration")] = AwsCallError(
            "lambda", "get_function_configuration", "AccessDenied"
        )
    elif drift == "no_unreserved":
        api.responses[("lambda", "get_account_settings")] = {
            "AccountLimit": {"UnreservedConcurrentExecutions": 0}
        }
    elif drift == "account_read_denied":
        api.responses[("lambda", "get_account_settings")] = AwsCallError(
            "lambda", "get_account_settings", "AccessDenied"
        )
    schema = SchemaReceipt(
        run_id=plan().run_id,
        account_id=plan().account_id,
        schema_revision=plan().schema_revision,
        verified_at=NOW,
        schema_applied_by_human=True,
        application_role_is_nonowner=True,
        verifier_role_is_read_only=True,
        single_event_count=1,
    )
    if drift is None:
        assert (
            lifecycle.start(
                api, plan(), prepared, schema, now=NOW, manifest_bytes=b"manifest"
            ).command.identifier
            == "command-id"
        )
        assert [operation for _, operation, _ in api.calls][-5:] == [
            "get_function_configuration",
            "list_provisioned_concurrency_configs",
            "get_function_concurrency",
            "get_account_settings",
            "send_command",
        ]
    else:
        with pytest.raises((ValueError, AwsCallError)):
            lifecycle.start(api, plan(), prepared, schema, now=NOW, manifest_bytes=b"manifest")
        assert not any(operation == "send_command" for _, operation, _ in api.calls)
    assert not any(
        operation in {"put_function_concurrency", "delete_function_concurrency", "create_function"}
        for _, operation, _ in api.calls
    )


@pytest.mark.parametrize(
    "observed",
    [
        {},
        {"ProvisionedConcurrencyConfigs": [{"FunctionArn": "arn:version"}]},
        {"NextMarker": "more-configurations"},
    ],
)
def test_billable_provisioned_versions_or_incomplete_readback_block_prepare(
    monkeypatch: pytest.MonkeyPatch, observed: dict[str, object]
) -> None:
    api, _ = worker_api(monkeypatch)
    api.responses[("lambda", "list_provisioned_concurrency_configs")] = observed
    with pytest.raises(ValueError):
        lifecycle.prepare(api, plan(), b"worker", now=NOW, manifest_bytes=b"manifest")
    assert not any(
        operation in {"create_stack", "put_function_concurrency"} for _, operation, _ in api.calls
    )
