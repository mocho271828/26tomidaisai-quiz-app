from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from websockets.exceptions import ConnectionClosedError

from quiz_app.audience.contracts import AudienceEventResponse, ParticipantCommonStateResponse
from quiz_app.quiz_configuration import ResultVisibility
from quiz_app.realtime.contracts import (
    HeartbeatMessage,
    ParticipantStateMessage,
    PollingRequiredMessage,
)
from validation.local_demo_acceptance import (
    EVENT_ID,
    ValidationFailure,
    receive_state_message,
    validate_degraded_connection,
)


class RecordedConnection:
    def __init__(self, messages: list[str | bytes | ConnectionClosedError]) -> None:
        self.messages = iter(messages)

    def recv(self, timeout: float) -> str | bytes:
        assert timeout > 0
        message = next(self.messages)
        if isinstance(message, ConnectionClosedError):
            raise message
        return message


def snapshot(version: int, *, event_id: UUID = EVENT_ID) -> ParticipantStateMessage:
    return ParticipantStateMessage(
        type="participant.state",
        audience="participant",
        event_id=EVENT_ID,
        stream_id=uuid4(),
        sequence=version,
        state=ParticipantCommonStateResponse(
            award_ceremony=None,
            projected_at=datetime.now(UTC),
            event=AudienceEventResponse(
                timing_basis="server_received_v1",
                arrival_grace_ms=0,
                clock_tolerance_ms=0,
                client_ahead_tolerance_ms=0,
                event_id=event_id,
                event_title="acceptance",
                event_state="LobbyOpen",
                event_version=version,
                start_delay_seconds=3,
            ),
            current_round=None,
            latest_result=None,
            result_visibility=ResultVisibility(
                distribution=True, personal_ranking=False, overall_ranking=False
            ),
            latest_ranking_snapshot_version=None,
        ),
    )


def test_acceptance_waits_for_current_snapshot_after_heartbeat_and_old_version() -> None:
    heartbeat = HeartbeatMessage(
        type="heartbeat",
        audience="participant",
        event_id=EVENT_ID,
        stream_id=uuid4(),
        sequence=1,
    )
    current = snapshot(3)
    connection = RecordedConnection(
        [heartbeat.model_dump_json(), snapshot(1).model_dump_json(), current.model_dump_json()]
    )

    assert receive_state_message(connection, "participant", 3) == current


def test_acceptance_rejects_misrouted_nested_projection() -> None:
    connection = RecordedConnection([snapshot(1, event_id=uuid4()).model_dump_json()])
    with pytest.raises(ValidationFailure, match="projection was misrouted"):
        receive_state_message(connection, "participant")


@pytest.mark.parametrize(
    "message,reason",
    [
        (b"binary", "binary message"),
        (
            '{"type":"polling.required","reason":"realtime_unavailable","poll_after_ms":5000}',
            "unexpectedly required polling",
        ),
    ],
)
def test_healthy_acceptance_rejects_invalid_transport_state(
    message: str | bytes, reason: str
) -> None:
    with pytest.raises(ValidationFailure, match=reason):
        receive_state_message(RecordedConnection([message]), "participant")


def test_degraded_acceptance_keeps_state_and_heartbeat_on_same_connection() -> None:
    state = snapshot(1)
    heartbeat = HeartbeatMessage(
        type="heartbeat",
        audience="participant",
        event_id=EVENT_ID,
        stream_id=state.stream_id,
        sequence=2,
    )
    validate_degraded_connection(
        RecordedConnection([state.model_dump_json(), heartbeat.model_dump_json()]), "participant"
    )


def test_degraded_acceptance_rejects_redis_failure_fanout_to_polling() -> None:
    polling = PollingRequiredMessage(
        type="polling.required", reason="realtime_unavailable", poll_after_ms=5_000
    )
    with pytest.raises(ValidationFailure, match="unexpectedly required polling"):
        validate_degraded_connection(
            RecordedConnection([snapshot(1).model_dump_json(), polling.model_dump_json()]),
            "participant",
        )
