from typing import Self, cast
from uuid import UUID

import pytest
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session

from quiz_app.audience.errors import AudienceServiceUnavailableError, ParticipantIdExhaustedError
from quiz_app.audience.participant_ids import ALLOCATE_PARTICIPANT_ID, allocate_participant_id

EVENT_ID = UUID("70000000-0000-4000-8000-000000000001")


class DatabaseFailure(Exception):
    def __init__(self, sqlstate: str) -> None:
        self.sqlstate = sqlstate


class AllocationSession:
    def __init__(self, value: object) -> None:
        self.value = value
        self.calls: list[tuple[str, object]] = []

    def execute(self, statement: object, parameters: object) -> Self:
        self.calls.append((str(statement), parameters))
        if isinstance(self.value, DBAPIError):
            raise self.value
        return self

    def mappings(self) -> Self:
        return self

    def one_or_none(self) -> object:
        return self.value


def allocation(
    capacity: object = 17576,
    ordinal: object = 42,
    public_label: object = "XYZ",
    version: object = "shuffled-v1",
) -> dict[str, object]:
    return dict(capacity=capacity, ordinal=ordinal, public_label=public_label, version=version)


def test_allocation_binds_sequence_and_reads_only_the_chosen_slot_in_one_roundtrip() -> None:
    session = AllocationSession(allocation())
    assert allocate_participant_id(cast(Session, session), EVENT_ID) == "XYZ"
    assert session.calls == [
        (
            str(ALLOCATE_PARTICIPANT_ID),
            {
                "event_id": EVENT_ID,
                "sequence_name": f"public.participant_id_{EVENT_ID.hex}",
                "maximum_capacity": 17576,
                "pool_version": "shuffled-v1",
            },
        )
    ]
    sql = str(ALLOCATE_PARTICIPANT_ID)
    assert sql.count("nextval(") == 1
    assert "AS MATERIALIZED" in sql
    assert "CAST(CAST(:sequence_name AS text) AS regclass)" in sql
    assert "CASE WHEN capacity BETWEEN 1 AND :maximum_capacity AND version = :pool_version" in sql
    assert "slot.ordinal = allocation.ordinal" in sql
    assert "FOR UPDATE" not in sql and "public.participants" not in sql


@pytest.mark.parametrize("capacity,ordinal", [(17576, 1), (17576, 17576), (1, 1)])
def test_first_and_last_available_id(capacity: int, ordinal: int) -> None:
    session = AllocationSession(allocation(capacity, ordinal))
    assert allocate_participant_id(cast(Session, session), EVENT_ID) == "XYZ"


@pytest.mark.parametrize(
    "value", [None, allocation(version="unknown"), allocation(-1), allocation(17577)]
)
def test_unprepared_pool_fails_without_retry(value: object) -> None:
    session = AllocationSession(value)
    with pytest.raises(AudienceServiceUnavailableError):
        allocate_participant_id(cast(Session, session), EVENT_ID)
    assert len(session.calls) == 1


def test_empty_pool_exhaustion_does_not_need_a_sequence() -> None:
    session = AllocationSession(allocation(0, None, None))
    with pytest.raises(ParticipantIdExhaustedError):
        allocate_participant_id(cast(Session, session), EVENT_ID)
    assert len(session.calls) == 1


@pytest.mark.parametrize("ordinal", [None, 0, 3, True, "1"])
def test_invalid_sequence_never_wraps_or_retries(ordinal: object) -> None:
    session = AllocationSession(allocation(2, ordinal))
    with pytest.raises(AudienceServiceUnavailableError):
        allocate_participant_id(cast(Session, session), EVENT_ID)
    assert len(session.calls) == 1


@pytest.mark.parametrize("label", [None, "", "AB", "ABCD", "aBc", "１２３", 123])
def test_slot_validation_fails_without_fallback(label: object) -> None:
    session = AllocationSession(allocation(public_label=label))
    with pytest.raises(AudienceServiceUnavailableError):
        allocate_participant_id(cast(Session, session), EVENT_ID)
    assert len(session.calls) == 1


@pytest.mark.parametrize("sqlstate", ["2200H", "42P01", "42501", "08006"])
def test_database_failure_is_exhaustion_only_for_sequence_limit(sqlstate: str) -> None:
    error = DBAPIError("hidden query", {}, DatabaseFailure(sqlstate))
    session = AllocationSession(error)
    expected = (
        ParticipantIdExhaustedError if sqlstate == "2200H" else AudienceServiceUnavailableError
    )
    with pytest.raises(expected):
        allocate_participant_id(cast(Session, session), EVENT_ID)
    assert len(session.calls) == 1
