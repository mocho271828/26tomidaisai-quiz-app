class AudienceControlError(Exception):
    """参加者adapterが安全なHTTP errorへ変換する基底error。"""


class ParticipantSessionInvalidError(AudienceControlError):
    """参加者sessionが存在しない、期限切れ、または失効済みである。"""


class AudienceServiceUnavailableError(AudienceControlError):
    """参加者adapterの処理に必要な内部依存を利用できない。"""


class EventJoinUnavailableError(AudienceControlError):
    """対象eventが参加受付可能な状態ではない。"""


class ParticipantIdExhaustedError(AudienceControlError):
    """対象eventで割り当て可能な参加者IDをすべて使用した。"""
