import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_aws_review_host import inputs as inputs
from test_aws_review_host import release_directory as release_directory
from test_aws_review_host import release_event as release_event
from test_aws_review_host import secrets_fixture as secrets_fixture

from quiz_app.config import Settings
from validation.aws_review import host, host_data, host_update
from validation.aws_review.host_contracts import HostInput, HostRefusedError

EXECUTION_HOST = "ip-10-80-1-10.ap-northeast-1.compute.internal"


@pytest.fixture
def existing_environment(
    inputs: HostInput,
    secrets_fixture: host_data.HostSecrets,
    release_directory: Path,
    release_event: host.RehearsalEventInput,
    monkeypatch: pytest.MonkeyPatch,
) -> Settings:
    previous_inputs = HostInput.model_validate(inputs.model_dump() | {"source_commit": "b" * 40})
    settings = host_data.runtime_settings(previous_inputs, secrets_fixture, release_event.event_id)
    host_data.write_runtime_file(settings, host.RUNTIME_FILE)
    host._release(previous_inputs, release_event)
    for field in ("RUNTIME_FILE", "RELEASE_FILE", "POSTGRES_CA_FILE", "BACKUP_DIRECTORY"):
        monkeypatch.setattr(host_update, field, getattr(host, field))
    return settings


def _validate(inputs: HostInput, **identity: str) -> host_update.ExistingHostConfiguration:
    return host_update.validate_existing_host(
        inputs,
        host.REPOSITORY_ROOT,
        execution_host=identity.get("execution_host", EXECUTION_HOST),
        docker_context=identity.get("docker_context", "default"),
        docker_endpoint=identity.get("docker_endpoint", "unix:///var/run/docker.sock"),
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("execution_host", "another.internal"),
        ("docker_context", "another"),
        ("docker_endpoint", "ssh://another.internal"),
    ],
)
def test_existing_host_validation_refuses_changed_execution_target(
    inputs: HostInput, existing_environment: Settings, field: str, value: str
) -> None:
    original_runtime = host.RUNTIME_FILE.read_bytes()
    original_release = host.RELEASE_FILE.read_bytes()
    with pytest.raises(HostRefusedError, match="review_update_release_identity_mismatch"):
        _validate(inputs, **{field: value})
    assert host.RUNTIME_FILE.read_bytes() == original_runtime
    assert host.RELEASE_FILE.read_bytes() == original_release


@pytest.mark.parametrize(
    "field,value",
    [
        ("rds_hostname", "other.abcdef.ap-northeast-1.rds.amazonaws.com"),
        ("redis_hostname", "other.0001.apne1.cache.amazonaws.com"),
        ("public_url", "https://different.cloudfront.net"),
    ],
)
def test_existing_host_validation_refuses_runtime_target_change_without_overwriting(
    inputs: HostInput, existing_environment: Settings, field: str, value: str
) -> None:
    original = host.RUNTIME_FILE.read_bytes()
    changed = HostInput.model_validate(inputs.model_dump() | {field: value})
    with pytest.raises(HostRefusedError, match="review_update_runtime_target_mismatch"):
        _validate(changed)
    assert host.RUNTIME_FILE.read_bytes() == original


def test_existing_host_validation_requires_release(
    inputs: HostInput, existing_environment: Settings
) -> None:
    host.RELEASE_FILE.unlink()
    with pytest.raises(host.AwsDemoDeploymentValidationError):
        _validate(inputs)


def test_compose_keeps_host_probe_overlay_without_dashboard_environment(
    inputs: HostInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = Mock(return_value="")
    monkeypatch.setattr(host, "_command", command)

    host._compose(inputs, ["config", "--quiet"])

    assert any(
        argument.endswith("compose.rehearsal-monitoring.yaml")
        for argument in command.call_args.args[0]
    )
    assert command.call_args.kwargs == {"private_ip": inputs.private_ip, "timeout": 300}


def test_command_does_not_forward_dashboard_environment_or_ambient_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="ready"))
    monkeypatch.setattr(host.subprocess, "run", run)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fixture-ambient-value")
    monkeypatch.setenv("AWS_REVIEW_SYSTEM_HEALTH_DASHBOARD_URL", "https://example.invalid")

    assert host._command(["docker", "compose"]) == "ready"

    environment = run.call_args.kwargs["env"]
    assert "AWS_REVIEW_SYSTEM_HEALTH_DASHBOARD_URL" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
