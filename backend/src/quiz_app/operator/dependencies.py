from typing import Annotated

from fastapi import Cookie, Depends, Header, Security
from fastapi.security import APIKeyCookie

from quiz_app.config import OPERATOR_CSRF_COOKIE_NAME, OPERATOR_SESSION_COOKIE_NAME
from quiz_app.models.enums import AdminRole
from quiz_app.operator.auth_service import (
    OperatorAuthService,
    OperatorPrincipal,
    get_operator_auth_service,
)
from quiz_app.operator.errors import OperatorAuthorizationError, OperatorSessionInvalidError

operator_session_cookie = APIKeyCookie(
    name=OPERATOR_SESSION_COOKIE_NAME,
    scheme_name="OperatorSessionCookie",
    auto_error=False,
)


def require_operator_principal(
    session_token: Annotated[str | None, Security(operator_session_cookie)],
    auth_service: Annotated[OperatorAuthService, Depends(get_operator_auth_service)],
) -> OperatorPrincipal:
    """opaque Cookieから認証済み運営主体を復元する。"""

    if session_token is None:
        raise OperatorSessionInvalidError("operator_session_missing")
    return auth_service.authenticate(session_token)


def require_operator_mutation(
    principal: Annotated[OperatorPrincipal, Depends(require_operator_principal)],
    auth_service: Annotated[OperatorAuthService, Depends(get_operator_auth_service)],
    csrf_cookie: Annotated[
        str | None,
        Cookie(
            alias=OPERATOR_CSRF_COOKIE_NAME,
            description="状態変更では必須。不足または不一致は403。",
        ),
    ] = None,
    csrf_header: Annotated[
        str | None,
        Header(
            alias="X-CSRF-Token",
            description="状態変更ではCSRF Cookieと同じ値が必須。不一致は403。",
        ),
    ] = None,
) -> OperatorPrincipal:
    """状態変更要求にroleとdouble-submit CSRF検証を要求する。"""

    if principal.role not in {AdminRole.OPERATOR, AdminRole.SUPERVISOR}:
        raise OperatorAuthorizationError("operator_role_required")
    auth_service.verify_csrf(principal, csrf_cookie, csrf_header)
    return principal
