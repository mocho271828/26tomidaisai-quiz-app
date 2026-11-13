from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from quiz_app.audience.contracts import ParticipantCommonStateResponse
from quiz_app.audience.projection import (
    AudienceProjectionService,
    get_audience_projection_service,
)
from quiz_app.contracts import ApiErrorResponse
from quiz_app.domain.errors import RoundBackupFailedError
from quiz_app.operator.auth_service import OperatorPrincipal
from quiz_app.operator.control_contracts import (
    AwardCeremonyMutationRequest,
    NextRoundMutationRequest,
    OperatorLatestRankingResponse,
    OperatorMutationRequest,
    OperatorMutationResponse,
    OperatorStateResponse,
    QuestionSelectionMutationRequest,
    ResultVisibilityMutationRequest,
    ScheduleRoundMutationRequest,
)
from quiz_app.operator.dependencies import (
    require_operator_mutation,
    require_operator_principal,
)
from quiz_app.operator.final_round import FinalRoundService, get_final_round_service
from quiz_app.operator.final_round_contracts import FinalRoundMutationRequest, FinalRoundResponse
from quiz_app.operator.mutations import (
    OperatorMutationService,
    get_operator_mutation_service,
)
from quiz_app.operator.projection import (
    OperatorProjectionService,
    get_operator_projection_service,
)
from quiz_app.operator.response_time_contracts import (
    OperatorResponseTimeFilters,
    OperatorResponseTimesResponse,
)
from quiz_app.operator.response_times import (
    OperatorResponseTimeService,
    get_operator_response_time_service,
)
from quiz_app.realtime.redis_adapter import (
    RealtimePublisher,
    audiences_for_invalidation,
    get_realtime_publisher,
)
from quiz_app.single_event import require_single_event_id, require_single_event_round

router = APIRouter(prefix="/api/v1/operator", tags=["operator-control"])
CONTROL_ERROR_RESPONSES = {
    status.HTTP_401_UNAUTHORIZED: {"model": ApiErrorResponse},
    status.HTTP_403_FORBIDDEN: {"model": ApiErrorResponse},
    status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse},
    status.HTTP_409_CONFLICT: {"model": ApiErrorResponse},
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ApiErrorResponse},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ApiErrorResponse},
}
NO_STORE = "no-store"


@router.get(
    "/state",
    response_model=OperatorStateResponse,
    responses=CONTROL_ERROR_RESPONSES,
)
def get_event_state(
    response: Response,
    _: Annotated[OperatorPrincipal, Depends(require_operator_principal)],
    projection_service: Annotated[
        OperatorProjectionService,
        Depends(get_operator_projection_service),
    ],
    event_id: Annotated[UUID, Depends(require_single_event_id)],
) -> OperatorStateResponse:
    response.headers["Cache-Control"] = NO_STORE
    return projection_service.get_event_state(event_id)


@router.get(
    "/participant-preview",
    response_model=ParticipantCommonStateResponse,
    responses=CONTROL_ERROR_RESPONSES,
)
def get_participant_preview(
    response: Response,
    _: Annotated[OperatorPrincipal, Depends(require_operator_principal)],
    projection_service: Annotated[
        AudienceProjectionService,
        Depends(get_audience_projection_service),
    ],
    event_id: Annotated[UUID, Depends(require_single_event_id)],
) -> ParticipantCommonStateResponse:
    """運営認証下でも参加者の公開範囲を保ち、本人情報なしのプレビューを返す。"""

    response.headers["Cache-Control"] = NO_STORE
    return projection_service.get_common_snapshot(event_id, normal=True).state


@router.get(
    "/final-round",
    response_model=FinalRoundResponse,
    responses=CONTROL_ERROR_RESPONSES,
)
def get_final_round(
    response: Response,
    _: Annotated[OperatorPrincipal, Depends(require_operator_principal)],
    service: Annotated[FinalRoundService, Depends(get_final_round_service)],
    event_id: Annotated[UUID, Depends(require_single_event_id)],
) -> FinalRoundResponse:
    response.headers["Cache-Control"] = NO_STORE
    return service.get_final_round(event_id)


@router.post(
    "/final-round",
    response_model=OperatorMutationResponse,
    responses=CONTROL_ERROR_RESPONSES,
)
def set_final_round(
    request: FinalRoundMutationRequest,
    principal: Annotated[OperatorPrincipal, Depends(require_operator_mutation)],
    mutation_service: Annotated[OperatorMutationService, Depends(get_operator_mutation_service)],
    realtime_publisher: Annotated[RealtimePublisher, Depends(get_realtime_publisher)],
    event_id: Annotated[UUID, Depends(require_single_event_id)],
) -> OperatorMutationResponse:
    result = mutation_service.set_final_round(principal, event_id, request)
    realtime_publisher.notify(
        result.event_id, audiences_for_invalidation("operator_mutation"), cause="operator_mutation"
    )
    return result


@router.post(
    "/award-ceremony",
    response_model=OperatorMutationResponse,
    responses=CONTROL_ERROR_RESPONSES,
)
def set_award_ceremony(
    request: AwardCeremonyMutationRequest,
    principal: Annotated[OperatorPrincipal, Depends(require_operator_mutation)],
    mutation_service: Annotated[OperatorMutationService, Depends(get_operator_mutation_service)],
    realtime_publisher: Annotated[RealtimePublisher, Depends(get_realtime_publisher)],
    event_id: Annotated[UUID, Depends(require_single_event_id)],
) -> OperatorMutationResponse:
    result = mutation_service.set_award_ceremony(principal, event_id, request)
    realtime_publisher.notify(
        result.event_id, audiences_for_invalidation("operator_mutation"), cause="operator_mutation"
    )
    return result


@router.get(
    "/ranking",
    response_model=OperatorLatestRankingResponse | None,
    responses=CONTROL_ERROR_RESPONSES,
)
def get_latest_ranking(
    response: Response,
    _: Annotated[OperatorPrincipal, Depends(require_operator_principal)],
    projection_service: Annotated[
        OperatorProjectionService,
        Depends(get_operator_projection_service),
    ],
    event_id: Annotated[UUID, Depends(require_single_event_id)],
) -> OperatorLatestRankingResponse | None:
    response.headers["Cache-Control"] = NO_STORE
    return projection_service.get_latest_ranking(event_id)


@router.get(
    "/response-times",
    response_model=OperatorResponseTimesResponse,
    responses=CONTROL_ERROR_RESPONSES,
)
def get_response_times(
    response: Response,
    _: Annotated[OperatorPrincipal, Depends(require_operator_principal)],
    service: Annotated[
        OperatorResponseTimeService,
        Depends(get_operator_response_time_service),
    ],
    event_id: Annotated[UUID, Depends(require_single_event_id)],
    round_id: UUID | None = None,
) -> OperatorResponseTimesResponse:
    """認証済み運営へ、単一開催の確定済み回答速度をキャッシュ禁止で返す。"""

    response.headers["Cache-Control"] = NO_STORE
    return service.get_response_times(
        event_id,
        OperatorResponseTimeFilters(round_id=round_id),
    )


@router.post(
    "/rounds/{round_id}/prepare",
    response_model=OperatorMutationResponse,
    responses=CONTROL_ERROR_RESPONSES,
)
def begin_preparation(
    round_id: UUID,
    request: OperatorMutationRequest,
    principal: Annotated[OperatorPrincipal, Depends(require_operator_mutation)],
    _: Annotated[UUID, Depends(require_single_event_round)],
    mutation_service: Annotated[
        OperatorMutationService,
        Depends(get_operator_mutation_service),
    ],
    realtime_publisher: Annotated[RealtimePublisher, Depends(get_realtime_publisher)],
) -> OperatorMutationResponse:
    result = mutation_service.begin_preparation(principal, round_id, request)
    realtime_publisher.notify(
        result.event_id, audiences_for_invalidation("operator_mutation"), cause="operator_mutation"
    )
    return result


@router.post(
    "/rounds/{round_id}/question",
    response_model=OperatorMutationResponse,
    responses=CONTROL_ERROR_RESPONSES,
)
def select_round_question(
    round_id: UUID,
    request: QuestionSelectionMutationRequest,
    principal: Annotated[OperatorPrincipal, Depends(require_operator_mutation)],
    _: Annotated[UUID, Depends(require_single_event_round)],
    mutation_service: Annotated[
        OperatorMutationService,
        Depends(get_operator_mutation_service),
    ],
    realtime_publisher: Annotated[RealtimePublisher, Depends(get_realtime_publisher)],
) -> OperatorMutationResponse:
    result = mutation_service.select_round_question(principal, round_id, request)
    realtime_publisher.notify(
        result.event_id, audiences_for_invalidation("operator_mutation"), cause="operator_mutation"
    )
    return result


@router.post(
    "/rounds/{round_id}/schedule",
    response_model=OperatorMutationResponse,
    responses=CONTROL_ERROR_RESPONSES,
)
def schedule_round(
    round_id: UUID,
    request: ScheduleRoundMutationRequest,
    principal: Annotated[OperatorPrincipal, Depends(require_operator_mutation)],
    _: Annotated[UUID, Depends(require_single_event_round)],
    mutation_service: Annotated[
        OperatorMutationService,
        Depends(get_operator_mutation_service),
    ],
    realtime_publisher: Annotated[RealtimePublisher, Depends(get_realtime_publisher)],
) -> OperatorMutationResponse:
    result = mutation_service.schedule_round(principal, round_id, request)
    realtime_publisher.notify(
        result.event_id, audiences_for_invalidation("operator_mutation"), cause="operator_mutation"
    )
    return result


@router.post(
    "/rounds/{round_id}/publish-results",
    response_model=OperatorMutationResponse,
    responses=CONTROL_ERROR_RESPONSES,
)
def publish_round_results(
    round_id: UUID,
    request: OperatorMutationRequest,
    principal: Annotated[OperatorPrincipal, Depends(require_operator_mutation)],
    _: Annotated[UUID, Depends(require_single_event_round)],
    mutation_service: Annotated[
        OperatorMutationService,
        Depends(get_operator_mutation_service),
    ],
    realtime_publisher: Annotated[RealtimePublisher, Depends(get_realtime_publisher)],
) -> OperatorMutationResponse:
    result = mutation_service.publish_round_results(principal, round_id, request)
    realtime_publisher.notify(
        result.event_id, audiences_for_invalidation("operator_mutation"), cause="operator_mutation"
    )
    return result


@router.post(
    "/rounds/{round_id}/retry-results",
    response_model=OperatorMutationResponse,
    responses=CONTROL_ERROR_RESPONSES,
)
def retry_round_results(
    round_id: UUID,
    request: OperatorMutationRequest,
    principal: Annotated[OperatorPrincipal, Depends(require_operator_mutation)],
    _: Annotated[UUID, Depends(require_single_event_round)],
    mutation_service: Annotated[
        OperatorMutationService,
        Depends(get_operator_mutation_service),
    ],
    realtime_publisher: Annotated[RealtimePublisher, Depends(get_realtime_publisher)],
) -> OperatorMutationResponse:
    result = mutation_service.retry_round_results(principal, round_id, request)
    realtime_publisher.notify(
        result.event_id, audiences_for_invalidation("operator_mutation"), cause="operator_mutation"
    )
    return result


@router.post(
    "/rounds/{round_id}/advance",
    response_model=OperatorMutationResponse,
    responses=CONTROL_ERROR_RESPONSES,
)
def advance_round(
    round_id: UUID,
    request: NextRoundMutationRequest,
    principal: Annotated[OperatorPrincipal, Depends(require_operator_mutation)],
    _: Annotated[UUID, Depends(require_single_event_round)],
    mutation_service: Annotated[
        OperatorMutationService,
        Depends(get_operator_mutation_service),
    ],
    realtime_publisher: Annotated[RealtimePublisher, Depends(get_realtime_publisher)],
) -> OperatorMutationResponse:
    try:
        result = mutation_service.advance_round(principal, round_id, request)
    except RoundBackupFailedError as error:
        realtime_publisher.notify(
            error.event_id,
            audiences_for_invalidation("operator_mutation"),
            cause="operator_mutation",
        )
        raise
    realtime_publisher.notify(
        result.event_id, audiences_for_invalidation("operator_mutation"), cause="operator_mutation"
    )
    return result


@router.post(
    "/rounds/{round_id}/complete-event",
    response_model=OperatorMutationResponse,
    responses=CONTROL_ERROR_RESPONSES,
)
def complete_event(
    round_id: UUID,
    request: OperatorMutationRequest,
    principal: Annotated[OperatorPrincipal, Depends(require_operator_mutation)],
    _: Annotated[UUID, Depends(require_single_event_round)],
    mutation_service: Annotated[
        OperatorMutationService,
        Depends(get_operator_mutation_service),
    ],
    realtime_publisher: Annotated[RealtimePublisher, Depends(get_realtime_publisher)],
) -> OperatorMutationResponse:
    try:
        result = mutation_service.complete_event(principal, round_id, request)
    except RoundBackupFailedError as error:
        realtime_publisher.notify(
            error.event_id,
            audiences_for_invalidation("operator_mutation"),
            cause="operator_mutation",
        )
        raise
    realtime_publisher.notify(
        result.event_id, audiences_for_invalidation("operator_mutation"), cause="operator_mutation"
    )
    return result


@router.post(
    "/result-visibility",
    response_model=OperatorMutationResponse,
    responses=CONTROL_ERROR_RESPONSES,
)
def set_result_visibility(
    request: ResultVisibilityMutationRequest,
    principal: Annotated[OperatorPrincipal, Depends(require_operator_mutation)],
    mutation_service: Annotated[
        OperatorMutationService,
        Depends(get_operator_mutation_service),
    ],
    realtime_publisher: Annotated[RealtimePublisher, Depends(get_realtime_publisher)],
    event_id: Annotated[UUID, Depends(require_single_event_id)],
) -> OperatorMutationResponse:
    result = mutation_service.set_result_visibility(principal, event_id, request)
    realtime_publisher.notify(
        result.event_id, audiences_for_invalidation("operator_mutation"), cause="operator_mutation"
    )
    return result
