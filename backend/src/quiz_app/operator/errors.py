class OperatorControlError(Exception):
    """運営adapterが安全なHTTP errorへ変換する基底error。"""


class InvalidOperatorCredentialsError(OperatorControlError):
    """usernameまたはpasswordが一致しない。"""


class OperatorSessionInvalidError(OperatorControlError):
    """sessionが存在しない、期限切れ、または失効済みである。"""


class CsrfValidationError(OperatorControlError):
    """CSRF cookie/header/session hashが一致しない。"""


class DemoAuthDisabledError(OperatorControlError):
    """local demo認証が現在環境で無効である。"""


class OperatorAuthorizationError(OperatorControlError):
    """認証済みaccountのroleでは要求操作を実行できない。"""


class IdempotencyKeyConflictError(OperatorControlError):
    """同じkeyが異なる操作内容へ再利用された。"""
