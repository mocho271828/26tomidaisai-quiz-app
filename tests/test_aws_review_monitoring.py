"""Dashboard の監視期限・配置権限・対象 ID を AWS/DB に接続せずに検証する。"""

import json
import os
import shlex
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from test_aws_review_deploy import CONFIG, owned_stack
from test_aws_review_deploy import host_input as host_input
from test_aws_review_domain_templates import _template
from test_load_test_aws import FakeAws

from validation.aws_review import deploy, monitoring_install, remote
from validation.aws_review.host_contracts import HostInput
from validation.aws_review.models import DeploymentInput, validate_monitoring_deadline

ROOT = Path(__file__).parents[1]
NOW = datetime(2026, 10, 21, 13, tzinfo=UTC)


def test_create_requires_explicit_expiry_and_default_update_has_no_extension() -> None:
    assert CONFIG.deployment_mode == "update"
    assert CONFIG.monitoring_expires_at is None
    with pytest.raises(ValidationError, match="create_monitoring_expiry_required"):
        DeploymentInput.model_validate({**CONFIG.model_dump(), "deployment_mode": "create"})
    with pytest.raises(ValidationError):
        DeploymentInput.model_validate(
            {**CONFIG.model_dump(), "monitoring_expires_at": "2026-10-21T20:00:00"}
        )


@pytest.mark.parametrize("hours", [-1, 0, 12.001, 100])
def test_explicit_deadline_is_future_and_bounded(hours: float) -> None:
    config = CONFIG.model_copy(update={"monitoring_expires_at": NOW + timedelta(hours=hours)})
    with pytest.raises(ValueError, match="expiry_must_be_within_12_hours"):
        validate_monitoring_deadline(config, now=NOW)


def test_update_preserves_even_expired_monitoring_deadline(host_input: HostInput) -> None:
    explicit = host_input.model_copy(update={"monitoring_expires_at": NOW + timedelta(hours=6)})
    first = monitoring_install.collector_config(explicit, None, now=NOW)
    later = monitoring_install.collector_config(host_input, first, now=NOW + timedelta(hours=8))
    assert later.expires_at == first.expires_at
    assert later.environment_id == f"gs-rehearsal-{host_input.demo_id}"
    assert set(later.model_dump()) == {"region", "environment_id", "expires_at"}
    with pytest.raises(ValueError, match="expiry_required"):
        monitoring_install.collector_config(host_input, None, now=NOW)


@pytest.mark.parametrize("explicit_expiry", [False, True])
def test_update_rejects_different_previous_environment(
    host_input: HostInput, explicit_expiry: bool
) -> None:
    explicit = host_input.model_copy(update={"monitoring_expires_at": NOW + timedelta(hours=6)})
    first = monitoring_install.collector_config(explicit, None, now=NOW)
    with pytest.raises(ValueError, match="environment_mismatch"):
        monitoring_install.collector_config(
            explicit if explicit_expiry else host_input,
            first.model_copy(update={"environment_id": "gs-rehearsal-other"}),
            now=NOW,
        )


@pytest.mark.parametrize("legacy", [False, True])
def test_previous_config_migration_preserves_expiry_and_writes_only_current_fields(
    tmp_path: Path, host_input: HostInput, legacy: bool
) -> None:
    path = tmp_path / "config.json"
    previous = {
        "region": "ap-northeast-1",
        "environment_id": f"gs-rehearsal-{host_input.demo_id}",
        "expires_at": NOW.isoformat(),
    }
    if legacy:
        previous.update(
            {
                "instance_id": host_input.instance_id,
                "database_identifier": host_input.database_identifier,
                "redis_cluster_id": "gs-rehearsal-review-redis-001",
                "redis_node_id": "0001",
                "distribution_id": "EDISTRIBUTION123",
                "cpu_period_seconds": 60,
            }
        )
    path.write_text(json.dumps(previous))
    path.chmod(0o640)

    migrated = monitoring_install._previous_config(path)
    updated = monitoring_install.collector_config(
        host_input, migrated, now=NOW + timedelta(hours=8)
    )

    assert updated.expires_at == NOW
    assert updated.environment_id == previous["environment_id"]
    assert set(updated.model_dump()) == {"region", "environment_id", "expires_at"}
    # 移行の読取りだけでは元設定を更新せず、install の atomic write で初めて切り替える。
    assert json.loads(path.read_text()) == previous


@pytest.mark.parametrize(
    "field,value",
    [("unexpected", "field"), ("instance_id", "invalid"), ("expires_at", "invalid")],
)
def test_previous_config_migration_rejects_unknown_or_malformed_values(
    tmp_path: Path, field: str, value: str
) -> None:
    path = tmp_path / "config.json"
    previous = {
        "region": "ap-northeast-1",
        "environment_id": "gs-rehearsal-review",
        "expires_at": NOW.isoformat(),
        "instance_id": "i-0123456789abcdef0",
        "database_identifier": "gs-rehearsal-review-db",
        "redis_cluster_id": "gs-rehearsal-review-redis-001",
        "distribution_id": "EDISTRIBUTION123",
        field: value,
    }
    path.write_text(json.dumps(previous))
    path.chmod(0o640)
    with pytest.raises(ValidationError):
        monitoring_install._previous_config(path)


@pytest.mark.parametrize("mode,exists", [("create", True), ("update", False)])
def test_invalid_deployment_mode_stops_before_any_mutation(
    monkeypatch: pytest.MonkeyPatch, mode: str, exists: bool
) -> None:
    config = CONFIG.model_copy(
        update={"deployment_mode": mode, "monitoring_expires_at": NOW + timedelta(hours=1)}
    )
    monkeypatch.setattr(deploy, "get_stack", Mock(return_value=owned_stack() if exists else None))
    preflight = Mock()
    monkeypatch.setattr(deploy, "preflight", preflight)
    api = FakeAws()
    with pytest.raises(ValueError, match="deployment_mode_stack_mismatch"):
        deploy.deploy(api, config, ROOT, now=NOW)
    assert api.calls == []
    preflight.assert_not_called()


def test_update_refuses_resource_replacement_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = CONFIG.model_copy(update={"ubuntu_ami_id": "ami-00000000000000000"})
    monkeypatch.setattr(deploy, "get_stack", Mock(return_value=owned_stack()))
    api = FakeAws()
    with pytest.raises(ValueError, match="cannot_replace_data_or_host"):
        deploy.deploy(api, config, ROOT, now=NOW)
    assert api.calls == []


def test_redis_uses_actual_member_cluster_and_rejects_other_group() -> None:
    api = FakeAws()
    api.responses[("elasticache", "describe_replication_groups")] = {
        "ReplicationGroups": [
            {
                "ReplicationGroupId": "gs-rehearsal-review-redis",
                "MemberClusters": ["gs-rehearsal-review-redis-009"],
            }
        ]
    }
    assert (
        deploy.redis_cluster_id(api, "gs-rehearsal-review-redis") == "gs-rehearsal-review-redis-009"
    )
    with pytest.raises(ValueError, match="redis_cluster_identity_invalid"):
        deploy.redis_cluster_id(api, "gs-rehearsal-other-redis")


def test_remote_update_guard_precedes_prepare_and_monitor_starts_after_host(
    host_input: HostInput,
) -> None:
    script = remote.host_commands(ROOT, host_input, "gs-review-review-123456789012", "a" * 64)
    assert script.index("review_update_requires_prepared_host") < script.index(
        "rm -rf /opt/gs-app-tomidaisai"
    )
    assert script.index("monitoring_install") < script.index(
        "python -m validation.aws_review.host "
    )
    assert script.index("python -m validation.aws_review.host ") < script.index(
        "systemctl start gs-quiz-monitor.service"
    )
    assert "--python /usr/bin/python3" in script
    subprocess.run(["/bin/bash", "-n"], input=script, text=True, check=True)  # noqa: S603 -- 構文確認だけで配置scriptは実行しない。
    service = monitoring_install.SERVICE
    for requirement in (
        "User=quiz-monitor",
        "Group=ubuntu",
        "Restart=on-failure",
        "CPUQuota=10%",
        "MemoryMax=256M",
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "PrivateTmp=true",
        "ReadWritePaths=/var/lib/quiz-monitoring",
    ):
        assert requirement in service
    assert "docker.sock" not in service


@pytest.mark.parametrize("mode", ["create", "update"])
def test_root_installer_imports_do_not_write_bytecode(
    host_input: HostInput, tmp_path: Path, mode: str
) -> None:
    config = HostInput.model_validate(
        {
            **host_input.model_dump(),
            "deployment_mode": mode,
            "monitoring_expires_at": NOW + timedelta(hours=2),
        }
    )
    script = remote.host_commands(ROOT, config, "gs-review-review-123456789012", "a" * 64)
    installer_line = next(
        line
        for line in script.splitlines()
        if " -m validation.aws_review.monitoring_install " in line
    )
    arguments = shlex.split(installer_line.removesuffix("\\"))
    cache = tmp_path / "bytecode"
    environment = {**os.environ, "PYTHONPYCACHEPREFIX": str(cache)}
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    # 実際の起動引数でimportだけを実行し、root所有cacheを生む副作用を検出する。
    result = subprocess.run(  # noqa: S603 -- 生成済み固定コマンドのimportだけを一時cache先で検証する。
        [sys.executable, *arguments[1:], "--help"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert "Install the dedicated quiz monitoring service" in result.stdout
    assert not list(cache.rglob("*.pyc"))


def test_dashboard_has_actual_id_references_and_no_ec2_credit_assumption() -> None:
    edge = _template(ROOT / "infra/aws-rehearsal/public-edge.yaml")
    dashboard = edge["Resources"]["SystemHealthDashboard"]
    assert dashboard["Condition"] == "HasMonitoring"
    assert dashboard["Properties"]["DashboardName"] == {"Fn::Sub": "gs-rehearsal-${DemoId}-health"}
    body = json.loads(dashboard["Properties"]["DashboardBody"]["Fn::Sub"])
    assert len(body["widgets"]) <= 12
    widgets = [item["properties"] for item in body["widgets"] if item["type"] == "metric"]
    metrics = [line for item in widgets for line in item["metrics"]]
    assert not any(line[:2] == ["AWS/EC2", "CPUCreditBalance"] for line in metrics)
    assert all("${MonitoringInstanceId}" in line for line in metrics if line[0] == "AWS/EC2")
    requests = next(
        item
        for item in widgets
        if item["metrics"][0][:2] == ["GSQuiz/SystemHealth", "ApiRequestsPerMinute"]
    )
    assert requests["stat"] == "Average"
    health = next(
        item for item in widgets if item["metrics"][0][:2] == ["GSQuiz/SystemHealth", "ApiHealth"]
    )
    assert health["stat"] == "Minimum" and len(health["metrics"]) == 4
    cloudfront = next(item for item in widgets if item["region"] == "us-east-1")
    assert all("${Distribution}" in line and "Global" in line for line in cloudfront["metrics"])


def test_instance_credit_is_conditional_and_monitor_permissions_are_scoped() -> None:
    workload = _template(ROOT / "infra/aws-rehearsal/workload.yaml")
    assert workload["Parameters"]["AppInstanceType"]["Default"] == "c7i.xlarge"
    instance = workload["Resources"]["AppInstance"]["Properties"]
    assert instance["Monitoring"] is True
    assert instance["CreditSpecification"] == {
        "Fn::If": ["IsBurstableApp", {"CPUCredits": "standard"}, {"Ref": "AWS::NoValue"}]
    }
    assert instance["MetadataOptions"]["HttpPutResponseHopLimit"] == 1
    statements = workload["Resources"]["AppRole"]["Properties"]["Policies"][0]["PolicyDocument"][
        "Statement"
    ]
    assert not any(item.get("Action") == "cloudwatch:GetMetricData" for item in statements)
    publish = next(item for item in statements if item.get("Action") == "cloudwatch:PutMetricData")
    assert publish["Condition"]["StringEquals"]["aws:RequestedRegion"] == "ap-northeast-1"
    assert publish["Condition"]["StringEquals"]["cloudwatch:namespace"] == "GSQuiz/SystemHealth"


def test_installer_failure_logs_only_safe_locations(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        monitoring_install, "_load_inputs", Mock(side_effect=ValueError("private-fixture-input"))
    )
    assert monitoring_install.main(["--release-input", "/ignored"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    error = json.loads(output.err)
    assert error["code"] == "review_host_failed"
    assert "private-fixture-input" not in output.err
    assert "Traceback" not in output.err


def test_installer_restart_is_bounded_and_system_python_is_required() -> None:
    service = monitoring_install.SERVICE
    assert "StartLimitIntervalSec=300" in service
    assert "StartLimitBurst=3" in service
    assert "ExecStart=/opt/gs-app-tomidaisai/backend/.venv/bin/python" in service
    prepare = (ROOT / "scripts/aws-rehearsal-host-prepare.sh").read_text()
    assert "--python /usr/bin/python3" in prepare
    assert "Environment=PYTHONDONTWRITEBYTECODE=1" in service


def test_source_access_uses_only_backend_and_rejects_home_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "source"
    (repository / "backend/.venv/bin").mkdir(parents=True)
    (repository / "backend/.venv/bin/python").symlink_to("/home/ubuntu/.local/python")
    monkeypatch.setattr(monitoring_install, "ROOT", repository)
    with pytest.raises(ValueError, match="system_python_required"):
        monitoring_install._allow_backend_read()


@pytest.fixture
def monitoring_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repository = tmp_path / "source"
    (repository / "backend/.venv/bin").mkdir(parents=True)
    (repository / "backend/src").mkdir()
    (repository / "backend/.venv/bin/python").symlink_to("/usr/bin/python3")
    for name in ("backend/pyproject.toml", "backend/src/app.py", "backend/.env", "backend/uv.lock"):
        path = repository / name
        path.write_text("fixture\n")
        path.chmod(0o600)
    original_lstat = Path.lstat

    def owned_source_stat(path: Path) -> os.stat_result:
        info = original_lstat(path)
        if not path.is_relative_to(repository):
            return info
        fields = list(info)
        fields[stat.ST_UID] = fields[stat.ST_GID] = 1000
        return os.stat_result(fields)

    monkeypatch.setattr(monitoring_install, "ROOT", repository)
    monkeypatch.setattr(Path, "lstat", owned_source_stat)
    monkeypatch.setattr(monitoring_install.os, "chown", Mock())
    return repository


def test_monitor_source_grants_pyproject_group_read_without_exposing_other_backend_files(
    monitoring_source: Path,
) -> None:
    configuration = monitoring_source / "backend/pyproject.toml"
    assert stat.S_IMODE(configuration.stat().st_mode) == 0o600

    monitoring_install._allow_backend_read()

    assert stat.S_IMODE(configuration.stat().st_mode) == 0o640
    assert configuration.read_text() == "fixture\n"
    assert stat.S_IMODE((monitoring_source / "backend/src/app.py").stat().st_mode) == 0o640
    for name in ("backend/.env", "backend/uv.lock"):
        assert stat.S_IMODE((monitoring_source / name).stat().st_mode) == 0o600


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink", "directory", "foreign_owner"])
def test_monitor_source_refuses_unsafe_pyproject_without_granting_read(
    monitoring_source: Path, monkeypatch: pytest.MonkeyPatch, unsafe_kind: str
) -> None:
    configuration = monitoring_source / "backend/pyproject.toml"
    protected = monitoring_source / "backend/.env"
    if unsafe_kind == "foreign_owner":
        original_lstat = Path.lstat

        def foreign_owner_stat(path: Path) -> os.stat_result:
            fields = list(original_lstat(path))
            if path == configuration:
                fields[stat.ST_UID] = 0
            return os.stat_result(fields)

        monkeypatch.setattr(Path, "lstat", foreign_owner_stat)
    else:
        configuration.unlink()
        if unsafe_kind == "symlink":
            configuration.symlink_to(protected)
        elif unsafe_kind == "hardlink":
            configuration.hardlink_to(protected)
        else:
            configuration.mkdir(mode=0o700)

    with pytest.raises(ValueError, match="review_monitoring_source_unsafe"):
        monitoring_install._allow_backend_read()

    assert stat.S_IMODE(protected.stat().st_mode) == 0o600
    assert stat.S_IMODE(configuration.stat().st_mode) == (
        0o700 if unsafe_kind == "directory" else 0o600
    )


def test_installer_sets_dedicated_state_owner_and_root_owned_config(
    host_input: HostInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    config = host_input.model_copy(update={"monitoring_expires_at": NOW + timedelta(hours=2)})
    monkeypatch.setattr(monitoring_install.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        monitoring_install.pwd, "getpwnam", lambda _name: SimpleNamespace(pw_uid=1000)
    )
    monkeypatch.setattr(
        monitoring_install.grp, "getgrnam", lambda _name: SimpleNamespace(gr_gid=1000)
    )
    monkeypatch.setattr(
        monitoring_install, "_require_monitor_user", lambda: SimpleNamespace(pw_uid=999)
    )
    monkeypatch.setattr(monitoring_install, "_allow_backend_read", Mock())
    directories = Mock()
    writes = Mock()
    monkeypatch.setattr(monitoring_install, "_directory", directories)
    monkeypatch.setattr(monitoring_install, "_write", writes)
    monitoring_install.install(config, now=NOW)
    directories.assert_any_call(monitoring_install.STATE_DIRECTORY, uid=999, gid=1000)
    assert writes.call_args_list[0].kwargs == {"gid": 1000, "mode": 0o640}
    assert writes.call_args_list[1].kwargs == {"gid": 0, "mode": 0o644}


@pytest.mark.parametrize(
    "key,value",
    [("ManualReview", "false"), ("ManualReview", None), ("DemoId", "another"), ("DemoId", None)],
)
def test_update_refuses_implicit_environment_mode_switch(
    monkeypatch: pytest.MonkeyPatch, key: str, value: str | None
) -> None:
    existing = owned_stack()
    parameters = tuple(
        item.model_copy(update={"value": value}) if item.key == key else item
        for item in existing.parameters
        if item.key != key or value is not None
    )
    monkeypatch.setattr(
        deploy,
        "get_stack",
        Mock(return_value=existing.model_copy(update={"parameters": parameters})),
    )
    mutation = Mock()
    monkeypatch.setattr(deploy, "apply_stack", mutation)
    api = FakeAws()
    with pytest.raises(ValueError, match="cannot_replace_data_or_host"):
        deploy.deploy(api, CONFIG, ROOT, now=NOW)
    mutation.assert_not_called()
    assert api.calls == []
