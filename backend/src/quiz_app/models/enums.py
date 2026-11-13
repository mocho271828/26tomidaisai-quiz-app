from enum import StrEnum


class EventState(StrEnum):
    DRAFT = "Draft"
    CHECKED = "Checked"
    LOBBY_OPEN = "LobbyOpen"
    IN_PROGRESS = "InProgress"
    SUSPENDED = "Suspended"
    DATA_STORE_UNAVAILABLE = "DataStoreUnavailable"
    SERVICE_LOST = "ServiceLost"
    COMPLETED = "Completed"
    ABORTED = "Aborted"
    ARCHIVED = "Archived"


class RankingVisibility(StrEnum):
    HIDDEN_ALL = "HiddenAll"
    PARTICIPANT_ONLY = "ParticipantOnly"
    PUBLIC = "Public"


class RoundState(StrEnum):
    PREPARING_ASSETS = "PreparingAssets"
    READY_TO_SCHEDULE = "ReadyToSchedule"
    SCHEDULED = "Scheduled"
    OPEN = "Open"
    AWAITING_VALIDATION = "AwaitingValidation"
    READY_TO_REVEAL = "ReadyToReveal"
    VOID_CANDIDATE = "VoidCandidate"
    REVEAL = "Reveal"
    VOIDED = "Voided"
    ROUND_BACKUP_PENDING = "RoundBackupPending"
    ROUND_BACKUP_VALIDATED = "RoundBackupValidated"
    ROUND_BACKUP_FAILED = "RoundBackupFailed"
    ROUND_BACKUP_RETRYING = "RoundBackupRetrying"


class RoundResultPublication(StrEnum):
    WITHHELD = "Withheld"
    PUBLISHED = "Published"


class PublicLabelKind(StrEnum):
    RANDOM_ID = "RandomId"
    DISPLAY_NAME = "DisplayName"


class AdminRole(StrEnum):
    OPERATOR = "Operator"
    SUPERVISOR = "Supervisor"


class ScoreExclusionReason(StrEnum):
    NONE = "None"
    WRONG = "Wrong"
    VOID = "Void"
    INELIGIBLE = "Ineligible"


class SpeedCohort(StrEnum):
    CORRECT = "Correct"
    ALL_VALID = "AllValid"
    EXCLUDED = "Excluded"
