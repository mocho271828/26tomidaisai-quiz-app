"""初期化hostが対象不一致では停止せず、確定後の失敗では停止を維持することを検証する。"""

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlmodel import Session
from test_aws_review_host import inputs as inputs
from test_aws_review_host import release_directory as release_directory
from test_aws_review_host import release_event as release_event
from test_aws_review_host import secrets_fixture as secrets_fixture

from quiz_app.config import Settings
from validation.aws_review import host, host_data, host_reset
from validation.aws_review.host_contracts import HostInput, HostRefusedError
from validation.aws_review.host_update import ExistingHostConfiguration
from validation.aws_review.models import ResetInput
from validation.aws_review.reset_data import EventResetResult, ResetDeletedCounts


@dataclass
class ResetContext:
    request: ResetInput
    existing: ExistingHostConfiguration
    result: EventResetResult
    operations: list[str]
    mocks: dict[str, Mock]
    protected_files: dict[Path, bytes]


@pytest.fixture
def context(
    inputs: HostInput,
    secrets_fixture: host_data.HostSecrets,
    release_directory: Path,
    release_event: host.RehearsalEventInput,
    monkeypatch: pytest.MonkeyPatch,
) -> ResetContext:
    settings = host_data.runtime_settings(inputs, secrets_fixture, release_event.event_id)
    host_data.write_runtime_file(settings, host.RUNTIME_FILE)
    release = host._release(inputs, release_event)
    existing = ExistingHostConfiguration(release, settings)
    request = ResetInput(
        account_id=inputs.account_id,
        demo_id=inputs.demo_id,
        source_commit=inputs.source_commit,
        expected_event_id=release_event.event_id,
        expected_event_version=7,
        confirmation="RESET EVENT",
    )
    result = EventResetResult(
        event_id=request.expected_event_id,
        previous_version=request.expected_event_version,
        event_version=request.expected_event_version + 1,
        new_round_id=uuid4(),
        participant_ids_remaining=17_000,
        deleted=ResetDeletedCounts(
            participants=100,
            rounds=8,
            answers=800,
            score_ledgers=800,
            operation_audits=20,
            idempotency_records=20,
            admin_sessions=1,
        ),
    )
    operations: list[str] = []
    mocks: dict[str, Mock] = {}
    for name, label, value in (
        ("_require_release_source", "source", None),
        ("require_ownership", "ownership", None),
        ("validate_existing_host", "existing", existing),
        ("inspect_reset_event", "inspect", None),
        ("load_secrets", "secrets", secrets_fixture),
        ("capture_reset_archive", "capture", Mock()),
        ("write_reset_archive", "archive", "a" * 64),
    ):

        def observe(*_: object, label: str = label, value: object = value, **__: object) -> object:
            operations.append(label)
            return value

        mocks[name] = Mock(side_effect=observe)
        monkeypatch.setattr(host_reset, name, mocks[name])

    def command(argv: list[str], **_: object) -> str:
        if argv[0] == "/bin/bash":
            operations.append("preflight")
            return "ready"
        if argv[:3] == ["/usr/bin/docker", "context", "show"]:
            return "default"
        if argv[0] == "/usr/bin/hostname":
            return "ip-10-80-1-10.ap-northeast-1.compute.internal"
        return "unix:///var/run/docker.sock"

    def compose(_inputs: HostInput, arguments: list[str], **_: object) -> str:
        operations.append(arguments[0])
        if arguments[0] != "exec":
            return ""
        if arguments[-1].endswith("/live"):
            return json.dumps(
                {"status": "ok", "service": "quiz-api", "version": inputs.source_commit}
            )
        return json.dumps(
            {"status": "ready", "database": "ok", "migration": "current", "redis": "ok"}
        )

    def reset_data(
        _settings: Settings,
        _secrets: host_data.HostSecrets,
        _event_id: UUID,
        _version: int,
        *,
        reset_id: UUID,
        before_delete: Callable[[Session], None],
        postgres_ca: Path,
    ) -> EventResetResult:
        operations.append("transaction")
        before_delete(Mock(spec=Session))
        operations.append("delete-commit")
        return result

    mocks["command"] = Mock(side_effect=command)
    mocks["compose"] = Mock(side_effect=compose)
    mocks["reset_event_data"] = Mock(side_effect=reset_data)
    monkeypatch.setattr(host, "_command", mocks["command"])
    monkeypatch.setattr(host, "_compose", mocks["compose"])
    monkeypatch.setattr(host_reset, "reset_event_data", mocks["reset_event_data"])
    monkeypatch.setattr(host_reset, "_read_input", lambda _: inputs.model_dump_json().encode())
    monkeypatch.setattr(host_reset, "REPOSITORY_ROOT", Path("/opt/gs-app-tomidaisai"))
    monkeypatch.setattr(host_reset.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(host_reset.os, "getegid", lambda: 1000)
    for field in ("RUNTIME_FILE", "RELEASE_FILE", "POSTGRES_CA_FILE", "BACKUP_DIRECTORY"):
        monkeypatch.setattr(host_reset, field, getattr(host, field))
    protected = {
        file: file.read_bytes()
        for file in (host.RUNTIME_FILE, host.RELEASE_FILE, host.POSTGRES_CA_FILE)
    }
    return ResetContext(request, existing, result, operations, mocks, protected)


def test_reset_preserves_release_runtime_and_checks_both_apis_after_commit(
    context: ResetContext, capsys: pytest.CaptureFixture[str]
) -> None:
    host_reset.reset_host(context.request)
    assert context.operations == [
        "source",
        "ownership",
        "existing",
        "inspect",
        "secrets",
        "config",
        "stop",
        "transaction",
        "capture",
        "archive",
        "delete-commit",
        "preflight",
        "up",
        "exec",
        "exec",
        "exec",
        "exec",
    ]
    for path, contents in context.protected_files.items():
        assert path.read_bytes() == contents
    commands = [call.args[1] for call in context.mocks["compose"].call_args_list]
    assert commands[1] == ["stop", "--timeout", "30", "gateway", "api", "api-secondary"]
    assert commands[2] == [
        "up",
        "-d",
        "--no-build",
        "--pull",
        "never",
        "--wait",
        "--wait-timeout",
        "180",
        "api",
        "api-secondary",
        "gateway",
    ]
    assert [arguments[-1] for arguments in commands[3:]] == [
        f"http://{service}:8000/api/health/{probe}"
        for service in ("api", "api-secondary")
        for probe in ("live", "ready")
    ]
    payload = json.loads(capsys.readouterr().out)
    assert payload["event_version"] == context.request.expected_event_version + 1
    assert context.existing.settings.session_secret.get_secret_value() not in json.dumps(payload)
    assert context.mocks["write_reset_archive"].call_args.kwargs["encryption_key"] == (
        context.existing.settings.round_backup_encryption_key
    )


@pytest.mark.parametrize(
    "field,value",
    [("source_commit", "b" * 40), ("account_id", "999999999999"), ("demo_id", "other")],
)
def test_request_mismatch_rejects_before_aws_or_service_stop(
    context: ResetContext, field: str, value: str
) -> None:
    request = ResetInput.model_validate(context.request.model_dump() | {field: value})
    with pytest.raises(HostRefusedError, match="review_reset_host_input_mismatch"):
        host_reset.reset_host(request)
    context.mocks["require_ownership"].assert_not_called()
    context.mocks["compose"].assert_not_called()
    context.mocks["reset_event_data"].assert_not_called()


@pytest.mark.parametrize("field", ["source_commit", "event_id", "api_image", "gateway_image"])
def test_release_mismatch_rejects_before_database_or_service_stop(
    context: ResetContext, field: str
) -> None:
    values = context.existing.release.model_dump()
    if field == "event_id":
        values[field] = uuid4()
    elif field == "source_commit":
        values[field] = "b" * 40
    else:
        values[field] = str(values[field]).split("@", 1)[0] + "@sha256:" + "c" * 64
    altered = context.existing.release.model_copy(update={field: values[field]})
    context.mocks["validate_existing_host"].side_effect = None
    context.mocks["validate_existing_host"].return_value = ExistingHostConfiguration(
        altered, context.existing.settings
    )
    with pytest.raises(HostRefusedError, match="review_reset_release_mismatch"):
        host_reset.reset_host(context.request)
    context.mocks["inspect_reset_event"].assert_not_called()
    context.mocks["compose"].assert_not_called()


@pytest.mark.parametrize(
    "boundary",
    [
        "_require_release_source",
        "require_ownership",
        "validate_existing_host",
        "inspect_reset_event",
        "load_secrets",
    ],
)
def test_preflight_boundary_failure_leaves_running_services_untouched(
    context: ResetContext, boundary: str
) -> None:
    context.mocks[boundary].side_effect = HostRefusedError("private-preflight-input")
    with pytest.raises(HostRefusedError):
        host_reset.reset_host(context.request)
    context.mocks["compose"].assert_not_called()
    context.mocks["reset_event_data"].assert_not_called()


@pytest.mark.parametrize(
    "field", ["database_url", "redis_url", "session_secret", "round_backup_encryption_key"]
)
def test_changed_runtime_credentials_refuse_before_stop(context: ResetContext, field: str) -> None:
    value: str | SecretStr = "private-changed-value"
    if field in {"session_secret", "round_backup_encryption_key"}:
        value = SecretStr(value)
    settings = context.existing.settings.model_copy(update={field: value})
    context.mocks["validate_existing_host"].side_effect = None
    context.mocks["validate_existing_host"].return_value = ExistingHostConfiguration(
        context.existing.release, settings
    )
    with pytest.raises(HostRefusedError, match="review_reset_credentials_mismatch"):
        host_reset.reset_host(context.request)
    context.mocks["compose"].assert_not_called()
    context.mocks["reset_event_data"].assert_not_called()


def test_archive_failure_prevents_deletion_and_keeps_services_stopped(
    context: ResetContext,
) -> None:
    context.mocks["write_reset_archive"].side_effect = OSError("archive-disk-full")
    with pytest.raises(OSError):
        host_reset.reset_host(context.request)
    assert "delete-commit" not in context.operations
    assert "preflight" not in context.operations and "up" not in context.operations
    assert context.operations[-1] == "stop"
    assert context.operations.count("stop") == 2


def test_post_commit_preflight_failure_does_not_restart_or_repeat_reset(
    context: ResetContext,
) -> None:
    context.mocks["command"].side_effect = [
        "default",
        "ip-10-80-1-10.ap-northeast-1.compute.internal",
        "unix:///var/run/docker.sock",
        subprocess.CalledProcessError(1, "preflight"),
    ]
    with pytest.raises(subprocess.CalledProcessError):
        host_reset.reset_host(context.request)
    assert "delete-commit" in context.operations and "up" not in context.operations
    assert context.operations[-1] == "stop"
    context.mocks["reset_event_data"].assert_called_once()


@pytest.mark.parametrize("service", ["api", "api-secondary"])
def test_either_unready_api_keeps_gateway_and_both_apis_stopped(
    context: ResetContext, service: str
) -> None:
    original = context.mocks["compose"].side_effect
    assert callable(original)

    def compose(inputs: HostInput, arguments: list[str], **kwargs: object) -> str:
        result = original(inputs, arguments, **kwargs)
        if arguments[-1] == f"http://{service}:8000/api/health/ready":
            return json.dumps(
                {
                    "status": "degraded",
                    "database": "ok",
                    "migration": "current",
                    "redis": "degraded",
                }
            )
        return result

    context.mocks["compose"].side_effect = compose
    with pytest.raises(HostRefusedError, match="review_api_readiness_failed"):
        host_reset.reset_host(context.request)
    assert context.operations[-1] == "stop"
    context.mocks["reset_event_data"].assert_called_once()


def test_cli_failure_does_not_expose_input_or_raw_exception(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(host_reset, "configure_logging", Mock())
    monkeypatch.setattr(host_reset, "_read_input", Mock(side_effect=OSError("private-secret")))
    run = Mock()
    monkeypatch.setattr(host_reset, "reset_host", run)
    assert host_reset.main(["--reset-input", "/private-input"]) == 2
    assert "private-" not in str(capsys.readouterr())
    run.assert_not_called()
