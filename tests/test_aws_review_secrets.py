import base64
import hashlib
import json
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import SecretStr

from validation.aws_review import host_data
from validation.aws_review.host_contracts import AwsClient, HostInput, HostRefusedError


@pytest.fixture
def review_inputs() -> HostInput:
    account = "123456789012"
    repository = f"{account}.dkr.ecr.ap-northeast-1.amazonaws.com/gs-rehearsal-review"
    prefix = f"arn:aws:secretsmanager:ap-northeast-1:{account}:secret:"
    return HostInput(
        account_id=account,
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
        database_master_secret_arn=prefix + "rds!db-1234-AbCdEf",
        redis_auth_secret_arn=prefix + "gs-rehearsal-review-redis-AbCdEf",
        runtime_secret_arn=prefix + "gs-rehearsal-review-runtime-AbCdEf",
    )


@pytest.mark.parametrize("requires_retry", [False, True])
def test_session_derivation_satisfies_runtime_without_changing_other_secrets(
    review_inputs: HostInput, monkeypatch: pytest.MonkeyPatch, requires_retry: bool
) -> None:
    # この合成seedの従来session候補には数字がなく、既存Settingsが拒否する。
    seed = SecretStr(
        hashlib.sha256(b"review-synthetic-input-444").hexdigest()
        if requires_retry
        else "Az8t3A6p9B2r5C7x4N1m6L9kQ2w7E5y0R4u8I6o3P9s1D5f7G2h4J6k8L0m2N4p6Q"
    )
    values = {
        review_inputs.database_master_secret_arn: {
            "username": "quizadmin",
            "password": "Qz8t3A6p9B2r5C7x4N1m6L9k",
        },
        review_inputs.redis_auth_secret_arn: {"token": "Zx8t3A6p9B2r5C7x4N1m6L9k"},
        review_inputs.runtime_secret_arn: {"session_secret": seed.get_secret_value()},
    }
    client = Mock(spec=AwsClient)
    client.get_secret_value.side_effect = lambda **kwargs: {
        "ARN": kwargs["SecretId"],
        "SecretString": json.dumps(values[kwargs["SecretId"]]),
    }
    monkeypatch.setattr(host_data, "aws_client", lambda _: client)
    previous_session = host_data._derive(seed, "review-session-v1")
    assert any(character.isdigit() for character in previous_session.get_secret_value()) is (
        not requires_retry
    )

    secrets = host_data.load_secrets(review_inputs)
    settings = host_data.runtime_settings(review_inputs, secrets, uuid4())
    assert settings.session_secret == secrets.session_secret
    assert (secrets.session_secret != previous_session) is requires_retry
    assert host_data.load_secrets(review_inputs) == secrets
    assert secrets.initialization.operator_password == host_data._derive(seed, "review-operator-v1")
    assert secrets.backup_key == host_data._derive(seed, "review-backup-v1", padded=True)
    assert len(base64.urlsafe_b64decode(secrets.backup_key.get_secret_value())) == 32
    assert secrets.backup_key.get_secret_value().endswith("=")
    assert host_data._derive(seed, "review-app-database-v1").get_secret_value() in (
        secrets.initialization.app_database_url.get_secret_value()
    )
    assert [str(origin).rstrip("/") for origin in settings.realtime_allowed_origins] == [
        review_inputs.public_url
    ]


@pytest.mark.parametrize(
    "invalid_candidate",
    [
        "aB_cD-eF" * 5 + "ghi",
        "a1_b2-c3" * 5 + "xyz",
        "A1_B2-C3" * 5 + "XYZ",
        "aB3_token" * 5,
        "aB3" * 14 + "a",
    ],
)
def test_invalid_session_candidates_use_a_distinct_retry_purpose(
    monkeypatch: pytest.MonkeyPatch, invalid_candidate: str
) -> None:
    seed = SecretStr("synthetic-seed")
    valid = host_data._derive(SecretStr("stable-synthetic-seed"), "review-session-v1")
    derive = Mock(side_effect=[SecretStr(invalid_candidate), valid])
    monkeypatch.setattr(host_data, "_derive", derive)
    assert host_data._derive_session_secret(seed) == valid
    assert derive.call_args_list[0].args == (seed, "review-session-v1")
    assert derive.call_args_list[1].args == (seed, "review-session-v1-retry-1")
    assert derive.call_count == 2


def test_invalid_session_derivation_is_bounded_and_reports_no_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    derive = Mock(return_value=SecretStr("A" * 43))
    monkeypatch.setattr(host_data, "_derive", derive)
    with pytest.raises(HostRefusedError) as error:
        host_data._derive_session_secret(SecretStr("must-not-be-reported"))
    assert str(error.value) == "review_session_secret_derivation_failed"
    assert derive.call_count == 32
