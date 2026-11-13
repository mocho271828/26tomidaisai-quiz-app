from pathlib import Path
from unittest.mock import MagicMock, Mock
from uuid import UUID

import psycopg
import pytest
from pydantic import SecretStr, ValidationError

from quiz_app.dependency_hosts import ValidatedDependencyHost
from quiz_app.rehearsal.initialization import RehearsalInitializationSecrets
from validation.aws_review import participant_ids
from validation.aws_review.host_contracts import HostRefusedError
from validation.aws_review.host_data import HostSecrets

EVENT_ID = UUID("70000000-0000-4000-8000-000000000001")
PRIVATE_VALUE = "Aa7_Qz8t3A6p9B2r5C7x4N1m6L9kQ2w7E5y0"


@pytest.fixture
def review_secrets() -> HostSecrets:
    def database_url(role: str) -> SecretStr:
        return SecretStr(
            f"postgresql+psycopg://{role}:{PRIVATE_VALUE}@"
            "review.ap-northeast-1.rds.amazonaws.com:5432/quiz?sslmode=verify-full"
        )

    return HostSecrets(
        initialization=RehearsalInitializationSecrets(
            admin_database_url=database_url("quizadmin"),
            app_database_url=database_url("rehearsal_app"),
            operator_username="rehearsal-operator",
            operator_password=SecretStr(PRIVATE_VALUE),
        ),
        redis_url=SecretStr("unused"),
        session_secret=SecretStr("unused"),
        backup_key=SecretStr("unused"),
    )


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, Mock, MagicMock]:
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.side_effect = [
        {"database_name": "quiz", "current_role": "quizadmin", "session_role": "quizadmin"},
        {"present": False},
        {"present": True},
    ]
    connection.cursor.return_value = cursor
    connect = Mock(return_value=connection)
    monkeypatch.setattr(participant_ids.psycopg, "connect", connect)
    monkeypatch.setattr(
        participant_ids,
        "validate_remote_dependency_host",
        lambda *_: ValidatedDependencyHost(
            "database", "review.ap-northeast-1.rds.amazonaws.com", ("10.80.2.20",), "10.80.2.20"
        ),
    )
    return cursor, connect, connection


def test_fresh_review_event_prepares_pool_then_sequence_usage_in_one_transaction(
    review_secrets: HostSecrets, database: tuple[MagicMock, Mock, MagicMock]
) -> None:
    cursor, connect, connection = database

    participant_ids.prepare_review_participant_ids(review_secrets, EVENT_ID)

    options = connect.call_args.kwargs
    assert options["sslmode"] == "verify-full"
    assert options["hostaddr"] == "10.80.2.20"
    assert options["host"] == "review.ap-northeast-1.rds.amazonaws.com"
    assert options["dbname"] == "quiz" and options["user"] == "quizadmin"
    assert options["connect_timeout"] == 5
    assert options["options"] == "-c statement_timeout=30000 -c lock_timeout=5000"
    assert "autocommit" not in options
    calls = cursor.execute.call_args_list
    assert calls[2].args[1] == ("rehearsal_app",)
    assert calls[3].args[1] == (EVENT_ID,)
    preparation = calls[4].args[0].as_string()
    assert calls[4].kwargs == {"prepare": False}
    assert "INSERT INTO public.participant_id_pools" in preparation
    assert "INSERT INTO public.participant_id_slots" in preparation
    assert "CREATE SEQUENCE" in preparation
    assert "COMMIT;" not in preparation and "\\set" not in preparation
    assert calls[5].args[0].as_string() == (
        f'GRANT USAGE ON SEQUENCE public."participant_id_{EVENT_ID.hex}" TO "rehearsal_app"'
    )
    connection.__exit__.assert_called_once_with(None, None, None)


@pytest.mark.parametrize("refusal", ["identity", "active-app", "existing-participants"])
def test_unexpected_database_state_is_rejected_before_pool_creation(
    review_secrets: HostSecrets, database: tuple[MagicMock, Mock, MagicMock], refusal: str
) -> None:
    cursor, _, _ = database
    cursor.fetchone.side_effect = [
        {
            "database_name": "production" if refusal == "identity" else "quiz",
            "current_role": "quizadmin",
            "session_role": "quizadmin",
        },
        {"present": refusal == "active-app"},
        {"present": refusal != "existing-participants"},
    ]
    with pytest.raises((HostRefusedError, ValidationError)):
        participant_ids.prepare_review_participant_ids(review_secrets, EVENT_ID)

    assert all(isinstance(call.args[0], str) for call in cursor.execute.call_args_list)


@pytest.mark.parametrize("failed_statement", [4, 5])
def test_pool_or_grant_failure_exits_transaction_without_exposing_driver_error(
    review_secrets: HostSecrets,
    database: tuple[MagicMock, Mock, MagicMock],
    failed_statement: int,
) -> None:
    cursor, _, connection = database
    failure = psycopg.OperationalError(PRIVATE_VALUE)
    cursor.execute.side_effect = [None] * failed_statement + [failure]

    with pytest.raises(HostRefusedError, match="review_participant_id_preparation_failed") as error:
        participant_ids.prepare_review_participant_ids(review_secrets, EVENT_ID)

    assert PRIVATE_VALUE not in str(error.value)
    assert error.value.__suppress_context__
    assert connection.__exit__.call_args.args[0] is psycopg.OperationalError
    connection.commit.assert_not_called()


def test_relative_ca_path_is_rejected_before_connecting(
    review_secrets: HostSecrets, database: tuple[MagicMock, Mock, MagicMock]
) -> None:
    _, connect, _ = database
    with pytest.raises(HostRefusedError, match="review_participant_id_database_target_invalid"):
        participant_ids.prepare_review_participant_ids(review_secrets, EVENT_ID, Path("ca.pem"))
    connect.assert_not_called()
