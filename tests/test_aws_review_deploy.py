from __future__ import annotations

import json
import shlex
from collections import deque
from pathlib import Path
from typing import TypeVar
from unittest.mock import Mock

import pytest
from pydantic import BaseModel, ValidationError
from test_load_test_aws import FakeAws

from quiz_app.load_test_aws.aws import AwsCallError
from validation.aws_review import __main__ as cli
from validation.aws_review import deploy, remote, stacks
from validation.aws_review.host_contracts import HostInput
from validation.aws_review.models import DeploymentInput, ReviewTarget

ResponseT = TypeVar("ResponseT", bound=BaseModel)
ACCOUNT = "123456789012"
COMMIT = "a" * 40
CERTIFICATE_ARN = (
    f"arn:aws:acm:us-east-1:{ACCOUNT}:certificate/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
)
TARGET = ReviewTarget(account_id=ACCOUNT, demo_id="review")
CONFIG = DeploymentInput(
    account_id=ACCOUNT,
    review_domain_name="quiz.example.org",
    review_certificate_arn=CERTIFICATE_ARN,
    demo_id="review",
    app_az="ap-northeast-1a",
    secondary_az="ap-northeast-1c",
    ubuntu_ami_id="ami-0123456789abcdef0",
    database_engine_version="16.6",
    source_commit=COMMIT,
)
SAFE_WORKLOAD_CHANGES = [
    {
        "Type": "Resource",
        "ResourceChange": {
            "Action": "Modify",
            "LogicalResourceId": "AppInstance",
            "ResourceType": "AWS::EC2::Instance",
            "Replacement": "False",
            "Scope": ["Properties"],
            "Details": [
                {
                    "Target": {
                        "Attribute": "Properties",
                        "Name": "Monitoring",
                        "RequiresRecreation": "Never",
                    }
                }
            ],
        },
    }
]


def owned_stack(kind: str = "workload", status: str = "CREATE_COMPLETE") -> stacks.Stack:
    name = f"{TARGET.prefix}-{kind}"
    return stacks.Stack.model_validate(
        {
            "StackId": f"arn:aws:cloudformation:ap-northeast-1:{ACCOUNT}:stack/{name}/id",
            "StackName": name,
            "StackStatus": status,
            "Parameters": [
                {"ParameterKey": key, "ParameterValue": value}
                for key, value in {
                    "DemoId": CONFIG.demo_id,
                    "ManualReview": "true",
                    "AppAvailabilityZone": CONFIG.app_az,
                    "SecondaryAvailabilityZone": CONFIG.secondary_az,
                    "UbuntuAmiId": CONFIG.ubuntu_ami_id,
                    "DatabaseEngineVersion": CONFIG.database_engine_version,
                }.items()
            ],
            "Tags": [
                {"Key": "DemoId", "Value": TARGET.demo_id},
                {"Key": "Purpose", "Value": "quiz-rehearsal"},
            ],
        }
    )


class ScriptedAws(FakeAws):
    def __init__(self) -> None:
        super().__init__()
        self.sequence: dict[tuple[str, str], deque[object]] = {}

    def call(
        self, service: str, operation: str, response: type[ResponseT], **kwargs: object
    ) -> ResponseT:
        queue = self.sequence.get((service, operation))
        if queue is None:
            return super().call(service, operation, response, **kwargs)
        self.calls.append((service, operation, kwargs))
        value = queue.popleft()
        if isinstance(value, AwsCallError):
            raise value
        return response.model_validate(value)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    value = Clock()
    monkeypatch.setattr(stacks.time, "monotonic", value.monotonic)
    monkeypatch.setattr(stacks.time, "sleep", value.sleep)
    return value


@pytest.fixture
def template(tmp_path: Path) -> Path:
    path = tmp_path / "template.yaml"
    path.write_text("Resources: {}\n")
    return path


@pytest.fixture
def owner_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "AWS_REVIEW_REPOSITORY": "committee/quiz",
        "AWS_REVIEW_OPERATOR": "operator",
        "GITHUB_REPOSITORY": "committee/quiz",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_ACTOR": "operator",
        "GITHUB_TRIGGERING_ACTOR": "operator",
        "GITHUB_SHA": COMMIT,
        "AWS_ACCOUNT_ID": ACCOUNT,
        "AWS_REVIEW_ID": "review",
        "AWS_REVIEW_PUBLIC_ENTRYPOINT": "custom-domain",
        "AWS_REVIEW_DOMAIN_NAME": CONFIG.review_domain_name,
        "AWS_REVIEW_CERTIFICATE_ARN": CERTIFICATE_ARN,
        "AWS_APP_AZ": CONFIG.app_az,
        "AWS_SECONDARY_AZ": CONFIG.secondary_az,
        "AWS_UBUNTU_AMI_ID": CONFIG.ubuntu_ami_id,
        "AWS_DATABASE_ENGINE_VERSION": CONFIG.database_engine_version,
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    for key in ("VITE_EVENT_START_AT", "VITE_SURVEY_URL", "VITE_HOME_URL"):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize(
    "key,value",
    [
        ("AWS_REVIEW_REPOSITORY", ""),
        ("AWS_REVIEW_OPERATOR", ""),
        ("GITHUB_EVENT_NAME", "push"),
        ("GITHUB_REPOSITORY", "someone/gs-app-tomidaisai"),
        ("GITHUB_REF", "refs/heads/topic"),
        ("GITHUB_ACTOR", "someone"),
        ("GITHUB_TRIGGERING_ACTOR", "someone"),
    ],
)
def test_non_owner_or_non_manual_context_fails_before_source_access(
    owner_environment: None, monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    monkeypatch.setenv(key, value)
    git = Mock()
    monkeypatch.setattr(cli.subprocess, "check_output", git)
    with pytest.raises(ValueError, match="manual_owner_execution_required"):
        cli.configuration("deploy")
    git.assert_not_called()


@pytest.mark.parametrize("head,dirty", [("b" * 40, ""), (COMMIT, " M content/file.json\n")])
def test_deploy_rejects_commit_mismatch_and_uncommitted_source(
    owner_environment: None, monkeypatch: pytest.MonkeyPatch, head: str, dirty: str
) -> None:
    monkeypatch.setattr(cli.subprocess, "check_output", Mock(side_effect=[head, dirty]))
    with pytest.raises(ValueError, match="source_must_match_clean_commit"):
        cli.configuration("deploy")


def test_clean_source_is_accepted_and_destroy_does_not_depend_on_git(
    owner_environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = Mock(side_effect=[COMMIT + "\n", ""])
    monkeypatch.setattr(cli.subprocess, "check_output", git)
    config = cli.configuration("deploy")
    assert isinstance(config, DeploymentInput)
    assert config.monitoring_expires_at is not None
    assert config.model_dump(exclude={"monitoring_expires_at"}) == CONFIG.model_dump(
        exclude={"monitoring_expires_at"}
    )
    git.reset_mock()
    assert cli.configuration("destroy") == TARGET
    git.assert_not_called()


def test_frontend_environment_is_loaded_without_reading_local_env(
    owner_environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = {
        "VITE_EVENT_START_AT": "2026-09-30T18:00:00+09:00",
        "VITE_SURVEY_URL": "https://forms.example.org/survey?a=1&b=2",
        "VITE_HOME_URL": "https://festival.example.org/",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(cli.subprocess, "check_output", Mock(side_effect=[COMMIT, ""]))
    config = cli.configuration("deploy")
    assert isinstance(config, DeploymentInput)
    assert {key: getattr(config, key.lower()) for key in values} == values


def test_invalid_frontend_input_prevents_aws_calls_but_does_not_block_destroy(
    owner_environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VITE_HOME_URL", "http://festival.example.org/")
    aws = Mock()
    monkeypatch.setattr(cli, "BotoAwsApi", aws)
    monkeypatch.setattr(cli, "configure_logging", Mock())
    monkeypatch.setattr(cli, "log_failure", Mock())
    assert cli.main(["deploy"]) == 1
    aws.assert_not_called()
    with pytest.raises(ValidationError):
        cli.configuration("deploy")
    assert cli.configuration("destroy") == TARGET


@pytest.mark.parametrize("field,value", [("arn", "arn:aws:other"), ("tags", ())])
def test_stack_ownership_mismatch_is_not_treated_as_absence(field: str, value: object) -> None:
    api = FakeAws()
    foreign = owned_stack().model_copy(update={field: value})
    api.page_responses[("cloudformation", "describe_stacks")] = [
        {"Stacks": [foreign.model_dump(by_alias=True)]}
    ]
    with pytest.raises(ValueError, match="ownership_mismatch"):
        stacks.get_stack(api, TARGET, "workload")


@pytest.mark.parametrize("status", ["UPDATE_IN_PROGRESS", "UPDATE_ROLLBACK_COMPLETE"])
def test_stack_incomplete_state_is_rejected_before_changeset(
    template: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    api = FakeAws()
    monkeypatch.setattr(stacks, "get_stack", lambda *_args: owned_stack(status=status))
    with pytest.raises(ValueError, match="not_ready_for_update"):
        stacks.apply_stack(api, TARGET, "workload", template, {})
    assert api.calls == []


@pytest.mark.parametrize("is_update", [False, True])
def test_stack_creation_and_update_wait_for_changeset_execution(
    template: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock, is_update: bool
) -> None:
    api = ScriptedAws()
    initial = owned_stack() if is_update else None
    final = owned_stack(status="UPDATE_COMPLETE" if is_update else "CREATE_COMPLETE")
    # 更新直後の古いCREATE_COMPLETEだけで成功にしない。
    reads = iter((initial, owned_stack(), final))
    monkeypatch.setattr(stacks, "get_stack", lambda *_args: next(reads))
    api.responses[("cloudformation", "create_change_set")] = {"Id": "change-id"}
    api.sequence[("cloudformation", "describe_change_set")] = deque(
        [
            {"Status": "CREATE_COMPLETE", "Changes": SAFE_WORKLOAD_CHANGES},
            {"Status": "CREATE_COMPLETE", "ExecutionStatus": "EXECUTE_IN_PROGRESS"},
            {"Status": "CREATE_COMPLETE", "ExecutionStatus": "EXECUTE_COMPLETE"},
        ]
    )
    result = stacks.apply_stack(api, TARGET, "workload", template, {"DemoId": "review"})
    assert result == final
    assert clock.now == 15
    creation = next(arguments for _, op, arguments in api.calls if op == "create_change_set")
    assert creation["ChangeSetType"] == ("UPDATE" if is_update else "CREATE")
    assert creation["StackName"] == (initial.arn if initial else f"{TARGET.prefix}-workload")
    assert sum(op == "execute_change_set" for _, op, _ in api.calls) == 1


@pytest.mark.parametrize("has_current", [False, True])
def test_no_change_result_only_succeeds_for_an_existing_stack(
    template: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: Clock,
    has_current: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    api = FakeAws()
    current = owned_stack() if has_current else None
    monkeypatch.setattr(stacks, "get_stack", lambda *_args: current)
    api.responses[("cloudformation", "create_change_set")] = {"Id": "change-id"}
    api.responses[("cloudformation", "describe_change_set")] = {
        "Status": "FAILED",
        "StatusReason": "The submitted information didn't contain changes.",
    }
    if has_current:
        assert stacks.apply_stack(api, TARGET, "workload", template, {}) == current
    else:
        with pytest.raises(ValueError, match="change_set_failed"):
            stacks.apply_stack(api, TARGET, "workload", template, {})
    assert any(op == "delete_change_set" for _, op, _ in api.calls)
    assert not any(op == "execute_change_set" for _, op, _ in api.calls)
    output = capsys.readouterr().err
    states = [json.loads(line)["state"] for line in output.splitlines()]
    if has_current:
        assert states[-1] == "NO_CHANGES" and "変更不要" in output
        assert "FAILED" not in output
    else:
        assert states[-1] == "FAILED"


def test_changeset_wait_has_a_finite_deadline(
    template: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    api = FakeAws()
    monkeypatch.setattr(stacks, "get_stack", lambda *_args: None)
    api.responses[("cloudformation", "create_change_set")] = {"Id": "change-id"}
    api.responses[("cloudformation", "describe_change_set")] = {"Status": "CREATE_PENDING"}
    with pytest.raises(TimeoutError, match="change_set_timeout"):
        stacks.apply_stack(api, TARGET, "workload", template, {}, timeout_seconds=1)
    assert not any(op == "execute_change_set" for _, op, _ in api.calls)


@pytest.fixture
def host_input() -> HostInput:
    registry = f"{ACCOUNT}.dkr.ecr.ap-northeast-1.amazonaws.com/{TARGET.prefix}"
    secret = f"arn:aws:secretsmanager:ap-northeast-1:{ACCOUNT}:secret:"
    return HostInput(
        account_id=ACCOUNT,
        demo_id="review",
        source_commit=COMMIT,
        api_image=f"{registry}/api@sha256:{'a' * 64}",
        gateway_image=f"{registry}/gateway@sha256:{'b' * 64}",
        instance_id="i-0123456789abcdef0",
        private_ip="10.80.1.10",
        public_url=CONFIG.public_url,
        review_domain_name=CONFIG.review_domain_name,
        database_identifier="gs-rehearsal-review-db",
        redis_identifier="gs-rehearsal-review-redis",
        rds_hostname="review.abcdef.ap-northeast-1.rds.amazonaws.com",
        redis_hostname="review.0001.apne1.cache.amazonaws.com",
        database_master_secret_arn=secret + "rds!db-1234-AbCdEf",
        redis_auth_secret_arn=secret + "gs-rehearsal-review-redis-AbCdEf",
        runtime_secret_arn=secret + "gs-rehearsal-review-runtime-AbCdEf",
    )


def online_host(api: FakeAws, inputs: HostInput) -> None:
    api.responses[("ssm", "describe_instance_information")] = {
        "InstanceInformationList": [{"InstanceId": inputs.instance_id, "PingStatus": "Online"}]
    }
    api.responses[("ssm", "send_command")] = {
        "Command": {"CommandId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}
    }


def test_offline_host_times_out_without_sending_mutation(
    host_input: HostInput, clock: Clock
) -> None:
    api = FakeAws()
    api.responses[("ssm", "describe_instance_information")] = {"InstanceInformationList": []}
    with pytest.raises(TimeoutError, match="ssm_host_offline"):
        remote.deploy_host(api, host_input, "script")
    assert not any(op == "send_command" for _, op, _ in api.calls)
    assert clock.now == 600


def test_remote_timeout_cancels_exact_command(host_input: HostInput, clock: Clock) -> None:
    api = FakeAws()
    online_host(api, host_input)
    api.responses[("ssm", "get_command_invocation")] = {"Status": "InProgress", "ResponseCode": -1}
    with pytest.raises(TimeoutError, match="host_deployment_timeout"):
        remote.deploy_host(api, host_input, "script")
    assert api.calls[-1] == (
        "ssm",
        "cancel_command",
        {
            "CommandId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "InstanceIds": [host_input.instance_id],
        },
    )


def test_ssm_eventual_invocation_creation_is_retried(host_input: HostInput, clock: Clock) -> None:
    api = ScriptedAws()
    online_host(api, host_input)
    api.sequence[("ssm", "get_command_invocation")] = deque(
        [
            AwsCallError("ssm", "get_command_invocation", "InvocationDoesNotExist"),
            {"Status": "Success", "ResponseCode": 0},
        ]
    )
    script = "printf '%s\\n' 'review'\nprintf 'second line'\n"
    remote.deploy_host(api, host_input, script)
    assert clock.now == 15
    assert not any(op == "cancel_command" for _, op, _ in api.calls)
    send = next(arguments for _, op, arguments in api.calls if op == "send_command")
    assert send["Parameters"] == {
        "commands": ["bash -c " + shlex.quote(script)],
        "executionTimeout": ["2400"],
    }


def test_ssm_access_denied_is_not_retried_as_pending(host_input: HostInput, clock: Clock) -> None:
    api = FakeAws()
    online_host(api, host_input)
    api.responses[("ssm", "get_command_invocation")] = AwsCallError(
        "ssm", "get_command_invocation", "AccessDeniedException"
    )
    with pytest.raises(AwsCallError, match="AccessDeniedException"):
        remote.deploy_host(api, host_input, "script")
    assert clock.now == 0


def test_preflight_wrong_account_prevents_resource_checks() -> None:
    api = FakeAws()
    api.responses[("sts", "get_caller_identity")] = {
        "Account": "999999999999",
        "Arn": "arn:aws:iam::999999999999:role/other",
    }
    with pytest.raises(ValueError, match="account_mismatch"):
        deploy.preflight(api, CONFIG)
    assert [op for _, op, _ in api.calls] == ["get_caller_identity"]


def test_cloudfront_preflight_does_not_require_acm() -> None:
    config = DeploymentInput.model_validate(
        {**CONFIG.model_dump(), "public_entrypoint": "cloudfront"}
    )
    api = FakeAws()
    api.responses[("sts", "get_caller_identity")] = {
        "Account": ACCOUNT,
        "Arn": f"arn:aws:iam::{ACCOUNT}:role/review",
    }
    api.responses[("ec2", "describe_images")] = {
        "Images": [
            {
                "ImageId": config.ubuntu_ami_id,
                "OwnerId": "099720109477",
                "Architecture": "x86_64",
                "Name": "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-20261021",
                "State": "available",
            }
        ]
    }
    api.responses[("ec2", "describe_availability_zones")] = {
        "AvailabilityZones": [
            {"ZoneName": config.app_az, "ZoneId": "apne1-az1", "State": "available"},
            {"ZoneName": config.secondary_az, "ZoneId": "apne1-az2", "State": "available"},
        ]
    }
    api.responses[("rds", "describe_orderable_db_instance_options")] = {
        "OrderableDBInstanceOptions": [{"EngineVersion": config.database_engine_version}]
    }
    api.responses[("ec2", "describe_managed_prefix_lists")] = {
        "PrefixLists": [{"PrefixListId": "pl-0123456789abcdef0"}]
    }
    assert deploy.preflight(api, config) == "pl-0123456789abcdef0"
    assert [(service, operation) for service, operation, _ in api.calls] == [
        ("sts", "get_caller_identity"),
        ("ec2", "describe_images"),
        ("ec2", "describe_availability_zones"),
        ("rds", "describe_orderable_db_instance_options"),
        ("ec2", "describe_managed_prefix_lists"),
    ]


@pytest.mark.parametrize("public_entrypoint", ["custom-domain", "cloudfront"])
@pytest.mark.parametrize(
    "host_failure,workload_present",
    [(False, True), (True, True), (False, False)],
)
def test_public_entry_requires_successful_host_and_existing_workload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_input: HostInput,
    host_failure: bool,
    workload_present: bool,
    public_entrypoint: str,
) -> None:
    config = DeploymentInput.model_validate(
        {**CONFIG.model_dump(), "public_entrypoint": public_entrypoint}
    )
    expected_url = (
        "https://d123example.cloudfront.net"
        if public_entrypoint == "cloudfront"
        else host_input.public_url
    )
    api = FakeAws()
    api.responses[("s3", "get_bucket_tagging")] = {
        "TagSet": [tag.model_dump(by_alias=True) for tag in owned_stack().tags]
    }
    api.responses[("elasticache", "describe_replication_groups")] = {
        "ReplicationGroups": [
            {
                "ReplicationGroupId": host_input.redis_identifier,
                "MemberClusters": ["gs-rehearsal-review-redis-001"],
            }
        ]
    }
    outputs = {
        "control": {
            "CloudFormationExecutionRoleArn": f"arn:aws:iam::{ACCOUNT}:role/gs-review-review-cfn",
            "ArtifactBucketName": f"gs-review-review-{ACCOUNT}",
        },
        "workload": {
            "AppInstanceArn": (
                f"arn:aws:ec2:ap-northeast-1:{ACCOUNT}:instance/{host_input.instance_id}"
            ),
            "AppInstanceId": host_input.instance_id,
            "AppPrivateDnsName": "ip-10-80-1-10.ap-northeast-1.compute.internal",
            "AppSecurityGroupId": "sg-0123456789abcdef0",
            "AppPrivateIp": host_input.private_ip,
            "DatabaseIdentifier": host_input.database_identifier,
            "RedisIdentifier": host_input.redis_identifier,
            "DatabaseEndpoint": host_input.rds_hostname,
            "RedisEndpoint": host_input.redis_hostname,
            "DatabaseMasterSecretArn": host_input.database_master_secret_arn,
            "RedisAuthSecretArn": host_input.redis_auth_secret_arn,
            "RuntimeSecretArn": host_input.runtime_secret_arn,
        },
        "edge": {
            "PublicUrl": host_input.public_url,
            "CloudFrontDomainName": "d123example.cloudfront.net",
            "DistributionId": "EDISTRIBUTION123",
        },
    }
    edge_states: list[str] = []

    def apply(
        _api: object,
        _target: object,
        kind: str,
        _template: Path,
        parameters: dict[str, str],
        **_kwargs: object,
    ) -> stacks.Stack:
        if kind == "workload":
            assert _kwargs["allow_instance_type_conditional"] is True
        if kind == "edge":
            edge_states.append(parameters["PublicEnabled"])
            assert parameters["ReviewDomainName"] == config.review_domain_name
            assert parameters["ReviewCertificateArn"] == config.review_certificate_arn
            if public_entrypoint == "cloudfront":
                assert parameters["ReviewDomainName"] == parameters["ReviewCertificateArn"] == ""
        return owned_stack(kind).model_copy(
            update={
                "outputs": tuple(
                    stacks.Output(key=key, value=value) for key, value in outputs[kind].items()
                )
            }
        )

    def bundle(_root: Path, arguments: list[str]) -> None:
        assert arguments[:3] == ["git", "bundle", "create"]
        Path(arguments[3]).write_bytes(b"verified source bundle")

    monkeypatch.setattr(deploy, "preflight", lambda *_args: "pl-0123456789abcdef0")
    monkeypatch.setattr(deploy, "validate_update_instance", Mock())
    monkeypatch.setattr(deploy, "apply_stack", apply)
    monkeypatch.setattr(
        deploy, "build_images", lambda *_args: (host_input.api_image, host_input.gateway_image)
    )
    monkeypatch.setattr(deploy, "run", bundle)
    monkeypatch.setattr(deploy, "host_commands", lambda *_args: "host-script")
    host = Mock(side_effect=ValueError("host_failed") if host_failure else None)
    monkeypatch.setattr(deploy, "deploy_host", host)
    monkeypatch.setattr(
        deploy,
        "get_stack",
        Mock(side_effect=[owned_stack(), owned_stack() if workload_present else None]),
    )
    if host_failure or not workload_present:
        with pytest.raises(ValueError, match="host_failed|workload_disappeared"):
            deploy.deploy(api, config, tmp_path)
        assert edge_states == ["false"]
    else:
        result = deploy.deploy(api, config, tmp_path)
        assert result.public_entrypoint == public_entrypoint
        assert result.url == expected_url
        assert result.cloudfront_domain_name == "d123example.cloudfront.net"
        assert edge_states == ["false", "true"]
    host.assert_called_once()
    assert host.call_args.args[1].public_url == expected_url
