from __future__ import annotations

import hashlib
import os
import re
import stat
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)

from quiz_app.load_test.controller_contracts import (
    VerificationExpectation,
    VerificationRequestExpectation,
    canonical_json_bytes,
    manifest_context,
    verification_id,
)
from quiz_app.load_test.manifest import LoadTestManifest
from quiz_app.load_test.scenario import LoadTestRoundScenarioManifest
from quiz_app.load_test.verification_contracts import (
    ArtifactIdentity,
    LoadedVerificationReceipt,
    RoundVerificationReceipt,
    Sha256,
)

MAX_HANDOFF_ARTIFACT_BYTES = 64 * 1024 * 1024
ARTIFACT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
SOURCE_COMMIT_PATTERN = r"^[0-9a-f]{40}$"
OptionLabel = Literal["A", "B", "C", "D"]
ScenarioCondition = Literal[
    "none",
    "receipt-discard",
    "redis-unavailable",
    "redis-recovered",
    "api-secondary-unavailable",
    "api-secondary-recovered",
    "accumulated",
]


class VerificationHandoffError(ValueError):
    """秘密値やartifact pathを含めない固定codeでhandoffを拒否する。"""


class StrictHandoffModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RoundOptionCounts(StrictHandoffModel):
    a: StrictInt = Field(ge=0, le=500)
    b: StrictInt = Field(ge=0, le=500)
    c: StrictInt = Field(ge=0, le=500)
    d: StrictInt = Field(ge=0, le=500)

    def as_tuple(self) -> tuple[int, int, int, int]:
        return self.a, self.b, self.c, self.d


class GeneratorRoundEvidence(StrictHandoffModel):
    """generatorがsealする、個人識別子を含まないRound集計証跡。"""

    format: Literal["load-test-generator-round-evidence-v2"]
    run_id: UUID
    event_id: UUID
    source_commit: StrictStr = Field(pattern=SOURCE_COMMIT_PATTERN)
    schema_revision: StrictStr = Field(min_length=1, max_length=128)
    manifest_sha256: Sha256
    scenario_sha256: Sha256
    round_number: StrictInt = Field(ge=1, le=10)
    round_id: UUID
    question_id: UUID
    scenario_condition: ScenarioCondition
    answer_population: Literal["mixed", "concentrated"]
    virtual_session_count: StrictInt = Field(ge=1, le=600)
    virtual_answer_count: StrictInt = Field(ge=0, le=500)
    virtual_option_counts: RoundOptionCounts
    started_answer_count: StrictInt = Field(ge=0, le=500)
    resolved_answer_count: StrictInt = Field(ge=0, le=500)
    request_start_width_ms: StrictInt = Field(ge=0)
    unresolved_answer_count: StrictInt = Field(ge=0)
    missed_round_count: StrictInt = Field(ge=0)
    duplicate_answer_count: StrictInt = Field(ge=0)
    misleading_success_count: StrictInt = Field(ge=0)
    replay_attempt_count: StrictInt = Field(ge=0, le=500)
    replay_same_answer_id_count: StrictInt = Field(ge=0, le=500)
    replay_row_change_count: StrictInt = Field(ge=0, le=500)
    receipt_discard_count: StrictInt = Field(ge=0, le=500)
    answer_status_query_count: StrictInt = Field(ge=0, le=500)
    status_recovered_count: StrictInt = Field(ge=0, le=500)
    sealed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_internal_totals(self) -> Self:
        if sum(self.virtual_option_counts.as_tuple()) != self.virtual_answer_count:
            raise ValueError("virtual option counts must total virtual answer count")
        return self


class RoundVerificationOracle(StrictHandoffModel):
    """manifest scenarioからcontrollerが再計算する検証期待値。"""

    total_answer_count: Literal[500]
    virtual_session_count: Literal[600]
    virtual_answer_count: Literal[500]
    option_counts: RoundOptionCounts
    round_start_width_max_ms: StrictInt = Field(gt=0)
    unresolved_answer_maximum: Literal[0]
    missed_round_maximum: Literal[0]
    duplicate_answer_maximum: Literal[0]
    misleading_success_maximum: Literal[0]
    replay_attempt_count: StrictInt = Field(ge=0, le=500)
    replay_same_answer_id_count: StrictInt = Field(ge=0, le=500)
    replay_row_change_maximum: Literal[0]
    receipt_discard_count: StrictInt = Field(ge=0, le=500)
    answer_status_query_count: StrictInt = Field(ge=0, le=500)
    status_recovered_count: StrictInt = Field(ge=0, le=500)


class RoundVerificationRequest(StrictHandoffModel):
    """controller journal headとgenerator evidenceをUnit 6へ結ぶrequest。"""

    format: Literal["load-test-round-verification-request-v2"]
    verification_id: UUID
    run_id: UUID
    event_id: UUID
    source_commit: StrictStr = Field(pattern=SOURCE_COMMIT_PATTERN)
    schema_revision: StrictStr = Field(min_length=1, max_length=128)
    manifest_sha256: Sha256
    scenario_sha256: Sha256
    round_number: StrictInt = Field(ge=1, le=10)
    round_id: UUID
    question_id: UUID
    scenario_condition: ScenarioCondition
    verification_requested_head_sha256: Sha256
    generator_evidence: ArtifactIdentity
    oracle: RoundVerificationOracle
    requested_at: AwareDatetime


class LoadedGeneratorRoundEvidence(StrictHandoffModel):
    evidence: GeneratorRoundEvidence
    artifact: ArtifactIdentity
    loaded_at: AwareDatetime


Unit6VerificationReceipt = RoundVerificationReceipt


class VerificationProvider(Protocol):
    """controller coreが依存するread/validate専用境界。"""

    def generator_identity(
        self,
        path: Path,
        manifest: LoadTestManifest,
        *,
        round_number: int,
        round_id: UUID,
    ) -> ArtifactIdentity: ...

    def load_receipt(
        self,
        path: Path,
        expectation: VerificationExpectation,
    ) -> LoadedVerificationReceipt: ...

    def seal_verification_request(
        self,
        directory: Path,
        generator_path: Path,
        manifest: LoadTestManifest,
        expectation: VerificationRequestExpectation,
    ) -> ArtifactIdentity: ...

    def validate_verification_request(
        self,
        directory: Path,
        artifact: ArtifactIdentity,
        manifest: LoadTestManifest,
        expectation: VerificationRequestExpectation,
    ) -> None: ...


def _runtime_repository_root() -> Path:
    module_path = Path(__file__).resolve(strict=True)
    layouts = (
        (Path("backend/pyproject.toml"), Path("compose.yaml")),
        (Path("pyproject.toml"), Path("alembic.ini")),
    )
    # source checkoutは内側のcontainer layoutにも一致するため、外側layoutを優先する。
    for layout in layouts:
        for candidate in module_path.parents:
            if all((candidate / sentinel).is_file() for sentinel in layout):
                return candidate.resolve(strict=True)
    raise VerificationHandoffError("handoff_runtime_root_invalid")


def _require_no_lexical_directory_symlink(directory: Path) -> None:
    """resolve前の各directory componentを調べ、ancestor symlink経由を拒否する。"""

    for ancestor in reversed((directory, *directory.parents)):
        ancestor_status = ancestor.lstat()
        if stat.S_ISLNK(ancestor_status.st_mode) or not stat.S_ISDIR(ancestor_status.st_mode):
            raise VerificationHandoffError("handoff_artifact_directory_invalid")


def _read_external_file(path: Path) -> tuple[bytes, ArtifactIdentity]:
    """repository外の0600 single-link通常fileをdescriptor基準で読む。"""

    descriptor = -1
    directory_descriptor = -1
    try:
        if (
            not path.is_absolute()
            or ".." in path.parts
            or ARTIFACT_NAME_PATTERN.fullmatch(path.name) is None
        ):
            raise VerificationHandoffError("handoff_artifact_path_invalid")
        _require_no_lexical_directory_symlink(path.parent)
        resolved_parent = path.parent.resolve(strict=True)
        if resolved_parent.is_relative_to(_runtime_repository_root()):
            raise VerificationHandoffError("handoff_artifact_must_be_external")

        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory_descriptor = os.open(resolved_parent, directory_flags)
        directory_status = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(directory_status.st_mode)
            or directory_status.st_uid != os.geteuid()
            or stat.S_IMODE(directory_status.st_mode) & 0o022
        ):
            raise VerificationHandoffError("handoff_artifact_directory_invalid")

        path_status = os.stat(path.name, dir_fd=directory_descriptor, follow_symlinks=False)
        descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_descriptor,
        )
        opened_status = os.fstat(descriptor)
        if (
            (path_status.st_dev, path_status.st_ino) != (opened_status.st_dev, opened_status.st_ino)
            or not stat.S_ISREG(opened_status.st_mode)
            or stat.S_IMODE(opened_status.st_mode) != 0o600
            or opened_status.st_uid != os.geteuid()
            or opened_status.st_gid != os.getegid()
            or opened_status.st_nlink != 1
            or not 1 <= opened_status.st_size <= MAX_HANDOFF_ARTIFACT_BYTES
        ):
            raise VerificationHandoffError("handoff_artifact_file_invalid")

        with os.fdopen(descriptor, "rb") as input_file:
            descriptor = -1
            body = input_file.read(MAX_HANDOFF_ARTIFACT_BYTES + 1)
            final_status = os.fstat(input_file.fileno())
        if (
            len(body) != opened_status.st_size
            or len(body) > MAX_HANDOFF_ARTIFACT_BYTES
            or (
                final_status.st_size,
                final_status.st_mtime_ns,
                final_status.st_ctime_ns,
                final_status.st_nlink,
            )
            != (
                opened_status.st_size,
                opened_status.st_mtime_ns,
                opened_status.st_ctime_ns,
                opened_status.st_nlink,
            )
        ):
            raise VerificationHandoffError("handoff_artifact_changed_while_reading")
        return body, ArtifactIdentity(
            name=path.name,
            sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
        )
    except VerificationHandoffError:
        raise
    except (OSError, ValueError) as error:
        raise VerificationHandoffError("handoff_artifact_read_failed") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


def _round_scenario(
    manifest: LoadTestManifest,
    round_number: int,
) -> LoadTestRoundScenarioManifest:
    matches = tuple(round_ for round_ in manifest.scenario.rounds if round_.number == round_number)
    if len(matches) != 1:
        raise VerificationHandoffError("handoff_round_scenario_invalid")
    return matches[0]


def _question_id(manifest: LoadTestManifest, round_number: int) -> UUID:
    matches = tuple(
        question.id for question in manifest.questions if question.display_order == round_number
    )
    if len(matches) != 1:
        raise VerificationHandoffError("handoff_round_question_invalid")
    return matches[0]


def _round_window(
    manifest: LoadTestManifest,
    scenario: LoadTestRoundScenarioManifest,
) -> tuple[datetime, datetime]:
    later_offsets = tuple(
        candidate.start_offset_seconds
        for candidate in manifest.scenario.rounds
        if candidate.start_offset_seconds > scenario.start_offset_seconds
    )
    end_offset = (
        min(later_offsets) if later_offsets else manifest.scenario.lifecycle.measurement_end_seconds
    )
    if end_offset <= scenario.start_offset_seconds:
        raise VerificationHandoffError("handoff_round_window_invalid")
    return (
        manifest.scheduled_start_at + timedelta(seconds=scenario.start_offset_seconds),
        manifest.scheduled_start_at + timedelta(seconds=end_offset),
    )


def _expected_virtual_option_counts(
    manifest: LoadTestManifest,
    scenario: LoadTestRoundScenarioManifest,
) -> RoundOptionCounts:
    oracle = manifest.scenario.answer_oracle
    fixed = {assignment.vu_id: assignment.option for assignment in oracle.fixed_virtual_answers}
    counts: Counter[str] = Counter()
    answering_vus = (
        (*range(1, 499), 599, 600)
        if scenario.answer_population == "mixed"
        else tuple(range(1, 501))
    )
    for vu_id in answering_vus:
        if oracle.modulo_vu_start <= vu_id <= oracle.modulo_vu_end:
            offset = vu_id - oracle.modulo_vu_start
            option = oracle.modulo_option_cycle[offset % len(oracle.modulo_option_cycle)]
        else:
            option = fixed.get(vu_id)
            if option is None:
                raise VerificationHandoffError("handoff_answer_oracle_invalid")
        counts[option] += 1
    return RoundOptionCounts(
        a=counts["A"],
        b=counts["B"],
        c=counts["C"],
        d=counts["D"],
    )


def _expected_status_count(scenario: LoadTestRoundScenarioManifest) -> int:
    if scenario.condition != "receipt-discard":
        return 0
    start = scenario.receipt_discard_vu_start
    end = scenario.receipt_discard_vu_end
    if start is None or end is None or end < start:
        raise VerificationHandoffError("handoff_status_recovery_scenario_invalid")
    return end - start + 1


def _oracle(
    manifest: LoadTestManifest,
    scenario: LoadTestRoundScenarioManifest,
) -> RoundVerificationOracle:
    replay_count = scenario.virtual_answer_count if scenario.replay == "all" else 0
    status_count = _expected_status_count(scenario)
    thresholds = manifest.scenario.thresholds
    return RoundVerificationOracle(
        total_answer_count=thresholds.expected_answer_count,
        virtual_session_count=manifest.scenario.lifecycle.virtual_sessions,
        virtual_answer_count=scenario.virtual_answer_count,
        option_counts=RoundOptionCounts(
            a=scenario.expected_option_counts.a,
            b=scenario.expected_option_counts.b,
            c=scenario.expected_option_counts.c,
            d=scenario.expected_option_counts.d,
        ),
        round_start_width_max_ms=thresholds.round_start_width_max_ms,
        unresolved_answer_maximum=thresholds.unresolved_answers_maximum,
        missed_round_maximum=thresholds.missed_rounds_maximum,
        duplicate_answer_maximum=thresholds.duplicate_answers_maximum,
        misleading_success_maximum=thresholds.misleading_successes_maximum,
        replay_attempt_count=replay_count,
        replay_same_answer_id_count=replay_count,
        replay_row_change_maximum=thresholds.replay_row_change_maximum,
        receipt_discard_count=status_count,
        answer_status_query_count=status_count,
        status_recovered_count=status_count,
    )


def _binding_tuple(value: GeneratorRoundEvidence) -> tuple[object, ...]:
    return (
        value.run_id,
        value.event_id,
        value.source_commit,
        value.schema_revision,
        value.manifest_sha256,
        value.scenario_sha256,
        value.round_number,
        value.round_id,
        value.question_id,
        value.scenario_condition,
    )


def _expected_binding(
    manifest: LoadTestManifest,
    *,
    round_number: int,
    round_id: UUID,
) -> tuple[object, ...]:
    context = manifest_context(manifest)
    scenario = _round_scenario(manifest, round_number)
    return (
        context.run_id,
        context.event_id,
        context.source_commit,
        context.schema_revision,
        context.manifest_sha256,
        context.scenario_sha256,
        round_number,
        round_id,
        _question_id(manifest, round_number),
        scenario.condition,
    )


def _validate_generator_evidence(
    evidence: GeneratorRoundEvidence,
    manifest: LoadTestManifest,
    *,
    round_number: int,
    round_id: UUID,
) -> None:
    scenario = _round_scenario(manifest, round_number)
    oracle = _oracle(manifest, scenario)
    expected_virtual_counts = _expected_virtual_option_counts(manifest, scenario)
    window_start, window_end = _round_window(manifest, scenario)
    actual = (
        _binding_tuple(evidence),
        evidence.answer_population,
        evidence.virtual_session_count,
        evidence.virtual_answer_count,
        evidence.virtual_option_counts,
        evidence.started_answer_count,
        evidence.resolved_answer_count,
        evidence.unresolved_answer_count,
        evidence.missed_round_count,
        evidence.duplicate_answer_count,
        evidence.misleading_success_count,
        evidence.replay_attempt_count,
        evidence.replay_same_answer_id_count,
        evidence.replay_row_change_count,
        evidence.receipt_discard_count,
        evidence.answer_status_query_count,
        evidence.status_recovered_count,
    )
    expected = (
        _expected_binding(manifest, round_number=round_number, round_id=round_id),
        scenario.answer_population,
        oracle.virtual_session_count,
        oracle.virtual_answer_count,
        expected_virtual_counts,
        oracle.virtual_answer_count,
        oracle.virtual_answer_count,
        oracle.unresolved_answer_maximum,
        oracle.missed_round_maximum,
        oracle.duplicate_answer_maximum,
        oracle.misleading_success_maximum,
        oracle.replay_attempt_count,
        oracle.replay_same_answer_id_count,
        oracle.replay_row_change_maximum,
        oracle.receipt_discard_count,
        oracle.answer_status_query_count,
        oracle.status_recovered_count,
    )
    if (
        actual != expected
        or evidence.request_start_width_ms > oracle.round_start_width_max_ms
        or not window_start <= evidence.sealed_at < window_end
    ):
        raise VerificationHandoffError("generator_evidence_mismatch")


def load_generator_evidence(
    path: Path,
    manifest: LoadTestManifest,
    *,
    round_number: int,
    round_id: UUID,
) -> LoadedGeneratorRoundEvidence:
    body, artifact = _read_external_file(path)
    evidence: GeneratorRoundEvidence | None = None
    try:
        evidence = GeneratorRoundEvidence.model_validate_json(body)
    except ValidationError:
        pass
    if evidence is None:
        raise VerificationHandoffError("generator_evidence_invalid")
    if body != canonical_json_bytes(evidence) + b"\n":
        raise VerificationHandoffError("generator_evidence_invalid")
    _validate_generator_evidence(
        evidence,
        manifest,
        round_number=round_number,
        round_id=round_id,
    )
    return LoadedGeneratorRoundEvidence(
        evidence=evidence,
        artifact=artifact,
        loaded_at=datetime.now(UTC),
    )


def build_verification_request(
    manifest: LoadTestManifest,
    *,
    round_number: int,
    round_id: UUID,
    verification_requested_head_sha256: str,
    generator: LoadedGeneratorRoundEvidence,
    requested_at: datetime,
) -> RoundVerificationRequest:
    """generator artifactを再検証し、manifest由来oracleを付けたrequestを構築する。"""

    _validate_generator_evidence(
        generator.evidence,
        manifest,
        round_number=round_number,
        round_id=round_id,
    )
    scenario = _round_scenario(manifest, round_number)
    _, window_end = _round_window(manifest, scenario)
    if requested_at < generator.evidence.sealed_at or requested_at >= window_end:
        raise VerificationHandoffError("verification_request_time_invalid")
    expected_virtual = _expected_virtual_option_counts(manifest, scenario)
    oracle = _oracle(manifest, scenario)
    if (
        sum(expected_virtual.as_tuple()) != oracle.total_answer_count
        or expected_virtual != oracle.option_counts
    ):
        raise VerificationHandoffError("generator_answer_oracle_mismatch")

    context = manifest_context(manifest)
    try:
        return RoundVerificationRequest(
            format="load-test-round-verification-request-v2",
            verification_id=verification_id(
                manifest.run_id,
                round_number,
                verification_requested_head_sha256,
            ),
            run_id=context.run_id,
            event_id=context.event_id,
            source_commit=context.source_commit,
            schema_revision=context.schema_revision,
            manifest_sha256=context.manifest_sha256,
            scenario_sha256=context.scenario_sha256,
            round_number=round_number,
            round_id=round_id,
            question_id=_question_id(manifest, round_number),
            scenario_condition=scenario.condition,
            verification_requested_head_sha256=verification_requested_head_sha256,
            generator_evidence=generator.artifact,
            oracle=oracle,
            requested_at=requested_at,
        )
    except ValidationError as error:
        raise VerificationHandoffError("verification_request_invalid") from error


def verification_expectation_from_request(
    request: RoundVerificationRequest,
) -> VerificationExpectation:
    return VerificationExpectation(
        run_id=request.run_id,
        event_id=request.event_id,
        source_commit=request.source_commit,
        schema_revision=request.schema_revision,
        manifest_sha256=request.manifest_sha256,
        scenario_sha256=request.scenario_sha256,
        round_number=request.round_number,
        round_id=request.round_id,
        question_id=request.question_id,
        verification_requested_head_sha256=request.verification_requested_head_sha256,
        generator_evidence=request.generator_evidence,
    )


def load_verification_receipt(
    path: Path,
    expectation: VerificationExpectation,
) -> LoadedVerificationReceipt:
    """Unit 6のPASS receiptと同directoryのreportをdigestまで検証する。"""

    body, artifact = _read_external_file(path)
    try:
        receipt = RoundVerificationReceipt.model_validate_json(body)
    except ValidationError as error:
        raise VerificationHandoffError("verification_receipt_invalid") from error
    expected = (
        verification_id(
            expectation.run_id,
            expectation.round_number,
            expectation.verification_requested_head_sha256,
        ),
        expectation.run_id,
        expectation.event_id,
        expectation.source_commit,
        expectation.schema_revision,
        expectation.manifest_sha256,
        expectation.scenario_sha256,
        expectation.round_number,
        expectation.round_id,
        expectation.question_id,
        expectation.verification_requested_head_sha256,
        expectation.generator_evidence,
    )
    actual = (
        receipt.verification_id,
        receipt.run_id,
        receipt.event_id,
        receipt.source_commit,
        receipt.schema_revision,
        receipt.manifest_sha256,
        receipt.scenario_sha256,
        receipt.round_number,
        receipt.round_id,
        receipt.question_id,
        receipt.verification_requested_head_sha256,
        receipt.generator_evidence,
    )
    if actual != expected or receipt.report.name == artifact.name:
        raise VerificationHandoffError("verification_receipt_mismatch")
    _, report_artifact = _read_external_file(path.with_name(receipt.report.name))
    if report_artifact != receipt.report:
        raise VerificationHandoffError("verification_report_mismatch")
    return LoadedVerificationReceipt(
        receipt=receipt,
        artifact=artifact,
        loaded_at=datetime.now(UTC),
    )


def generator_identity(
    path: Path,
    manifest: LoadTestManifest,
    *,
    round_number: int,
    round_id: UUID,
) -> ArtifactIdentity:
    return load_generator_evidence(
        path,
        manifest,
        round_number=round_number,
        round_id=round_id,
    ).artifact


def write_verification_request(
    directory: Path,
    request: RoundVerificationRequest,
) -> ArtifactIdentity:
    """requestをrepository外へ0600・非上書きで永続化し、fileとdirectoryをfsyncする。"""

    directory_descriptor = -1
    descriptor = -1
    created = False
    name = f"verification-request-round-{request.round_number:02d}-{request.verification_id}.json"
    body = verification_request_bytes(request)
    try:
        if not directory.is_absolute() or ".." in directory.parts:
            raise VerificationHandoffError("verification_request_directory_invalid")
        _require_no_lexical_directory_symlink(directory)
        resolved_directory = directory.resolve(strict=True)
        if resolved_directory.is_relative_to(_runtime_repository_root()):
            raise VerificationHandoffError("verification_request_directory_must_be_external")
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory_descriptor = os.open(resolved_directory, directory_flags)
        directory_status = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(directory_status.st_mode)
            or stat.S_IMODE(directory_status.st_mode) != 0o700
            or directory_status.st_uid != os.geteuid()
            or directory_status.st_gid != os.getegid()
        ):
            raise VerificationHandoffError("verification_request_directory_invalid")
        if not 1 <= len(body) <= MAX_HANDOFF_ARTIFACT_BYTES:
            raise VerificationHandoffError("verification_request_size_invalid")

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(name, flags, 0o600, dir_fd=directory_descriptor)
        created = True
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            os.fchmod(output.fileno(), 0o600)
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
            written_status = os.fstat(output.fileno())
            if (
                not stat.S_ISREG(written_status.st_mode)
                or stat.S_IMODE(written_status.st_mode) != 0o600
                or written_status.st_uid != os.geteuid()
                or written_status.st_gid != os.getegid()
                or written_status.st_nlink != 1
                or written_status.st_size != len(body)
            ):
                raise VerificationHandoffError("verification_request_file_invalid")
        os.fsync(directory_descriptor)
    except VerificationHandoffError:
        if created and directory_descriptor >= 0:
            try:
                os.unlink(name, dir_fd=directory_descriptor)
                os.fsync(directory_descriptor)
            except OSError:
                pass
        raise
    except FileExistsError:
        existing_body, identity = _read_external_file(directory / name)
        if existing_body != body:
            raise VerificationHandoffError("verification_request_collision") from None
        return identity
    except OSError as error:
        if created and directory_descriptor >= 0:
            try:
                os.unlink(name, dir_fd=directory_descriptor)
                os.fsync(directory_descriptor)
            except OSError:
                pass
        raise VerificationHandoffError("verification_request_write_failed") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_descriptor >= 0:
            os.close(directory_descriptor)
    return ArtifactIdentity(
        name=name,
        sha256=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
    )


def verification_request_bytes(request: RoundVerificationRequest) -> bytes:
    """controller request の既存human-readable seal形式を一意に返す。"""

    return request.model_dump_json(indent=2).encode("utf-8") + b"\n"


def _base_verification_expectation(
    expectation: VerificationRequestExpectation,
) -> VerificationExpectation:
    return VerificationExpectation.model_validate(expectation.model_dump(exclude={"requested_at"}))


def validate_verification_request(
    directory: Path,
    artifact: ArtifactIdentity,
    manifest: LoadTestManifest,
    expectation: VerificationRequestExpectation,
) -> None:
    """sealed requestをdigestとmanifest由来oracleまで再検証する。"""

    expected_verification_id = verification_id(
        expectation.run_id,
        expectation.round_number,
        expectation.verification_requested_head_sha256,
    )
    expected_name = (
        f"verification-request-round-{expectation.round_number:02d}-{expected_verification_id}.json"
    )
    if artifact.name != expected_name:
        raise VerificationHandoffError("verification_request_mismatch")
    body, actual_artifact = _read_external_file(directory / artifact.name)
    if actual_artifact != artifact:
        raise VerificationHandoffError("verification_request_mismatch")
    try:
        request = RoundVerificationRequest.model_validate_json(body)
    except ValidationError as error:
        raise VerificationHandoffError("verification_request_invalid") from error
    scenario = _round_scenario(manifest, expectation.round_number)
    if (
        body != verification_request_bytes(request)
        or verification_expectation_from_request(request)
        != _base_verification_expectation(expectation)
        or request.verification_id != expected_verification_id
        or request.scenario_condition != scenario.condition
        or request.oracle != _oracle(manifest, scenario)
        or request.requested_at != expectation.requested_at
    ):
        raise VerificationHandoffError("verification_request_mismatch")


def seal_verification_request(
    directory: Path,
    generator_path: Path,
    manifest: LoadTestManifest,
    expectation: VerificationRequestExpectation,
) -> ArtifactIdentity:
    """generator evidenceを再読し、journal head固定requestをcrash-safeにsealする。"""

    generator = load_generator_evidence(
        generator_path,
        manifest,
        round_number=expectation.round_number,
        round_id=expectation.round_id,
    )
    if generator.artifact != expectation.generator_evidence:
        raise VerificationHandoffError("verification_request_evidence_changed")
    request = build_verification_request(
        manifest,
        round_number=expectation.round_number,
        round_id=expectation.round_id,
        verification_requested_head_sha256=(expectation.verification_requested_head_sha256),
        generator=generator,
        requested_at=expectation.requested_at,
    )
    if verification_expectation_from_request(request) != _base_verification_expectation(
        expectation
    ):
        raise VerificationHandoffError("verification_request_mismatch")
    artifact = write_verification_request(directory, request)
    validate_verification_request(directory, artifact, manifest, expectation)
    return artifact


class FileVerificationProvider:
    """fixture生成能力を持たないfilesystem provider。"""

    def generator_identity(
        self,
        path: Path,
        manifest: LoadTestManifest,
        *,
        round_number: int,
        round_id: UUID,
    ) -> ArtifactIdentity:
        return generator_identity(
            path,
            manifest,
            round_number=round_number,
            round_id=round_id,
        )

    def load_receipt(
        self,
        path: Path,
        expectation: VerificationExpectation,
    ) -> LoadedVerificationReceipt:
        return load_verification_receipt(path, expectation)

    def seal_verification_request(
        self,
        directory: Path,
        generator_path: Path,
        manifest: LoadTestManifest,
        expectation: VerificationRequestExpectation,
    ) -> ArtifactIdentity:
        return seal_verification_request(
            directory,
            generator_path,
            manifest,
            expectation,
        )

    def validate_verification_request(
        self,
        directory: Path,
        artifact: ArtifactIdentity,
        manifest: LoadTestManifest,
        expectation: VerificationRequestExpectation,
    ) -> None:
        validate_verification_request(directory, artifact, manifest, expectation)
