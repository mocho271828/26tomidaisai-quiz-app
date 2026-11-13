import base64
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from test_aws_review_deploy import CONFIG
from test_load_test_aws import FakeAws

from validation.aws_review import deploy
from validation.aws_review.models import DeploymentInput


@pytest.mark.parametrize("field", ["vite_survey_url", "vite_home_url"])
@pytest.mark.parametrize(
    "value",
    [
        None,
        " ",
        "/",
        "http://example.org/",
        "javascript:alert(1)",
        "https://user:password@example.org/",
        f"https://{CONFIG.review_domain_name}/",
        f"https://{CONFIG.review_domain_name.upper()}.:8443/result",
        "https://example.org/\x00",
        "https://example.org/" + "a" * 2_048,
    ],
)
def test_invalid_exit_urls_are_rejected(field: str, value: str | None) -> None:
    with pytest.raises(ValidationError):
        DeploymentInput.model_validate({**CONFIG.model_dump(), field: value})


@pytest.mark.parametrize(
    "value",
    [
        None,
        "2026-09-30T18:00:00",
        "2026-09-30",
        "2026-02-30T18:00:00+09:00",
        "2026-09-30 18:00:00+09:00",
        "1790758800",
        "2026-09-30T18:00:00+25:00",
    ],
)
def test_invalid_event_start_times_are_rejected(value: str | None) -> None:
    with pytest.raises(ValidationError):
        DeploymentInput.model_validate({**CONFIG.model_dump(), "vite_event_start_at": value})


@pytest.mark.parametrize("start", ["", "2026-09-30T18:00+09:00", "2026-09-30T09:00:00.123Z"])
def test_valid_event_start_times_are_preserved(start: str) -> None:
    config = DeploymentInput.model_validate({**CONFIG.model_dump(), "vite_event_start_at": start})
    assert config.vite_event_start_at == start


@pytest.mark.parametrize("configured", [False, True])
def test_only_gateway_receives_exact_public_build_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: bool
) -> None:
    values = {
        "VITE_EVENT_START_AT": "2026-09-30T18:00:00+09:00" if configured else "",
        "VITE_SURVEY_URL": "https://forms.example.org/?a=$(id)&b=2" if configured else "",
        "VITE_HOME_URL": "https://festival.example.org/" if configured else "",
    }
    config = DeploymentInput.model_validate(
        {**CONFIG.model_dump(), **{key.lower(): value for key, value in values.items()}}
    )
    registry = f"{config.account_id}.dkr.ecr.{config.region}.amazonaws.com"
    api = FakeAws()
    api.responses[("ecr", "get_authorization_token")] = {
        "authorizationData": [
            {
                "authorizationToken": base64.b64encode(b"AWS:test").decode(),
                "proxyEndpoint": f"https://{registry}",
            }
        ]
    }
    api.responses[("ecr", "describe_images")] = {
        "imageDetails": [{"imageDigest": "sha256:" + "b" * 64}]
    }
    runner = Mock()
    monkeypatch.setattr(deploy, "run", runner)
    deploy.build_images(api, tmp_path, config)
    builds = [call.args[1] for call in runner.call_args_list if "build" in call.args[1]]
    assert len(builds) == 2
    assert "backend/Dockerfile" in builds[0]
    assert not any(argument.startswith("VITE_") for argument in builds[0])
    assert "frontend/Dockerfile" in builds[1]
    actual = [argument for argument in builds[1] if argument.startswith("VITE_")]
    assert actual == [f"{key}={value}" for key, value in values.items()]
    for argument in actual:
        assert builds[1][builds[1].index(argument) - 1] == "--build-arg"
