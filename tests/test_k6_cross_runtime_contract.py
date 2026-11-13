from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

import pytest
from pydantic import (
    UUID4,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    JsonValue,
    StrictInt,
    StrictStr,
    StringConstraints,
)

from quiz_app.load_test import manifest as manifest_module
from quiz_app.load_test.controller_contracts import (
    canonical_json_bytes,
    manifest_context,
)
from quiz_app.load_test.controller_verification import (
    GeneratorRoundEvidence,
    load_generator_evidence,
)
from quiz_app.load_test.manifest import LoadTestManifest, build_load_test_manifest

FIXTURE_DIRECTORY = Path(__file__).parents[1] / "load-test" / "fixtures"
Sha256 = Annotated[StrictStr, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


@pytest.fixture(autouse=True)
def _use_golden_schema_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    # 保存済みformal v3のcanonical bytesは、新しいアプリschemaから独立して検証する。
    monkeypatch.setattr(manifest_module, "SCHEMA_REVISION", "20260804_0002")


class StrictFixtureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ContractInputs(StrictFixtureModel):
    run_id: UUID4
    event_id: UUID4
    event_slug: StrictStr
    scheduled_start_at: AwareDatetime
    public_origin: StrictStr
    source_commit: StrictStr
    api_image: StrictStr
    gateway_image: StrictStr
    generator_image: StrictStr
    scoring_policy_version: StrictStr
    scoring_base_points: StrictInt
    scoring_initial_speed_bonus: StrictInt
    scoring_speed_bucket_ms: StrictInt
    scoring_speed_decrement_per_bucket: StrictInt
    scoring_standard_duration_ms: StrictInt
    scoring_is_approved: Literal[True]
    display_name_policy_version: StrictStr
    display_notice_version: StrictStr
    display_max_code_points: StrictInt
    display_reject_patterns_json: StrictStr
    created_at: AwareDatetime


class Uuid5Expected(StrictFixtureModel):
    question1: UUID
    question1_option_a: UUID
    round1: UUID


class ContractExpected(StrictFixtureModel):
    manifest_sha256: Sha256
    scenario_sha256: Sha256
    feff_title_manifest_sha256: Sha256
    information_separator_title_manifest_sha256: Sha256
    uuid5: Uuid5Expected


class ContractGolden(StrictFixtureModel):
    inputs: ContractInputs
    expected: ContractExpected


class CanonicalGolden(StrictFixtureModel):
    value: JsonValue
    canonical_ascii: StrictStr
    sha256: Sha256


def _contract_golden() -> ContractGolden:
    return ContractGolden.model_validate_json(
        (FIXTURE_DIRECTORY / "python-contract-golden-v3.json").read_bytes()
    )


def _validated_title_variant(manifest: LoadTestManifest, title: str) -> LoadTestManifest:
    event = manifest.event.model_copy(update={"title": title})
    candidate = manifest.model_copy(update={"event": event})
    return LoadTestManifest.model_validate_json(candidate.model_dump_json())


def test_python_manifest_factory_matches_shared_node_contract_golden() -> None:
    golden = _contract_golden()
    manifest = build_load_test_manifest(**golden.inputs.model_dump())
    context = manifest_context(manifest)

    assert context.manifest_sha256 == golden.expected.manifest_sha256
    assert context.scenario_sha256 == golden.expected.scenario_sha256
    assert manifest.questions[0].id == golden.expected.uuid5.question1
    assert manifest.questions[0].options[0].id == golden.expected.uuid5.question1_option_a
    assert manifest.initial_round.id == golden.expected.uuid5.round1

    feff_title = f"{manifest.event.title}\ufeff"
    feff_manifest = _validated_title_variant(manifest, feff_title)
    assert feff_manifest.event.title == feff_title
    assert manifest_context(feff_manifest).manifest_sha256 == (
        golden.expected.feff_title_manifest_sha256
    )

    separator_title = f"{manifest.event.title}\u001c"
    separator_manifest = _validated_title_variant(manifest, separator_title)
    assert separator_manifest.event.title == separator_title
    assert manifest_context(separator_manifest).manifest_sha256 == (
        golden.expected.information_separator_title_manifest_sha256
    )


def test_python_canonical_serializer_matches_shared_node_bytes() -> None:
    golden = CanonicalGolden.model_validate_json(
        (FIXTURE_DIRECTORY / "canonical-python-golden.json").read_bytes()
    )
    assert isinstance(golden.value, dict)

    serialized = canonical_json_bytes(golden.value)

    assert serialized.decode("ascii") == golden.canonical_ascii
    assert sha256(serialized).hexdigest() == golden.sha256


def test_python_accepts_shared_node_generator_round_evidence(tmp_path: Path) -> None:
    body = (FIXTURE_DIRECTORY / "generator-round-evidence-v2.json").read_bytes()
    evidence = GeneratorRoundEvidence.model_validate_json(body)
    golden = _contract_golden()
    manifest = build_load_test_manifest(**golden.inputs.model_dump())
    evidence_directory = tmp_path / "evidence"
    evidence_directory.mkdir(mode=0o700)
    evidence_directory.chmod(0o700)
    evidence_path = evidence_directory / "generator-round-01.json"
    evidence_path.write_bytes(body)
    evidence_path.chmod(0o600)
    loaded = load_generator_evidence(
        evidence_path,
        manifest,
        round_number=1,
        round_id=manifest.initial_round.id,
    )

    assert evidence.virtual_session_count == 600
    assert evidence.virtual_answer_count == 500
    assert evidence.virtual_option_counts.as_tuple() == (126, 125, 125, 124)
    assert loaded.evidence == evidence
    assert loaded.artifact.sha256 == sha256(body).hexdigest()
    assert body == canonical_json_bytes(evidence) + b"\n"
