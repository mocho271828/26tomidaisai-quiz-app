from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Response, status

from quiz_app.config import (
    OPERATOR_CSRF_COOKIE_NAME,
    OPERATOR_SESSION_COOKIE_NAME,
    Settings,
    get_settings,
)
from quiz_app.contracts import ApiErrorResponse
from quiz_app.operator.auth_service import (
    OperatorAuthService,
    OperatorPrincipal,
    get_operator_auth_service,
)
from quiz_app.operator.contracts import (
    OperatorIdentityResponse,
    OperatorLoginRequest,
    OperatorSessionResponse,
)
from quiz_app.operator.dependencies import (
    require_operator_mutation,
    require_operator_principal,
)

router = APIRouter(prefix="/api/v1/operator", tags=["operator"])
AUTH_ERROR_RESPONSES = {
    status.HTTP_401_UNAUTHORIZED: {"model": ApiErrorResponse},
    status.HTTP_403_FORBIDDEN: {"model": ApiErrorResponse},
    status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse},
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ApiErrorResponse},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ApiErrorResponse},
}


@router.post(
    "/auth/login",
    response_model=OperatorSessionResponse,
    responses=AUTH_ERROR_RESPONSES,
)
def login(
    request: OperatorLoginRequest,
    response: Response,
    auth_service: Annotated[OperatorAuthService, Depends(get_operator_auth_service)],
    settings: Annotated[Settings, Depends(get_settings)],
    existing_session_token: Annotated[
        str | None,
        Cookie(alias=OPERATOR_SESSION_COOKIE_NAME),
    ] = None,
) -> OperatorSessionResponse:
    """local demo credentialをDB session Cookieへ交換する。"""

    created = auth_service.login(
        request.username,
        request.password,
        existing_session_token,
    )
    max_age = settings.operator_session_ttl_minutes * 60
    response.set_cookie(
        key=OPERATOR_SESSION_COOKIE_NAME,
        value=created.session_token,
        max_age=max_age,
        path="/api/v1/operator",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        key=OPERATOR_CSRF_COOKIE_NAME,
        value=created.csrf_token,
        max_age=max_age,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=False,
        samesite="strict",
    )
    return _session_response(created.principal)


@router.get(
    "/auth/session",
    response_model=OperatorSessionResponse,
    responses=AUTH_ERROR_RESPONSES,
)
def get_session(
    principal: Annotated[OperatorPrincipal, Depends(require_operator_principal)],
) -> OperatorSessionResponse:
    return _session_response(principal)


@router.post(
    "/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=AUTH_ERROR_RESPONSES,
)
def logout(
    response: Response,
    principal: Annotated[OperatorPrincipal, Depends(require_operator_mutation)],
    auth_service: Annotated[OperatorAuthService, Depends(get_operator_auth_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """DB sessionを失効し、browser Cookieを同じscopeで削除する。"""

    auth_service.logout(principal.session_id)
    response.delete_cookie(
        OPERATOR_SESSION_COOKIE_NAME,
        path="/api/v1/operator",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(
        OPERATOR_CSRF_COOKIE_NAME,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=False,
        samesite="strict",
    )


def _session_response(principal: OperatorPrincipal) -> OperatorSessionResponse:
    return OperatorSessionResponse(
        operator=OperatorIdentityResponse(
            account_id=principal.account_id,
            username=principal.username,
            role=principal.role,
        )
    )
