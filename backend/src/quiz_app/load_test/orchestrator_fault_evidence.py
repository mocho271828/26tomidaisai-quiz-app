from __future__ import annotations

from hashlib import sha256

from quiz_app.load_test.controller_contracts import canonical_json_bytes, manifest_context
from quiz_app.load_test.fault_controller_contracts import (
    FaultControllerJournalRecord,
    FaultControllerStatus,
    FaultInitializedPayload,
)
from quiz_app.load_test.fault_controller_journal import (
    MAX_FAULT_JOURNAL_BYTES,
    MAX_FAULT_JOURNAL_RECORDS,
    fault_controller_identity_sha256,
)
from quiz_app.load_test.fault_controller_state import (
    build_fault_controller_status,
    build_fault_slots,
    reduce_fault_records,
)
from quiz_app.load_test.manifest import LoadTestManifest
from quiz_app.load_test.orchestrator_contracts import OrchestratorError


def validate_fault_evidence(
    manifest: LoadTestManifest, body: bytes, status: FaultControllerStatus
) -> None:
    """転送されたjournalを同じ状態還元器へ渡し、stdoutだけの成功申告を拒否する。"""
    lines = body.splitlines()
    if (
        not body.endswith(b"\n")
        or len(body) > MAX_FAULT_JOURNAL_BYTES
        or not 1 <= len(lines) <= MAX_FAULT_JOURNAL_RECORDS
    ):
        raise OrchestratorError("orchestrator_fault_journal_invalid")
    records = tuple(FaultControllerJournalRecord.model_validate_json(line) for line in lines)
    first = records[0].payload
    if not isinstance(first, FaultInitializedPayload) or first.identity.context != manifest_context(
        manifest
    ):
        raise OrchestratorError("orchestrator_fault_journal_binding_invalid")
    identity_hash = fault_controller_identity_sha256(first.identity)
    previous = None
    for sequence, (line, record) in enumerate(zip(lines, records, strict=True), start=1):
        unsigned = record.model_dump(mode="json", exclude={"entry_sha256"})
        if (
            canonical_json_bytes(record) != line
            or record.sequence != sequence
            or record.previous_sha256 != previous
            or record.run_id != manifest.run_id
            or record.event_id != manifest.event.id
            or record.identity_sha256 != identity_hash
            or record.entry_sha256 != sha256(canonical_json_bytes(unsigned)).hexdigest()
        ):
            raise OrchestratorError("orchestrator_fault_journal_chain_invalid")
        previous = record.entry_sha256
    progress = reduce_fault_records(
        records, first.identity, build_fault_slots(manifest, first.identity)
    )
    observed = build_fault_controller_status(progress, previous)
    if observed != status or observed.status != "completed":
        raise OrchestratorError("orchestrator_fault_status_invalid")
