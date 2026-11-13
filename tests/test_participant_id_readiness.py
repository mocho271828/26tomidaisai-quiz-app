from collections.abc import Mapping
from typing import cast
from unittest.mock import MagicMock, Mock
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import Connection, Engine, create_engine, literal, select
from sqlalchemy.exc import SQLAlchemyError

from quiz_app import deployment_preflight
from quiz_app.config import Settings
from quiz_app.contracts import ReadinessHealthResponse
from quiz_app.deployment_preflight import (
    EXIT_NOT_READY,
    RuntimeDependencies,
)
from quiz_app.operator.auth_service import create_operator_password_hasher
from quiz_app.participant_id_readiness import (
    PARTICIPANT_ID_READINESS,
    inspect_participant_id_readiness,
)

EVENT_ID = UUID("70000000-0000-4000-8000-000000000001")


@pytest.fixture
def pool_row() -> dict[str, object]:
    return {
        "capacity": 17576,
        "version": "shuffled-v1",
        "slot_count": 17576,
        "label_count": 17576,
        "first_ordinal": 1,
        "last_ordinal": 17576,
        "invalid_label_count": 0,
        "min_value": 1,
        "max_value": 17576,
        "start_value": 1,
        "increment_by": 1,
        "cache_size": 1,
        "cycle": False,
        "sequence_last_value": None,
        "integer_sequence": True,
        "persistent_sequence": True,
        "immutable_pool_access": True,
        "sequence_access": True,
    }


def _connection(row: Mapping[str, object] | None) -> Mock:
    connection = Mock(spec=Connection)
    connection.execute.return_value.mappings.return_value.one_or_none.return_value = row
    return connection


@pytest.mark.parametrize("invalid_type", [False, True])
def test_sqlalchemy_row_mapping_preserves_strict_field_validation(
    pool_row: dict[str, object], invalid_type: bool
) -> None:
    values = pool_row | ({"capacity": "17576"} if invalid_type else {})
    engine = create_engine("sqlite://")
    try:
        with engine.connect() as source:
            # SELECTだけで実際のRowMappingを作り、PostgreSQLへのI/Oはmockする。
            row = (
                source.execute(
                    select(*(literal(value).label(key) for key, value in values.items()))
                )
                .mappings()
                .one()
            )
        connection = cast(Connection, _connection(row))
        if invalid_type:
            with pytest.raises(ValidationError) as failure:
                inspect_participant_id_readiness(connection, EVENT_ID)
            assert failure.value.errors(include_input=False)[0]["loc"] == ("capacity",)
        else:
            assert inspect_participant_id_readiness(connection, EVENT_ID)
    finally:
        engine.dispose()


@pytest.mark.parametrize("last_value", [None, 1, 17575])
def test_prepared_pool_can_be_checked_without_consuming_an_id(
    pool_row: dict[str, object], last_value: int | None
) -> None:
    connection = _connection(pool_row | {"sequence_last_value": last_value})

    assert inspect_participant_id_readiness(cast(Connection, connection), EVENT_ID)
    connection.execute.assert_called_once_with(
        PARTICIPANT_ID_READINESS,
        {
            "event_id": EVENT_ID,
            "sequence_name": f"participant_id_{EVENT_ID.hex}",
            "sequence_regclass": f"public.participant_id_{EVENT_ID.hex}",
        },
    )
    statement = str(PARTICIPANT_ID_READINESS).upper()
    assert statement.lstrip().startswith("SELECT")
    assert "NEXTVAL(" not in statement and "SETVAL(" not in statement
    assert "GRANT " not in statement and "CREATE " not in statement


def test_missing_pool_is_not_ready() -> None:
    assert not inspect_participant_id_readiness(cast(Connection, _connection(None)), EVENT_ID)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("capacity", 0),
        ("version", "unknown"),
        ("slot_count", 17575),
        ("label_count", 17575),
        ("first_ordinal", 2),
        ("last_ordinal", 17577),
        ("invalid_label_count", 1),
        ("min_value", 0),
        ("max_value", 17577),
        ("start_value", 2),
        ("increment_by", 2),
        ("cache_size", 10),
        ("cycle", True),
        ("sequence_last_value", 17576),
        ("sequence_last_value", 0),
        ("integer_sequence", False),
        ("persistent_sequence", False),
        ("immutable_pool_access", False),
        ("sequence_access", False),
        ("sequence_access", None),
        ("min_value", None),
    ],
)
def test_incomplete_exhausted_or_unsafe_pool_is_not_ready(
    pool_row: dict[str, object], field: str, value: object
) -> None:
    connection = _connection(pool_row | {field: value})
    assert not inspect_participant_id_readiness(cast(Connection, connection), EVENT_ID)


@pytest.mark.parametrize(("field", "value"), [("capacity", "17576"), ("sequence_access", 1)])
def test_unexpected_database_types_are_not_coerced(
    pool_row: dict[str, object], field: str, value: object
) -> None:
    connection = _connection(pool_row | {field: value})
    with pytest.raises(ValidationError):
        inspect_participant_id_readiness(cast(Connection, connection), EVENT_ID)


@pytest.mark.parametrize("pool_failure", ["missing", "database-error"])
def test_preflight_blocks_incomplete_pool_and_redacts_database_failure(
    monkeypatch: pytest.MonkeyPatch, pool_failure: str
) -> None:
    connection = MagicMock(spec=Connection)
    connection.__enter__.return_value = connection
    connection.scalar.side_effect = [1, 1, 1, 6]
    connection.execute.return_value.mappings.return_value.all.return_value = [
        {
            "username": "rehearsal-operator",
            "password_hash": create_operator_password_hasher().hash("test-only-password"),
            "role": "Operator",
            "is_demo": True,
            "is_revoked": False,
        }
    ]
    engine = MagicMock(spec=Engine)
    engine.connect.return_value = connection
    dependencies = RuntimeDependencies(engine=engine, redis_client=MagicMock())
    settings = Settings.model_construct(
        aws_demo_expected_event_id=EVENT_ID, aws_demo_round_scope="rehearsal-six-round"
    )

    def inspect_pool(selected: Connection, event_id: UUID) -> bool:
        assert selected is connection and event_id == EVENT_ID
        if pool_failure == "database-error":
            raise SQLAlchemyError("private-database-password")
        return False

    monkeypatch.setattr(deployment_preflight, "inspect_participant_id_readiness", inspect_pool)
    output: list[str] = []
    errors: list[str] = []
    result = deployment_preflight.main(
        settings_loader=lambda: settings,
        dependency_factory=lambda _: dependencies,
        readiness_inspector=lambda *_: ReadinessHealthResponse(
            status="ready", database="ok", migration="current", redis="ok"
        ),
        output=output.append,
        error_output=errors.append,
    )

    if pool_failure == "missing":
        assert result == EXIT_NOT_READY
        assert '"participant_ids":"invalid"' in output[1]
        assert errors == []
    else:
        assert result == EXIT_NOT_READY
        assert errors == ['{"status":"dependency_error","dependency":"runtime"}']
    assert "private-database-password" not in "".join(output + errors)
    engine.dispose.assert_called_once()
