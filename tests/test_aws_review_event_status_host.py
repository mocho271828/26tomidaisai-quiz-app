"""開催参照が対象を厳密検証し、読み取りだけで公開可能な項目を返す。"""

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, Mock
from uuid import UUID, uuid4

import httpx
import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.engine import Row, RowMapping
from sqlalchemy.engine.result import SimpleResultMetaData
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session
from test_aws_review_host import inputs as inputs
from test_aws_review_host import release_directory as release_directory
from test_aws_review_host import release_event as release_event
from test_aws_review_host import secrets_fixture as secrets_fixture

from quiz_app.dependency_hosts import ValidatedDependencyHost
from quiz_app.models.enums import EventState
from validation.aws_review import event_status_host as status
from validation.aws_review import host, host_data
from validation.aws_review.host_contracts import HostInput, HostRefusedError
from validation.aws_review.host_update import ExistingHostConfiguration

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class StatusContext:
    request: status.EventStatusRequest
    result: status.EventStatusResult
    existing: ExistingHostConfiguration
    mocks: dict[str, Mock]
    protected_files: dict[Path, bytes]


@pytest.fixture
def context(
    inputs: HostInput,
    secrets_fixture: host_data.HostSecrets,
    release_directory: Path,
    release_event: host.RehearsalEventInput,
    monkeypatch: pytest.MonkeyPatch,
) -> StatusContext:
    settings = host_data.runtime_settings(inputs, secrets_fixture, release_event.event_id)
    host_data.write_runtime_file(settings, host.RUNTIME_FILE)
    existing = ExistingHostConfiguration(host._release(inputs, release_event), settings)
    request = status.EventStatusRequest.model_validate(
        inputs.model_dump(include={"account_id", "demo_id", "region", "instance_id"})
    )
    result = status.EventStatusResult(
        event_id=release_event.event_id,
        event_version=7,
        event_state=EventState.IN_PROGRESS,
        observed_at=datetime(2026, 10, 22, tzinfo=UTC),
        source_commit=inputs.source_commit,
    )
    mocks = {
        name: Mock(return_value=value)
        for name, value in (
            ("_read_host_input", inputs),
            ("require_ownership", None),
            ("validate_existing_host", existing),
            ("_require_release_source", None),
            ("_read_event", result),
        )
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(status, name, mock)
    mocks["command"] = Mock(
        side_effect=[
            "default",
            "ip-10-80-1-10.ap-northeast-1.compute.internal",
            "unix:///var/run/docker.sock",
        ]
    )
    mocks["compose"] = Mock()
    mocks["secrets"] = Mock()
    monkeypatch.setattr(host, "_command", mocks["command"])
    monkeypatch.setattr(host, "_compose", mocks["compose"])
    monkeypatch.setattr(host_data, "load_secrets", mocks["secrets"])
    monkeypatch.setattr(status.Path, "cwd", lambda: status.REPOSITORY_ROOT)
    monkeypatch.setattr(status.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(status.os, "getegid", lambda: 1000)
    protected = {
        path: path.read_bytes()
        for path in (host.RUNTIME_FILE, host.RELEASE_FILE, host.POSTGRES_CA_FILE)
    }
    return StatusContext(request, result, existing, mocks, protected)


def test_status_reads_existing_release_without_stopping_or_fetching_secrets(
    context: StatusContext,
) -> None:
    assert status.read_event_status(context.request) == context.result
    context.mocks["_read_event"].assert_called_once_with(
        context.existing.settings,
        context.existing.release.event_id,
        context.existing.release.source_commit,
    )
    context.mocks["_require_release_source"].assert_called_once_with(
        status.REPOSITORY_ROOT, context.result.source_commit
    )
    context.mocks["compose"].assert_not_called()
    context.mocks["secrets"].assert_not_called()
    assert [call.args[0][:2] for call in context.mocks["command"].call_args_list] == [
        ["/usr/bin/docker", "context"],
        ["/usr/bin/hostname", "-f"],
        ["/usr/bin/docker", "context"],
    ]
    for path, content in context.protected_files.items():
        assert path.read_bytes() == content


@pytest.mark.parametrize("invalid", ["root", "group", "directory"])
def test_execution_context_rejects_before_reading_configuration(
    context: StatusContext, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    if invalid == "directory":
        monkeypatch.setattr(status.Path, "cwd", lambda: Path("/other-checkout"))
    else:
        monkeypatch.setattr(status.os, "geteuid" if invalid == "root" else "getegid", lambda: 0)
    with pytest.raises(HostRefusedError, match="execution_context_invalid"):
        status.read_event_status(context.request)
    context.mocks["_read_host_input"].assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "999999999999"),
        ("demo_id", "other"),
        ("instance_id", "i-99999999999999999"),
    ],
)
def test_request_target_mismatch_fails_before_aws(
    context: StatusContext, field: str, value: str
) -> None:
    request = status.EventStatusRequest.model_validate(
        context.request.model_dump() | {field: value}
    )
    with pytest.raises(HostRefusedError, match="host_input_mismatch"):
        status.read_event_status(request)
    context.mocks["require_ownership"].assert_not_called()
    context.mocks["_read_event"].assert_not_called()


@pytest.mark.parametrize("field", ["source_commit", "api_image", "gateway_image"])
def test_release_mismatch_fails_before_database(context: StatusContext, field: str) -> None:
    release = context.existing.release.model_copy(update={field: "different"})
    context.mocks["validate_existing_host"].return_value = ExistingHostConfiguration(
        release, context.existing.settings
    )
    with pytest.raises(HostRefusedError, match="release_mismatch"):
        status.read_event_status(context.request)
    context.mocks["_read_event"].assert_not_called()


@pytest.mark.parametrize("event_id", [None, uuid4()])
def test_runtime_event_mismatch_fails_before_database(
    context: StatusContext, event_id: UUID | None
) -> None:
    settings = context.existing.settings.model_copy(update={"aws_demo_expected_event_id": event_id})
    context.mocks["validate_existing_host"].return_value = ExistingHostConfiguration(
        context.existing.release, settings
    )
    with pytest.raises(HostRefusedError, match="release_mismatch"):
        status.read_event_status(context.request)
    context.mocks["_read_event"].assert_not_called()


@pytest.mark.parametrize(
    "boundary", ["require_ownership", "validate_existing_host", "_require_release_source"]
)
def test_failed_validation_never_reads_database(context: StatusContext, boundary: str) -> None:
    context.mocks[boundary].side_effect = HostRefusedError("private-input")
    with pytest.raises(HostRefusedError):
        status.read_event_status(context.request)
    context.mocks["_read_event"].assert_not_called()
    context.mocks["compose"].assert_not_called()


def test_main_outputs_only_allowlisted_json(
    context: StatusContext, capsys: pytest.CaptureFixture[str]
) -> None:
    assert status.main(["--request-json", context.request.model_dump_json()]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [context.result.model_dump_json()]
    assert set(json.loads(captured.out)) == {
        "event_id",
        "event_version",
        "event_state",
        "observed_at",
        "source_commit",
    }
    assert context.existing.settings.database_url not in captured.out


@pytest.mark.parametrize(
    "invalid_request",
    ["secret-invalid-json", '{"account_id":"secret"}', '{"region":"us-east-1"}'],
)
def test_invalid_request_is_safely_rejected_before_host_access(
    context: StatusContext, capsys: pytest.CaptureFixture[str], invalid_request: str
) -> None:
    assert status.main(["--request-json", invalid_request]) == 2
    context.mocks["_read_host_input"].assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "secret" not in captured.err
    assert json.loads(captured.err)["error_type"] == "ValidationError"


@pytest.mark.parametrize(
    "error",
    [
        OSError("secret-database-url"),
        SQLAlchemyError("secret-database-url"),
        httpx.HTTPError("secret-database-url"),
        ClientError(
            {"Error": {"Code": "PrivateCode", "Message": "secret-database-url"}}, "PrivateOperation"
        ),
    ],
)
def test_failure_never_outputs_raw_error_or_credentials(
    context: StatusContext, capsys: pytest.CaptureFixture[str], error: Exception
) -> None:
    context.mocks["_read_event"].side_effect = error
    assert status.main(["--request-json", context.request.model_dump_json()]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "secret-database-url" not in captured.err
    assert "PrivateCode" not in captured.err
    assert "PrivateOperation" not in captured.err
    assert json.loads(captured.err)["code"] == "review_host_failed"


@pytest.mark.parametrize(
    "field,value",
    [
        ("event_version", True),
        ("event_version", "7"),
        ("event_version", 0),
        ("event_id", "not-a-uuid"),
        ("event_state", "Unknown"),
        ("observed_at", "2026-10-22T00:00:00"),
        ("observed_at", "2026-10-22T09:00:00+09:00"),
        ("source_commit", "secret"),
        ("unexpected", "secret"),
    ],
)
def test_result_rejects_invalid_or_extra_external_fields(
    context: StatusContext, field: str, value: object
) -> None:
    raw = context.result.model_dump(mode="json") | {field: value}
    with pytest.raises(ValidationError):
        status.EventStatusResult.model_validate_json(json.dumps(raw))


def _row(values: dict[str, object]) -> RowMapping:
    return Row(
        SimpleResultMetaData(list(values)),
        None,
        {key: index for index, key in enumerate(values)},
        tuple(values.values()),
    )._mapping


@pytest.mark.parametrize("invalid", [None, "missing", "multiple", "mismatch", "role", "writable"])
def test_database_uses_readonly_role_and_only_required_columns(
    inputs: HostInput,
    secrets_fixture: host_data.HostSecrets,
    monkeypatch: pytest.MonkeyPatch,
    invalid: str | None,
) -> None:
    event_id = uuid4()
    settings = host_data.runtime_settings(inputs, secrets_fixture, event_id)
    engine = Mock(spec=Engine)
    session = MagicMock(spec=Session)
    session.__enter__.return_value = session
    identity = {
        "database_name": "quiz",
        "current_role": "rehearsal_app",
        "session_role": "rehearsal_app",
        "read_only": "on",
    }
    if invalid == "role":
        identity["current_role"] = "quizadmin"
    elif invalid == "writable":
        identity["read_only"] = "off"
    event = _row(
        {
            "event_id": uuid4() if invalid == "mismatch" else event_id,
            "event_version": 7,
            "event_state": EventState.IN_PROGRESS,
        }
    )
    events = [] if invalid == "missing" else [event, event] if invalid == "multiple" else [event]
    identity_result, event_result = Mock(), Mock()
    identity_result.mappings.return_value.one.return_value = _row(identity)
    event_result.mappings.return_value = events
    session.execute.side_effect = [Mock(), Mock(), Mock(), identity_result, event_result]
    monkeypatch.setattr(status, "_create_readonly_engine", Mock(return_value=engine))
    monkeypatch.setattr(status, "Session", Mock(return_value=session))
    if invalid is None:
        result = status._read_event(settings, event_id, inputs.source_commit)
        assert (result.event_id, result.event_version, result.event_state) == (
            event_id,
            7,
            EventState.IN_PROGRESS,
        )
        assert result.observed_at.utcoffset().total_seconds() == 0
    else:
        with pytest.raises((HostRefusedError, ValidationError)):
            status._read_event(settings, event_id, inputs.source_commit)
    statements = [str(call.args[0]) for call in session.execute.call_args_list]
    assert statements[:3] == [
        "SET TRANSACTION READ ONLY",
        "SET LOCAL statement_timeout = '30s'",
        "SET LOCAL lock_timeout = '5s'",
    ]
    if invalid not in {"role", "writable"}:
        query = session.execute.call_args.args[0]
        assert [column.name for column in query.selected_columns] == [
            "event_id",
            "event_version",
            "event_state",
        ]
        assert "LIMIT" in str(query)
    session.commit.assert_not_called()
    session.add.assert_not_called()
    session.flush.assert_not_called()
    engine.dispose.assert_called_once()


def test_engine_pins_private_host_and_enforces_tls_with_finite_readonly_timeouts(
    inputs: HostInput, secrets_fixture: host_data.HostSecrets, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = ValidatedDependencyHost("database", inputs.rds_hostname, ("10.80.2.10",), "10.80.2.10")
    resolver = Mock(return_value=target)
    factory = Mock(spec=Engine)
    create = Mock(return_value=factory)
    monkeypatch.setattr(status, "validate_remote_dependency_host", resolver)
    monkeypatch.setattr(status, "create_engine", create)
    url = secrets_fixture.initialization.app_database_url.get_secret_value()
    assert status._create_readonly_engine(url) is factory
    resolver.assert_called_once_with("database", url)
    options = create.call_args.kwargs["connect_args"]
    assert options["host"] == inputs.rds_hostname
    assert options["hostaddr"] == "10.80.2.10"
    assert options["connect_timeout"] == 5
    assert options["sslmode"] == "verify-full"
    assert options["sslrootcert"] == str(status.POSTGRES_CA_FILE)
    assert options["options"] == (
        "-c default_transaction_read_only=on -c statement_timeout=30000 -c lock_timeout=5000"
    )


def test_embedded_source_executes_without_file_or_installed_new_module() -> None:
    source = (ROOT / "validation/aws_review/event_status_host.py").read_text()
    request = status.EventStatusRequest(
        account_id="123456789012", demo_id="review", instance_id="i-0123456789abcdef0"
    )
    # 本番と同じ-c形式で実行し、host以外の場所ではAWSへ触れる前に拒否する。
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-B", "-c", source, "--request-json", request.model_dump_json()],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    failure = json.loads(completed.stderr)
    assert failure["error_type"] == "HostRefusedError"
    assert failure["code"] == "review_host_failed"
