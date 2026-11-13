from __future__ import annotations

import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, StrictStr

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
Sha256 = Annotated[StrictStr, Field(pattern=SHA256_PATTERN.pattern)]
ArtifactName = Annotated[
    StrictStr,
    Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$"),
]


class StrictVerificationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ArtifactIdentity(StrictVerificationModel):
    name: ArtifactName
    sha256: Sha256
    size_bytes: StrictInt = Field(ge=1, le=1_000_000_000)


class RoundVerificationReceipt(StrictVerificationModel):
    """Unit 6 がsealし、controllerが進行前に検証する最小receipt契約。"""

    format: Literal["load-test-round-verification-receipt-v2"]
    verification_id: UUID
    run_id: UUID
    event_id: UUID
    source_commit: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{40}$")]
    schema_revision: StrictStr = Field(min_length=1, max_length=128)
    manifest_sha256: Sha256
    scenario_sha256: Sha256
    round_number: StrictInt = Field(ge=1, le=10)
    round_id: UUID
    question_id: UUID
    verification_requested_head_sha256: Sha256
    generator_evidence: ArtifactIdentity
    report: ArtifactIdentity
    decision: Literal["PASS"]
    failure_codes: tuple[()]
    sealed_at: AwareDatetime


class LoadedVerificationReceipt(StrictVerificationModel):
    receipt: RoundVerificationReceipt
    artifact: ArtifactIdentity
    loaded_at: AwareDatetime
