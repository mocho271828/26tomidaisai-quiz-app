import json
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from validation.aws_review import host
from validation.aws_review.host_contracts import HostInput, HostRefusedError


@pytest.fixture
def inputs() -> HostInput:
    account = "123456789012"
    registry = f"{account}.dkr.ecr.ap-northeast-1.amazonaws.com/gs-rehearsal-review"
    secret_prefix = f"arn:aws:secretsmanager:ap-northeast-1:{account}:secret:"
    return HostInput(
        account_id=account,
        demo_id="review",
        source_commit="a" * 40,
        api_image=f"{registry}/api@sha256:{'b' * 64}",
        gateway_image=f"{registry}/gateway@sha256:{'c' * 64}",
        instance_id="i-0123456789abcdef0",
        private_ip="10.80.1.10",
        public_url="https://d123example.cloudfront.net",
        database_identifier="review-database",
        redis_identifier="gs-rehearsal-review-redis",
        rds_hostname="review.abcdef.ap-northeast-1.rds.amazonaws.com",
        redis_hostname="review.0001.apne1.cache.amazonaws.com",
        database_master_secret_arn=secret_prefix + "rds!db-1234-AbCdEf",
        redis_auth_secret_arn=secret_prefix + "gs-rehearsal-review-redis-AbCdEf",
        runtime_secret_arn=secret_prefix + "gs-rehearsal-review-runtime-AbCdEf",
    )


def _responses(inputs: HostInput) -> list[str]:
    return [
        json.dumps({"status": "ok", "service": "quiz-api", "version": inputs.source_commit}),
        json.dumps({"status": "ready", "database": "ok", "migration": "current", "redis": "ok"}),
    ] * 2


def test_readiness_checks_both_api_versions_and_dependencies_directly(
    inputs: HostInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = Mock(side_effect=_responses(inputs))
    monkeypatch.setattr(host, "_command", command)
    host._verify_api_readiness(inputs)
    assert [call.args[0][-1] for call in command.call_args_list] == [
        f"http://{service}:8000/api/health/{probe}"
        for service in ("api", "api-secondary")
        for probe in ("live", "ready")
    ]
    for call in command.call_args_list:
        assert call.args[0][-10:-6] == ["exec", "-T", "gateway", "curl"]
        assert "--max-time" in call.args[0]
        assert call.kwargs == {
            "private_ip": inputs.private_ip,
            "timeout": 15,
        }


@pytest.mark.parametrize("api_index", [0, 2])
@pytest.mark.parametrize("failure", ["old-version", "redis-degraded", "migration-outdated"])
def test_readiness_refuses_either_unready_or_wrong_version_api(
    inputs: HostInput, monkeypatch: pytest.MonkeyPatch, api_index: int, failure: str
) -> None:
    responses = _responses(inputs)
    if failure == "old-version":
        responses[api_index] = json.dumps({"version": "d" * 40})
    else:
        responses[api_index + 1] = json.dumps(
            {
                "status": "degraded" if failure == "redis-degraded" else "not_ready",
                "database": "ok",
                "migration": "outdated" if failure == "migration-outdated" else "current",
                "redis": "degraded" if failure == "redis-degraded" else "ok",
            }
        )
    monkeypatch.setattr(host, "_compose", Mock(side_effect=responses))
    with pytest.raises(HostRefusedError, match="review_api_readiness_failed"):
        host._verify_api_readiness(inputs)


def test_readiness_does_not_print_unvalidated_health_body(
    inputs: HostInput, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(host, "_compose", Mock(return_value='{"unexpected":"private-value"}'))
    with pytest.raises(ValidationError):
        host._verify_api_readiness(inputs)
    output = capsys.readouterr()
    assert output.out == output.err == ""
