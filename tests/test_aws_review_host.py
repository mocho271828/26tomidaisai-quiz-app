import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from quiz_app.dependency_hosts import ValidatedDependencyHost
from quiz_app.rehearsal.content import load_rehearsal_content
from validation.aws_review import host, host_contracts, host_data
from validation.aws_review.host_contracts import AwsClient, HostInput, HostRefusedError

ACCOUNT = "123456789012"
STACK_ID = (
    f"arn:aws:cloudformation:ap-northeast-1:{ACCOUNT}:stack/gs-rehearsal-review-workload/"
    "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
)
PREFIX = f"arn:aws:secretsmanager:ap-northeast-1:{ACCOUNT}:secret:"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def inputs() -> HostInput:
    repository = f"{ACCOUNT}.dkr.ecr.ap-northeast-1.amazonaws.com/gs-rehearsal-review"
    return HostInput(
        account_id=ACCOUNT,
        demo_id="review",
        source_commit="a1b2c3d4" * 5,
        api_image=f"{repository}/api@sha256:{'a1b2c3d4' * 8}",
        gateway_image=f"{repository}/gateway@sha256:{'e5f6a7b8' * 8}",
        instance_id="i-0123456789abcdef0",
        private_ip="10.80.1.10",
        public_url="https://d123example.cloudfront.net",
        database_identifier="review-database",
        redis_identifier="gs-rehearsal-review-redis",
        rds_hostname="review.abcdef.ap-northeast-1.rds.amazonaws.com",
        redis_hostname="review.0001.apne1.cache.amazonaws.com",
        database_master_secret_arn=PREFIX + "rds!db-1234-AbCdEf",
        redis_auth_secret_arn=PREFIX + "gs-rehearsal-review-redis-AbCdEf",
        runtime_secret_arn=PREFIX + "gs-rehearsal-review-runtime-AbCdEf",
    )


@pytest.fixture
def secrets_fixture(inputs: HostInput, monkeypatch: pytest.MonkeyPatch) -> host_data.HostSecrets:
    values = {
        inputs.database_master_secret_arn: {
            "username": "quizadmin",
            "password": "Qz8t3A6p9B2r5C7x4N1m6L9k",
        },
        inputs.redis_auth_secret_arn: {"token": "Zx8t3A6p9B2r5C7x4N1m6L9k"},
        inputs.runtime_secret_arn: {
            "session_secret": "Az8t3A6p9B2r5C7x4N1m6L9kQ2w7E5y0R4u8I6o3P9s1D5f7G2h4J6k8L0m2N4p6Q"
        },
    }
    client = Mock(spec=AwsClient)
    client.get_secret_value.side_effect = lambda **kwargs: {
        "ARN": kwargs["SecretId"],
        "SecretString": json.dumps(values[kwargs["SecretId"]]),
    }
    monkeypatch.setattr(host_data, "aws_client", lambda _: client)
    return host_data.load_secrets(inputs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("demo_id", "bad--name"),
        ("demo_id", "x" * 22),
        ("private_ip", "127.0.0.1"),
        ("private_ip", "169.254.169.254"),
        ("public_url", "https://example.com"),
        ("region", "us-east-1"),
        ("api_image", "nginx:latest"),
        ("redis_identifier", "gs-rehearsal-another-redis"),
        ("runtime_secret_arn", PREFIX + "gs-rehearsal-other-runtime-AbCdEf"),
    ],
)
def test_host_input_rejects_targets_outside_dedicated_environment(
    inputs: HostInput, field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        HostInput.model_validate(inputs.model_dump() | {field: value})


def test_secrets_are_distinct_stable_and_produce_valid_runtime_configuration(
    inputs: HostInput, secrets_fixture: host_data.HostSecrets
) -> None:
    settings = host_data.runtime_settings(inputs, secrets_fixture, uuid4())
    values = {
        make_url(settings.database_url).password,
        secrets_fixture.initialization.operator_password.get_secret_value(),
        settings.session_secret.get_secret_value(),
        settings.round_backup_encryption_key.get_secret_value(),
    }
    assert len(values) == 4
    assert make_url(settings.database_url).username == "rehearsal_app"
    assert make_url(settings.database_url).database == "quiz"
    assert make_url(settings.database_url).query == {"sslmode": "verify-full"}
    assert settings.redis_url.startswith("rediss://:")
    assert settings.session_cookie_secure and not settings.enable_demo_seed
    assert host_data.load_secrets(inputs) == secrets_fixture
    assert all(value not in repr(secrets_fixture) for value in values if value is not None)


def test_secret_response_arn_must_match_requested_resource(inputs: HostInput) -> None:
    client = Mock(spec=AwsClient)
    client.get_secret_value.return_value = {"ARN": "wrong", "SecretString": "private-answer"}
    with pytest.raises(HostRefusedError, match="review_secret_response_mismatch"):
        host_contracts.read_runtime_secret(cast(AwsClient, client), inputs.runtime_secret_arn)


@pytest.fixture
def ownership_clients(inputs: HostInput, monkeypatch: pytest.MonkeyPatch) -> dict[str, Mock]:
    tags = [
        {"Key": "DemoId", "Value": "review"},
        {"Key": "Purpose", "Value": "quiz-rehearsal"},
        {"Key": "aws:cloudformation:stack-name", "Value": "gs-rehearsal-review-workload"},
    ]
    clients = {
        name: Mock(spec=AwsClient)
        for name in ("ec2", "rds", "elasticache", "secretsmanager", "cloudformation")
    }
    clients["ec2"].describe_instances.return_value = {
        "Reservations": [
            {
                "OwnerId": ACCOUNT,
                "Instances": [
                    {
                        "InstanceId": inputs.instance_id,
                        "PrivateIpAddress": inputs.private_ip,
                        "VpcId": "vpc-123",
                        "Tags": [*tags, {"Key": "aws:cloudformation:stack-id", "Value": STACK_ID}],
                    }
                ],
            }
        ]
    }
    clients["rds"].describe_db_instances.return_value = {
        "DBInstances": [
            {
                "DBInstanceIdentifier": inputs.database_identifier,
                "DBInstanceArn": (
                    f"arn:aws:rds:ap-northeast-1:{ACCOUNT}:db:{inputs.database_identifier}"
                ),
                "DBName": "quiz",
                "MasterUsername": "quizadmin",
                "DBInstanceStatus": "available",
                "PubliclyAccessible": False,
                "Endpoint": {"Address": inputs.rds_hostname, "Port": 5432},
                "MasterUserSecret": {"SecretArn": inputs.database_master_secret_arn},
                "DBSubnetGroup": {"VpcId": "vpc-123"},
                "TagList": tags,
            }
        ]
    }
    clients["elasticache"].describe_replication_groups.return_value = {
        "ReplicationGroups": [
            {
                "ReplicationGroupId": inputs.redis_identifier,
                "ARN": (
                    f"arn:aws:elasticache:ap-northeast-1:{ACCOUNT}:replicationgroup:"
                    f"{inputs.redis_identifier}"
                ),
                "Status": "available",
                "TransitEncryptionEnabled": True,
                "AuthTokenEnabled": True,
                "NodeGroups": [
                    {"PrimaryEndpoint": {"Address": inputs.redis_hostname, "Port": 6379}}
                ],
            }
        ]
    }
    clients["elasticache"].list_tags_for_resource.return_value = {"TagList": tags}
    clients["cloudformation"].describe_stack_resource.return_value = {
        "StackResourceDetail": {
            "StackId": STACK_ID,
            "StackName": "gs-rehearsal-review-workload",
            "LogicalResourceId": "Redis",
            "PhysicalResourceId": inputs.redis_identifier,
            "ResourceType": "AWS::ElastiCache::ReplicationGroup",
            "ResourceStatus": "CREATE_COMPLETE",
        }
    }
    clients["secretsmanager"].describe_secret.side_effect = lambda **kwargs: {
        "ARN": kwargs["SecretId"],
        "Tags": tags,
    }
    monkeypatch.setattr(host_contracts, "aws_client", clients.__getitem__)
    http_client = MagicMock()
    http_client.__enter__.return_value = http_client
    http_client.put.return_value = httpx.Response(
        200,
        text="imds-token",
        request=httpx.Request("PUT", "http://169.254.169.254/latest/api/token"),
    )
    http_client.get.return_value = httpx.Response(
        200,
        json={
            "accountId": ACCOUNT,
            "instanceId": inputs.instance_id,
            "privateIp": inputs.private_ip,
            "region": inputs.region,
        },
        request=httpx.Request(
            "GET", "http://169.254.169.254/latest/dynamic/instance-identity/document"
        ),
    )
    monkeypatch.setattr(host_contracts.httpx, "Client", lambda **_: http_client)
    return clients


def test_owned_ec2_rds_redis_and_secrets_are_required_before_reading_secrets(
    inputs: HostInput, ownership_clients: dict[str, Mock]
) -> None:
    host_contracts.require_ownership(inputs)
    ownership_clients["secretsmanager"].get_secret_value.assert_not_called()


@pytest.mark.parametrize("status", ["CREATE_COMPLETE", "UPDATE_COMPLETE"])
def test_redis_without_automatic_stack_tags_still_requires_stack_ownership(
    inputs: HostInput, ownership_clients: dict[str, Mock], status: str
) -> None:
    response = ownership_clients["cloudformation"].describe_stack_resource.return_value
    response["StackResourceDetail"]["ResourceStatus"] = status
    ownership_clients["elasticache"].list_tags_for_resource.return_value = {
        "TagList": [
            {"Key": "DemoId", "Value": inputs.demo_id},
            {"Key": "Purpose", "Value": "quiz-rehearsal"},
        ]
    }
    host_contracts.require_ownership(inputs)
    ownership_clients["cloudformation"].describe_stack_resource.assert_called_once_with(
        StackName=STACK_ID, LogicalResourceId="Redis"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("StackId", STACK_ID.replace("eeeeeeeeeeee", "ffffffffffff")),
        ("StackName", "gs-rehearsal-another-workload"),
        ("LogicalResourceId", "AnotherRedis"),
        ("PhysicalResourceId", "gs-rehearsal-another-redis"),
        ("ResourceType", "AWS::ElastiCache::CacheCluster"),
        ("ResourceStatus", "DELETE_COMPLETE"),
        ("ResourceStatus", "CREATE_IN_PROGRESS"),
        ("ResourceStatus", "UPDATE_ROLLBACK_COMPLETE"),
        ("PhysicalResourceId", None),
    ],
)
def test_redis_stack_mismatch_prevents_secret_access(
    inputs: HostInput, ownership_clients: dict[str, Mock], field: str, value: object
) -> None:
    response = ownership_clients["cloudformation"].describe_stack_resource.return_value
    response["StackResourceDetail"][field] = value
    with pytest.raises((HostRefusedError, ValidationError)):
        host_contracts.require_ownership(inputs)
    ownership_clients["secretsmanager"].describe_secret.assert_not_called()


@pytest.mark.parametrize(
    "stack_id",
    [
        "",
        STACK_ID.replace(ACCOUNT, "999999999999"),
        STACK_ID.replace("ap-northeast-1", "ap-northeast-2"),
        STACK_ID.replace("review-workload/", "review-workload-other/"),
        STACK_ID + "/another",
    ],
)
def test_untrusted_ec2_stack_id_is_rejected_before_cloudformation(
    inputs: HostInput, ownership_clients: dict[str, Mock], stack_id: str
) -> None:
    instances = ownership_clients["ec2"].describe_instances.return_value["Reservations"][0]
    instances["Instances"][0]["Tags"][-1]["Value"] = stack_id
    with pytest.raises(HostRefusedError, match="review_instance_stack_identity_mismatch"):
        host_contracts.require_ownership(inputs)
    ownership_clients["cloudformation"].describe_stack_resource.assert_not_called()
    ownership_clients["secretsmanager"].describe_secret.assert_not_called()


@pytest.mark.parametrize(
    "key,value",
    [
        ("DemoId", "wrong"),
        ("Purpose", "wrong"),
        ("DemoId", None),
        ("Purpose", None),
        ("aws:cloudformation:stack-name", "wrong"),
    ],
)
def test_redis_still_refuses_incorrect_ownership_tags(
    inputs: HostInput, ownership_clients: dict[str, Mock], key: str, value: str | None
) -> None:
    tags = ownership_clients["elasticache"].list_tags_for_resource.return_value["TagList"]
    ownership_clients["elasticache"].list_tags_for_resource.return_value = {
        "TagList": [tag for tag in tags if tag["Key"] != key]
        + ([{"Key": key, "Value": value}] if value is not None else [])
    }
    with pytest.raises(HostRefusedError, match="review_resource_ownership_mismatch"):
        host_contracts.require_ownership(inputs)
    ownership_clients["cloudformation"].describe_stack_resource.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [("TransitEncryptionEnabled", False), ("AuthTokenEnabled", False), ("ARN", "wrong")],
)
def test_redis_still_requires_tls_authentication_and_exact_arn(
    inputs: HostInput, ownership_clients: dict[str, Mock], field: str, value: object
) -> None:
    response = ownership_clients["elasticache"].describe_replication_groups.return_value
    response["ReplicationGroups"][0][field] = value
    with pytest.raises((HostRefusedError, ValidationError)):
        host_contracts.require_ownership(inputs)
    ownership_clients["cloudformation"].describe_stack_resource.assert_not_called()


@pytest.mark.parametrize(
    "mismatch", ["tags", "database-host", "vpc", "master-secret", "redis-host"]
)
def test_ownership_refuses_mismatched_targets(
    inputs: HostInput, ownership_clients: dict[str, Mock], mismatch: str
) -> None:
    database = ownership_clients["rds"].describe_db_instances.return_value["DBInstances"][0]
    if mismatch == "tags":
        database["TagList"] = []
    elif mismatch == "database-host":
        database["Endpoint"]["Address"] = "other.ap-northeast-1.rds.amazonaws.com"
    elif mismatch == "vpc":
        database["DBSubnetGroup"]["VpcId"] = "vpc-other"
    elif mismatch == "master-secret":
        database["MasterUserSecret"]["SecretArn"] = "wrong"
    else:
        ownership_clients["elasticache"].describe_replication_groups.return_value[
            "ReplicationGroups"
        ][0]["NodeGroups"][0]["PrimaryEndpoint"]["Address"] = "wrong"
    with pytest.raises(HostRefusedError):
        host_contracts.require_ownership(inputs)
    ownership_clients["secretsmanager"].get_secret_value.assert_not_called()


def _database_cursor(monkeypatch: pytest.MonkeyPatch, rows: list[object]) -> tuple[Mock, Mock]:
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.side_effect = rows
    connection.cursor.return_value = cursor
    connect = Mock(return_value=connection)
    monkeypatch.setattr(host_data.psycopg, "connect", connect)
    monkeypatch.setattr(
        host_data,
        "validate_remote_dependency_host",
        lambda *_: ValidatedDependencyHost(
            "database", "review.ap-northeast-1.rds.amazonaws.com", ("10.80.2.20",), "10.80.2.20"
        ),
    )
    return cursor, connect


def test_reset_uses_pinned_tls_and_drops_only_public_schema_and_owned_app_role(
    secrets_fixture: host_data.HostSecrets, monkeypatch: pytest.MonkeyPatch
) -> None:
    cursor, connect = _database_cursor(
        monkeypatch,
        [
            {"database_name": "quiz", "current_role": "quizadmin", "session_role": "quizadmin"},
            {"present": False},
            {"present": False},
            {"present": True},
        ],
    )
    host_data.reset_dedicated_database(secrets_fixture)
    assert connect.call_args.kwargs["sslmode"] == "verify-full"
    assert connect.call_args.kwargs["hostaddr"] == "10.80.2.20"
    assert connect.call_args.kwargs["dbname"] == "quiz"
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert "DROP SCHEMA public CASCADE" in statements
    assert "CREATE SCHEMA public AUTHORIZATION quizadmin" in statements
    assert statements[-2].as_string() == 'DROP OWNED BY "rehearsal_app"'
    assert statements[-1].as_string() == 'DROP ROLE "rehearsal_app"'


@pytest.mark.parametrize("refusal", ["identity", "active-app", "custom-schema"])
def test_reset_refuses_before_any_drop_on_unexpected_database_state(
    secrets_fixture: host_data.HostSecrets, monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    identity = {"database_name": "quiz", "current_role": "quizadmin", "session_role": "quizadmin"}
    if refusal == "identity":
        identity["database_name"] = "production"
    cursor, _ = _database_cursor(
        monkeypatch,
        [identity, {"present": refusal == "active-app"}, {"present": refusal == "custom-schema"}],
    )
    with pytest.raises((ValidationError, HostRefusedError)):
        host_data.reset_dedicated_database(secrets_fixture)
    assert all("DROP" not in str(call.args[0]) for call in cursor.execute.call_args_list)


@pytest.mark.parametrize("deployment_mode", ["create", "update"])
def test_deploy_orders_ownership_stop_reset_initialization_preflight_and_start(
    inputs: HostInput,
    secrets_fixture: host_data.HostSecrets,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    deployment_mode: str,
) -> None:
    values = inputs.model_dump() | {"deployment_mode": deployment_mode}
    if deployment_mode == "create":
        values["monitoring_expires_at"] = datetime.now(UTC) + timedelta(hours=6)
    inputs = HostInput.model_validate(values)
    content = load_rehearsal_content(ROOT / "content/event-questions.json")
    operations: list[str] = []
    monkeypatch.setattr(host, "REPOSITORY_ROOT", Path("/opt/gs-app-tomidaisai"))
    monkeypatch.setattr(host.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(host.os, "getegid", lambda: 1000)
    monkeypatch.setattr(host, "_require_release_source", lambda *_: operations.append("source"))
    monkeypatch.setattr(host, "load_rehearsal_content", lambda _: content)
    monkeypatch.setattr(host, "require_ownership", lambda _: operations.append("ownership"))
    retired_update_files = tuple(tmp_path / f"retired-{index}.env" for index in range(4))
    for path in retired_update_files:
        path.write_text("SECRET=retired\n")
    monkeypatch.setattr(host, "RETIRED_UPDATE_FILES", retired_update_files)
    monkeypatch.setattr(host, "load_secrets", lambda _: secrets_fixture)
    monkeypatch.setattr(host, "write_runtime_file", lambda _: None)
    monkeypatch.setattr(host, "_release", lambda *_: Mock())
    monkeypatch.setattr(host, "validate_runtime_settings", lambda *_: None)
    writes: dict[str, str] = {}
    monkeypatch.setattr(
        host, "write_private_file", lambda path, body: writes.update({path.name: body})
    )
    monkeypatch.setattr(host, "_compose", lambda _, arguments: operations.append(arguments[0]))
    monkeypatch.setattr(host, "reset_dedicated_database", lambda _: operations.append("reset"))
    monkeypatch.setattr(
        host, "initialize_rehearsal_database", lambda *_, **__: operations.append("initialize")
    )
    monkeypatch.setattr(
        host, "prepare_rehearsal_event", lambda *_, **__: operations.append("prepare")
    )
    monkeypatch.setattr(
        host, "prepare_review_participant_ids", lambda *_, **__: operations.append("id-pool")
    )
    monkeypatch.setattr(host, "_verify_api_readiness", lambda _: operations.append("ready"))
    monkeypatch.setattr(host, "_command", lambda *_, **__: operations.append("preflight"))
    host.deploy_host(inputs)
    assert operations == [
        "source",
        "ownership",
        "down",
        "reset",
        "initialize",
        "prepare",
        "id-pool",
        "preflight",
        "config",
        "up",
        "ready",
    ]
    password = secrets_fixture.initialization.operator_password.get_secret_value()
    assert writes["review-operator-password"] == password + "\n"
    output = capsys.readouterr().out
    assert password not in output
    result = json.loads(output)
    assert result["mode"] == deployment_mode
    assert result["question_count"] == 8
    assert all(not path.exists() for path in retired_update_files)


def test_cli_failure_does_not_leak_invalid_input_or_exception_body(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        host, "validate_external_regular_file", Mock(side_effect=OSError("private-answer"))
    )
    assert host.main(["--release-input", "/private-answer"]) == 2
    assert "private-answer" not in str(capsys.readouterr())


def test_private_writer_refuses_shared_parent_without_writing_secret(tmp_path: Path) -> None:
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o777)
    parent.chmod(0o777)
    with pytest.raises(HostRefusedError, match="review_private_parent_unsafe"):
        host_data.write_private_file(parent / "secret", "private-answer")
    assert not (parent / "secret").exists()


def test_private_writer_sets_0600_and_refuses_symlinks_and_hardlinks() -> None:
    with TemporaryDirectory(prefix=".review-host-test-", dir=ROOT) as temporary:
        directory = Path(temporary)
        target = directory / "private"
        host_data.write_private_file(target, "first")
        assert target.stat().st_mode & 0o777 == 0o600
        host_data.write_private_file(target, "replacement")
        assert target.read_text() == "replacement"
        symlink = directory / "symlink"
        symlink.symlink_to(target)
        with pytest.raises(OSError):
            host_data.write_private_file(symlink, "should-not-write")
        hardlink = directory / "hardlink"
        os.link(target, hardlink)
        with pytest.raises(HostRefusedError, match="review_private_file_unsafe"):
            host_data.write_private_file(hardlink, "should-not-write")
        assert target.read_text() == "replacement"


def test_deploy_ownership_failure_prevents_secret_reads_service_stop_and_database_reset(
    inputs: HostInput,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = load_rehearsal_content(ROOT / "content/event-questions.json")
    monkeypatch.setattr(host, "REPOSITORY_ROOT", Path("/opt/gs-app-tomidaisai"))
    monkeypatch.setattr(host.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(host.os, "getegid", lambda: 1000)
    monkeypatch.setattr(host, "_require_release_source", lambda *_: None)
    monkeypatch.setattr(host, "load_rehearsal_content", lambda _: content)
    monkeypatch.setattr(host, "require_ownership", Mock(side_effect=HostRefusedError("ownership")))
    secret_read, compose, reset = Mock(), Mock(), Mock()
    monkeypatch.setattr(host, "load_secrets", secret_read)
    monkeypatch.setattr(host, "_compose", compose)
    monkeypatch.setattr(host, "reset_dedicated_database", reset)
    with pytest.raises(HostRefusedError):
        host.deploy_host(inputs)
    secret_read.assert_not_called()
    compose.assert_not_called()
    reset.assert_not_called()


def test_release_refuses_remote_docker_before_accessing_release_or_database(
    inputs: HostInput,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host, "_command", Mock(side_effect=["remote", "ssh://another-host"]))
    event = host.RehearsalEventInput(
        event_id=uuid4(),
        title="確認",
        slug="review-test",
        scoring_policy_version=host.get_quiz_settings().scoring.version,
        display_name_policy_version=host.DEMO_DISPLAY_NAME_POLICY_VERSION,
    )
    with pytest.raises(HostRefusedError, match="review_local_docker_required"):
        host._release(inputs, event)


@pytest.fixture
def release_directory(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # 祖先の共有書込を許可するmockを使わず、実際のpath保護も検証する。
    with TemporaryDirectory(prefix=".review-release-test-", dir=ROOT) as temporary:
        directory = Path(temporary)
        repository = directory / "source"
        repository.mkdir()
        configuration = directory / "configuration"
        configuration.mkdir(mode=0o700)
        backups = directory / "backups"
        backups.mkdir(mode=0o700)
        runtime = configuration / "runtime.env"
        runtime.write_text("APP_ENV=aws-demo\n")
        runtime.chmod(0o600)
        certificate = configuration / "rds-ca.pem"
        certificate.write_text("-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n")
        monkeypatch.setattr(host, "REPOSITORY_ROOT", repository)
        monkeypatch.setattr(host, "RUNTIME_FILE", runtime)
        monkeypatch.setattr(host, "POSTGRES_CA_FILE", certificate)
        monkeypatch.setattr(host, "RELEASE_FILE", configuration / "release.env")
        monkeypatch.setattr(host, "BACKUP_DIRECTORY", backups)
        monkeypatch.setattr(
            host,
            "_command",
            Mock(
                side_effect=[
                    "default",
                    "unix:///var/run/docker.sock",
                    "ip-10-80-1-10.ap-northeast-1.compute.internal",
                ]
            ),
        )
        yield directory


@pytest.fixture
def release_event() -> host.RehearsalEventInput:
    return host.RehearsalEventInput(
        event_id=uuid4(),
        title="確認",
        slug="review-test",
        scoring_policy_version=host.get_quiz_settings().scoring.version,
        display_name_policy_version=host.DEMO_DISPLAY_NAME_POLICY_VERSION,
    )


@pytest.mark.parametrize("release_exists", [False, True])
def test_release_creates_or_replaces_manifest_after_path_validation(
    inputs: HostInput,
    release_directory: Path,
    release_event: host.RehearsalEventInput,
    release_exists: bool,
) -> None:
    if release_exists:
        host.RELEASE_FILE.write_text("previous-release\n")
    release = host._release(inputs, release_event)
    assert release.event_id == release_event.event_id
    assert host.RELEASE_FILE.stat().st_mode & 0o777 == 0o600
    body = host.RELEASE_FILE.read_text()
    assert f"AWS_DEMO_SOURCE_COMMIT={inputs.source_commit}\n" in body
    assert f"AWS_DEMO_EVENT_ID={release_event.event_id}\n" in body
    assert "previous-release" not in body


@pytest.mark.parametrize("release_exists", [False, True])
def test_release_refuses_existing_or_planned_manifest_inside_backup(
    inputs: HostInput,
    release_directory: Path,
    release_event: host.RehearsalEventInput,
    release_exists: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = host.BACKUP_DIRECTORY / "release.env"
    monkeypatch.setattr(host, "RELEASE_FILE", destination)
    if release_exists:
        destination.write_text("previous-release\n")
    with pytest.raises(
        host.AwsDemoDeploymentValidationError,
        match="round_backup_host_directory_contains_control_file",
    ):
        host._release(inputs, release_event)
    if release_exists:
        assert destination.read_text() == "previous-release\n"
    else:
        assert not destination.exists()


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_release_refuses_links_without_overwriting_the_target(
    inputs: HostInput,
    release_directory: Path,
    release_event: host.RehearsalEventInput,
    link_kind: str,
) -> None:
    target = host.RELEASE_FILE.with_name("protected.env")
    target.write_text("preserved\n")
    if link_kind == "symlink":
        host.RELEASE_FILE.symlink_to(target)
    else:
        os.link(target, host.RELEASE_FILE)
    with pytest.raises((OSError, HostRefusedError)):
        host._release(inputs, release_event)
    assert target.read_text() == "preserved\n"


def test_release_refuses_symlink_target_inside_backup(
    inputs: HostInput,
    release_directory: Path,
    release_event: host.RehearsalEventInput,
) -> None:
    target = host.BACKUP_DIRECTORY / "protected.env"
    target.write_text("preserved\n")
    host.RELEASE_FILE.symlink_to(target)
    with pytest.raises(
        host.AwsDemoDeploymentValidationError,
        match="round_backup_host_directory_contains_control_file",
    ):
        host._release(inputs, release_event)
    assert target.read_text() == "preserved\n"


def test_release_still_requires_existing_runtime_file(
    inputs: HostInput,
    release_directory: Path,
    release_event: host.RehearsalEventInput,
) -> None:
    host.RUNTIME_FILE.unlink()
    with pytest.raises(
        host.AwsDemoDeploymentValidationError, match="protected_deployment_file_unavailable"
    ):
        host._release(inputs, release_event)
    assert not host.RELEASE_FILE.exists()


def test_runtime_serialization_contains_only_existing_parser_fields(
    inputs: HostInput,
    secrets_fixture: host_data.HostSecrets,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs: list[str] = []
    monkeypatch.setattr(host_data, "write_private_file", lambda _, body: outputs.append(body))
    host_data.write_runtime_file(host_data.runtime_settings(inputs, secrets_fixture, uuid4()))
    lines = dict(line.split("=", 1) for line in outputs[0].splitlines())
    assert lines.keys() == host_data.RUNTIME_ENVIRONMENT_FIELDS.keys()
    assert lines["APP_ENV"] == "aws-demo"
    assert json.loads(lines["REALTIME_ALLOWED_ORIGINS"]) == [inputs.public_url]
    assert lines["ENABLE_DEMO_SEED"] == "false"
    assert "\n" not in lines["SESSION_SECRET"]
