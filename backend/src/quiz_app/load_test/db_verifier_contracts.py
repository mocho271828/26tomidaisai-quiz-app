from __future__ import annotations

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    model_validator,
)

from quiz_app.load_test.controller_verification import RoundVerificationRequest
from quiz_app.load_test.verification_contracts import Sha256

DbVerifierMode = Literal["per-round", "final", "observe-locks"]
PostgresIdentifier = Annotated[StrictStr, Field(min_length=1, max_length=63)]


# 旧固定scenarioのlock観測期間。回答受付期限からは独立して保持する。
LOCK_OBSERVATION_DURATION_MS = 15_000


class StrictDbVerifierModel(BaseModel):
    """DB verifier境界の入力を型変換なし・未知fieldなしで検証する。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class PerRoundVerificationInput(StrictDbVerifierModel):
    """publish後の単一Roundを同じcontroller requestへ束縛する。"""

    format: Literal["load-test-db-verifier-input-v1"]
    mode: Literal["per-round"]
    request: RoundVerificationRequest


class FinalVerificationInput(StrictDbVerifierModel):
    """Round 10 requestを起点にEvent全体を最終照合する。"""

    format: Literal["load-test-db-verifier-input-v1"]
    mode: Literal["final"]
    request: RoundVerificationRequest

    @model_validator(mode="after")
    def require_final_round_for_final_mode(self) -> Self:
        if self.request.round_number != 10:
            raise ValueError("final verification requires round 10 request")
        return self


class LockObservationInput(StrictDbVerifierModel):
    """回答burst中にcontroller receiptを待たずlock観測を開始する契約。"""

    format: Literal["load-test-db-lock-observation-input-v1"]
    mode: Literal["observe-locks"]
    run_id: UUID
    event_id: UUID
    source_commit: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{40}$")]
    schema_revision: Annotated[StrictStr, Field(min_length=1, max_length=64)]
    manifest_sha256: Sha256
    scenario_sha256: Sha256
    round_number: StrictInt = Field(ge=1, le=10)
    observation_start_at: AwareDatetime
    observation_end_at: AwareDatetime

    @model_validator(mode="after")
    def require_positive_observation_window(self) -> Self:
        if self.observation_end_at <= self.observation_start_at:
            raise ValueError("lock observation end must be after start")
        return self


DbVerifierRequest = Annotated[
    PerRoundVerificationInput | FinalVerificationInput | LockObservationInput,
    Field(discriminator="mode"),
]
DB_VERIFIER_REQUEST_ADAPTER = TypeAdapter(DbVerifierRequest)


class DatabaseSessionRow(StrictDbVerifierModel):
    """接続直後のtransaction特性をDB自己申告値から検証する行。"""

    current_role: PostgresIdentifier
    session_role: PostgresIdentifier
    database_name: PostgresIdentifier
    is_read_only: Literal[True]
    isolation_level: Literal["repeatable read"]
    statement_timeout: Literal["10s"]
    lock_timeout: Literal["1s"]
    idle_transaction_timeout: Literal["15s"]
    is_search_path_pinned: Literal[True]


class DatabaseRoleRow(StrictDbVerifierModel):
    """current roleに危険なPostgreSQL属性がないことを検証する行。"""

    role_name: PostgresIdentifier
    is_superuser: Literal[False]
    can_create_database: Literal[False]
    can_create_role: Literal[False]
    can_login: Literal[True]
    is_replication_role: Literal[False]
    bypasses_row_level_security: Literal[False]
    inherits_privileges: Literal[True]
    role_setting_count: Literal[0]


class DatabaseRoleMembershipRow(StrictDbVerifierModel):
    """明示的・再帰的に継承する追加roleを表す。"""

    role_name: PostgresIdentifier


class DatabasePrivilegeSummaryRow(StrictDbVerifierModel):
    """SELECT以外の有効権限とobject所有を集約した拒否用行。"""

    owned_database_count: Literal[0]
    owned_schema_count: Literal[0]
    owned_relation_count: Literal[0]
    owned_routine_count: Literal[0]
    has_database_create: Literal[False]
    has_database_temporary: Literal[False]
    creatable_schema_count: Literal[0]
    writable_relation_count: Literal[0]
    privileged_sequence_count: Literal[0]
    usable_foreign_data_wrapper_count: Literal[0]
    usable_foreign_server_count: Literal[0]
    executable_security_definer_count: Literal[0]


class DatabaseColumnPrivilegeRow(StrictDbVerifierModel):
    """application relationの有効SELECT列権限を列単位で列挙する。"""

    schema_name: Literal["public"]
    table_name: PostgresIdentifier
    column_name: PostgresIdentifier
    has_select: StrictBool


class DatabaseSchemaRevisionRow(StrictDbVerifierModel):
    """同一snapshotで読んだAlembic revision。"""

    version_num: Annotated[StrictStr, Field(min_length=1, max_length=64)]


class QueryExecutionMetadata(StrictDbVerifierModel):
    """秘密値や業務行を含めずquery実行境界だけを記録する。"""

    query_id: Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    row_count: StrictInt = Field(ge=0)
