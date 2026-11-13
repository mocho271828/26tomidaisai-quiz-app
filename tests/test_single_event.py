from typing import Self, cast
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlmodel import Session

import quiz_app.single_event as single_event_module
from quiz_app.domain.errors import ResourceNotFoundError
from quiz_app.single_event import (
    SingleEventConfigurationError,
    SingleEventService,
    resolve_single_event_id,
)

EVENT_ID = UUID("90000000-0000-4000-8000-000000000001")
OTHER_EVENT_ID = UUID("90000000-0000-4000-8000-000000000002")


class FakeScalarResult:
    def __init__(self, values: list[UUID]) -> None:
        self._values = values

    def all(self) -> list[UUID]:
        return self._values


class FakeSingleEventSession:
    def __init__(
        self,
        event_ids: list[UUID],
        round_event_id: UUID | None = None,
    ) -> None:
        self._event_ids = event_ids
        self._round_event_id = round_event_id

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def scalars(self, _: object) -> FakeScalarResult:
        return FakeScalarResult(self._event_ids)

    def scalar(self, _: object) -> UUID | None:
        return self._round_event_id


def test_single_event_resolver_returns_the_only_event() -> None:
    event_id = resolve_single_event_id(
        cast(Session, FakeSingleEventSession([EVENT_ID]))
    )

    assert event_id == EVENT_ID


@pytest.mark.parametrize("event_ids", [[], [EVENT_ID, OTHER_EVENT_ID]])
def test_single_event_resolver_fails_closed_without_exactly_one_event(
    event_ids: list[UUID],
) -> None:
    with pytest.raises(
        SingleEventConfigurationError,
        match="single_event_configuration_required",
    ):
        resolve_single_event_id(cast(Session, FakeSingleEventSession(event_ids)))


def test_round_must_belong_to_the_single_event(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSingleEventSession([EVENT_ID], round_event_id=EVENT_ID)
    monkeypatch.setattr(single_event_module, "Session", lambda _: session)
    service = SingleEventService(cast(Engine, object()))

    assert service.require_round(UUID(int=10)) == EVENT_ID

    session._round_event_id = OTHER_EVENT_ID
    with pytest.raises(ResourceNotFoundError, match="round_not_found"):
        service.require_round(UUID(int=10))
