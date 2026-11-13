from uuid import UUID


class DomainError(Exception):
    """API adapterが安全なHTTP errorへ変換できるdomain error。"""


class ResourceNotFoundError(DomainError):
    """要求対象が存在しない。"""


class VersionConflictError(DomainError):
    """expected versionが現在の状態と一致しない。"""


class InvalidStateTransitionError(DomainError):
    """現在状態では要求した遷移を実行できない。"""


class AnswerWindowClosedError(DomainError):
    """DB受付時刻がparticipantの回答可能区間外である。"""


class ClientSelectionTimeError(AnswerWindowClosedError):
    """端末申告時間の型または選択期限が契約外である。"""


class ClientReceiptDifferenceError(AnswerWindowClosedError):
    """端末時間とDB受付時間の差が許容範囲外である。"""


class AnswerContentConflictError(DomainError):
    """既存回答と再送内容が一致しない。"""


class PublicationTooEarlyError(DomainError):
    """回答受付終端より前に結果処理を要求した。"""


class DomainInvariantError(DomainError):
    """DB内容がdomainの不変条件を満たしていない。"""


class RoundBackupFailedError(DomainError):
    """暗号化backupの作成または検証に失敗した。"""

    def __init__(self, message: str, *, event_id: UUID) -> None:
        super().__init__(message)
        self.event_id = event_id
