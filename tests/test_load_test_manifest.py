import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid5

import pytest
from pydantic import ValidationError

from quiz_app.load_test import manifest as manifest_module
from quiz_app.load_test.manifest import (
    LoadTestManifest,
    build_load_test_manifest,
    load_manifest,
    write_manifest,
)
from quiz_app.schema_revision import SCHEMA_REVISION

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
EVENT_ID = UUID("22222222-2222-4222-8222-222222222222")
IMAGE = f"registry.aws.internal/quiz/api@sha256:{'1' * 64}"
CREATED_AT = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _manifest(**overrides: object) -> LoadTestManifest:
    values: dict[str, object] = {
        "run_id": RUN_ID,
        "event_id": EVENT_ID,
        "event_slug": "load-test-2026-09-03",
        "scheduled_start_at": CREATED_AT + timedelta(hours=1),
        "public_origin": "https://quiz.aws.internal",
        "source_commit": "a" * 40,
        "api_image": IMAGE,
        "gateway_image": IMAGE,
        "generator_image": IMAGE,
        "scoring_policy_version": "scoring-v1",
        "scoring_base_points": 10_000,
        "scoring_initial_speed_bonus": 500,
        "scoring_speed_bucket_ms": 100,
        "scoring_speed_decrement_per_bucket": 10,
        "scoring_standard_duration_ms": 10_000,
        "scoring_is_approved": True,
        "display_name_policy_version": "display-v1",
        "display_notice_version": "notice-v1",
        "display_max_code_points": 20,
        "display_reject_patterns_json": "[]",
        "created_at": CREATED_AT,
    }
    values.update(overrides)
    return build_load_test_manifest(**values)  # type: ignore[arg-type]


def _mutated_json(manifest: LoadTestManifest, mutate: object) -> str:
    payload = json.loads(manifest.model_dump_json())
    assert isinstance(payload, dict)
    mutate(payload)  # type: ignore[operator]
    return json.dumps(payload)


def test_manifest_generates_deterministic_ten_question_contract() -> None:
    first = _manifest()
    second = _manifest()

    assert first == second
    assert first.format == "load-test-manifest-v3"
    assert first.schema_revision == SCHEMA_REVISION
    assert first.event.state == "LobbyOpen"
    assert first.event.scoring_policy.base_points == 10_000
    assert first.event.display_name_policy.notice_version == "notice-v1"
    assert first.initial_round.state == "PreparingAssets"
    assert first.initial_round.question_id == first.questions[0].id
    assert [question.version for question in first.questions] == list(range(1, 11))
    assert all(len(question.options) == 4 for question in first.questions)
    assert all(
        [option.label for option in question.options] == ["A", "B", "C", "D"]
        for question in first.questions
    )
    all_ids = {
        first.run_id,
        first.event.id,
        first.initial_round.id,
        *(question.id for question in first.questions),
        *(option.id for question in first.questions for option in question.options),
    }
    assert len(all_ids) == 53
    for number, question in enumerate(first.questions, start=1):
        assert question.id == uuid5(first.run_id, f"question:{number}")
        assert all(
            option.id == uuid5(first.run_id, f"question:{number}:option:{option.label}")
            for option in question.options
        )
    assert first.initial_round.id == uuid5(first.run_id, "round:1")
    assert first.public_origin == "https://quiz.aws.internal"
    assert "r2_object" not in first.model_dump()
    assert all("image_url" not in q.model_dump() for q in first.questions)
    assert first.scenario.lifecycle.ramp_targets == (100, 300, 500, 600)
    assert first.scenario.lifecycle.ramp_target_offsets_seconds == (150, 300, 450, 600)
    assert first.scenario.lifecycle.virtual_sessions == 600
    assert first.scenario.lifecycle.exclusive_window_confirmation_required is True
    assert [round_.start_offset_seconds for round_ in first.scenario.rounds] == [
        900 + 360 * index for index in range(10)
    ]
    assert first.scenario.rounds[2].replay == "all"
    assert first.scenario.rounds[3].receipt_discard_vu_end == 50
    assert first.scenario.rounds[3].receipt_discard_only_when_status == "Stored"
    assert first.scenario.rounds[3].answer_status_delay_ms == 2500
    assert first.scenario.rounds[3].retry_only_when_status == "NotStored"
    assert first.scenario.rounds[4].condition == "redis-unavailable"
    assert first.scenario.rounds[6].condition == "api-secondary-unavailable"
    assert first.scenario.rounds[9].replay == "all"
    assert all(round_.virtual_answer_count == 500 for round_ in first.scenario.rounds)
    assert all(
        tuple(round_.expected_option_counts.model_dump().values())
        == (126, 125, 125, 124)
        for round_ in first.scenario.rounds
    )
    assert first.scenario.protocol.static_asset_fetches_per_session == 1
    assert first.scenario.protocol.refetch_projection_after_invalidation is True
    assert first.scenario.protocol.send_readiness is True
    assert first.scenario.protocol.query_status_after_unknown_response is True
    assert first.scenario.thresholds.normal_expected_ready_count == 600
    assert first.scenario.thresholds.ready_count_minimum == 588
    assert first.scenario.thresholds.replay_same_answer_id_required is True
    assert first.scenario.thresholds.replay_row_change_maximum == 0
    assert first.scenario.thresholds.latency_report_statistics == (
        "p50",
        "p95",
        "p99",
        "max",
    )
    assert first.scenario.thresholds.hard_latency_threshold_ms is None
    assert first.scenario.answer_oracle.expected_option_counts.model_dump() == {
        "a": 126,
        "b": 125,
        "c": 125,
        "d": 124,
    }
    assert first.scenario.answer_oracle.non_answering_vu_start == 501
    assert first.scenario.answer_oracle.non_answering_vu_end == 598
    assert [fault.model_dump() for fault in first.scenario.fault_schedule] == [
        {"target": "redis", "action": "disable", "offset_seconds": 2280},
        {"target": "redis", "action": "enable", "offset_seconds": 2580},
        {"target": "api-secondary", "action": "disable", "offset_seconds": 3000},
        {"target": "api-secondary", "action": "enable", "offset_seconds": 3300},
    ]


def test_manifest_json_is_strict_and_secret_free() -> None:
    manifest = _manifest()
    serialized = manifest.model_dump_json()

    assert LoadTestManifest.model_validate_json(serialized) == manifest
    assert "database_url" not in serialized
    assert "redis_url" not in serialized
    assert "password" not in serialized.casefold()
    assert '"session_cookie"' not in serialized.casefold()
    assert '"cookie_value"' not in serialized.casefold()
    assert "is_correct" not in serialized
    assert "正解" not in serialized

    invalid_json = _mutated_json(
        manifest, lambda payload: payload.update({"extra": True})
    )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        LoadTestManifest.model_validate_json(invalid_json)


@pytest.mark.parametrize(
    "field_path",
    [
        ("format",),
        ("threshold_mode",),
        ("event", "state"),
        ("event", "ranking_visibility"),
        ("initial_round", "version"),
        ("initial_round", "state"),
        ("initial_round", "result_publication"),
    ],
)
def test_manifest_rejects_missing_fixed_contract_field(
    field_path: tuple[str, ...],
) -> None:
    payload = json.loads(_manifest().model_dump_json())
    assert isinstance(payload, dict)
    container = payload
    for field_name in field_path[:-1]:
        nested = container[field_name]
        assert isinstance(nested, dict)
        container = nested
    del container[field_path[-1]]

    with pytest.raises(ValidationError) as error_info:
        LoadTestManifest.model_validate_json(json.dumps(payload))

    error_locations = {tuple(error["loc"]) for error in error_info.value.errors()}
    assert field_path in error_locations


def test_manifest_rejects_legacy_v1_format() -> None:
    invalid_json = _mutated_json(
        _manifest(),
        lambda payload: payload.update({"format": "load-test-manifest-v1"}),
    )

    with pytest.raises(ValidationError) as error_info:
        LoadTestManifest.model_validate_json(invalid_json)

    assert ("format",) in {tuple(error["loc"]) for error in error_info.value.errors()}


@pytest.mark.parametrize(
    "field_mutator",
    [
        lambda payload: payload.update({"schema_revision": "older"}),
        lambda payload: payload["questions"][1].update(
            {"id": payload["questions"][0]["id"]}
        ),
        lambda payload: payload["questions"][0]["options"][1].update({"label": "A"}),
        lambda payload: payload["event"].update({"title": "実データ"}),
        lambda payload: payload["event"]["scoring_policy"].update(
            {"is_approved": False}
        ),
        lambda payload: payload["event"]["scoring_policy"].update(
            {"standard_duration_ms": 5_000}
        ),
        lambda payload: payload["event"]["display_name_policy"].update(
            {"reject_patterns_json": "{}"}
        ),
        lambda payload: payload["artifacts"].update({"api_image": "quiz:latest"}),
        lambda payload: payload["scenario"]["lifecycle"].update(
            {"virtual_sessions": 597}
        ),
        lambda payload: payload["scenario"]["answer_oracle"]["fixed_virtual_answers"][
            0
        ].update({"option": "D"}),
        lambda payload: payload["scenario"]["rounds"][4].update({"condition": "none"}),
        lambda payload: payload["scenario"]["rounds"][0].update(
            {"virtual_answer_count": 499}
        ),
        lambda payload: payload["scenario"]["rounds"][2].update(
            {"real_device_answers": ["B", "C"]}
        ),
        lambda payload: payload["scenario"]["rounds"][7][
            "expected_option_counts"
        ].update({"a": 125}),
        lambda payload: payload["scenario"]["fault_schedule"][0].update(
            {"offset_seconds": 2279}
        ),
        lambda payload: payload["scenario"]["protocol"].update(
            {"heartbeat_interval_seconds": 4}
        ),
        lambda payload: payload["scenario"]["thresholds"].update(
            {"ready_count_minimum": 587}
        ),
        lambda payload: payload["scenario"]["resource_abort"].update(
            {"scheduler_lag_ms_over": 51}
        ),
    ],
)
def test_manifest_rejects_schema_identity_content_and_artifact_drift(
    field_mutator: object,
) -> None:
    invalid_json = _mutated_json(_manifest(), field_mutator)

    with pytest.raises(ValidationError):
        LoadTestManifest.model_validate_json(invalid_json)


def test_manifest_requires_remote_https_origin_and_future_schedule() -> None:
    with pytest.raises(ValidationError, match="remote HTTPS"):
        _manifest(public_origin="http://localhost:8080")
    with pytest.raises(ValidationError, match="scheduled_start_at"):
        _manifest(scheduled_start_at=CREATED_AT)
    with pytest.raises(ValidationError, match="末尾slashなし"):
        _manifest(public_origin="https://quiz.aws.internal/")
    with pytest.raises(ValidationError, match="canonical"):
        _manifest(public_origin="https://QUIZ.aws.internal")


@pytest.mark.parametrize(
    "image_field", ["api_image", "gateway_image", "generator_image"]
)
def test_manifest_rejects_overlong_image_reference(image_field: str) -> None:
    overlong_digest_reference = f"registry.invalid/{'a' * 2000}@sha256:{'1' * 64}"

    with pytest.raises(ValidationError, match="string_too_long"):
        _manifest(**{image_field: overlong_digest_reference})


@pytest.mark.parametrize(
    "identity_mutator",
    [
        lambda payload: payload["questions"][2].update(
            {"id": "33333333-3333-4333-8333-333333333333"}
        ),
        lambda payload: payload["questions"][2]["options"][3].update(
            {"id": "44444444-4444-4444-8444-444444444444"}
        ),
        lambda payload: payload["initial_round"].update(
            {"id": "55555555-5555-4555-8555-555555555555"}
        ),
        lambda payload: payload.update(
            {"run_id": "66666666-6666-4666-8666-666666666666"}
        ),
    ],
)
def test_manifest_rejects_identity_not_derived_from_run_id(
    identity_mutator: object,
) -> None:
    invalid_json = _mutated_json(_manifest(), identity_mutator)

    with pytest.raises(ValidationError, match="規定UUIDv5"):
        LoadTestManifest.model_validate_json(invalid_json)


def test_manifest_write_is_exclusive_and_mode_0600(tmp_path: Path) -> None:
    artifact_directory = tmp_path / "artifacts"
    artifact_directory.mkdir(mode=0o700)
    path = artifact_directory / "run-manifest.json"
    manifest = _manifest()

    write_manifest(path, manifest, REPOSITORY_ROOT)

    assert load_manifest(path, REPOSITORY_ROOT) == manifest
    assert os.stat(path).st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        write_manifest(path, manifest, REPOSITORY_ROOT)
    assert load_manifest(path, REPOSITORY_ROOT) == manifest


def test_manifest_write_rejects_oversize_before_creating_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_directory = tmp_path / "artifacts"
    artifact_directory.mkdir(mode=0o700)
    path = artifact_directory / "oversize.json"
    manifest = _manifest()
    serialized_size = len(manifest.model_dump_json(indent=2).encode("utf-8")) + 1
    monkeypatch.setattr(manifest_module, "MAX_MANIFEST_BYTES", serialized_size - 1)

    with pytest.raises(ValueError, match="1MB以下"):
        write_manifest(path, manifest, REPOSITORY_ROOT)

    assert path.exists() is False


def test_manifest_io_rejects_repository_path_symlink_and_unsafe_mode(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    inside_path = REPOSITORY_ROOT / "tests" / "load-test-should-not-be-created.json"

    with pytest.raises(ValueError, match="repository外"):
        write_manifest(inside_path, manifest, REPOSITORY_ROOT)

    artifact_directory = tmp_path / "artifacts"
    artifact_directory.mkdir(mode=0o700)
    valid_path = artifact_directory / "valid.json"
    write_manifest(valid_path, manifest, REPOSITORY_ROOT)
    unsafe_path = artifact_directory / "unsafe.json"
    unsafe_path.write_bytes(valid_path.read_bytes())
    unsafe_path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        load_manifest(unsafe_path, REPOSITORY_ROOT)

    symlink_path = artifact_directory / "linked.json"
    symlink_path.symlink_to(valid_path)
    with pytest.raises(ValueError, match="通常file"):
        load_manifest(symlink_path, REPOSITORY_ROOT)

    shared_directory = tmp_path / "shared"
    shared_directory.mkdir(mode=0o770)
    shared_directory.chmod(0o770)
    with pytest.raises(ValueError, match="非共有書込"):
        write_manifest(shared_directory / "run.json", manifest, REPOSITORY_ROOT)


def test_manifest_io_rejects_caller_supplied_repository_root_bypass(
    tmp_path: Path,
) -> None:
    attacker_root = tmp_path / "attacker-root"
    attacker_root.mkdir()
    artifact_directory = tmp_path / "artifacts"
    artifact_directory.mkdir(mode=0o700)

    with pytest.raises(ValueError, match="実runtime rootと一致"):
        write_manifest(
            artifact_directory / "run.json",
            _manifest(),
            attacker_root,
        )


@pytest.mark.parametrize("legacy", ["load-test-manifest-v1", "load-test-manifest-v2"])
def test_manifest_rejects_previous_formats(legacy: str) -> None:
    payload = json.loads(_manifest().model_dump_json())
    payload["format"] = legacy
    with pytest.raises(ValidationError):
        LoadTestManifest.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("field", ["image_url", "image_alt"])
def test_manifest_rejects_question_image_fields(field: str) -> None:
    payload = json.loads(_manifest().model_dump_json())
    payload["questions"][0][field] = None
    with pytest.raises(ValidationError, match="extra_forbidden"):
        LoadTestManifest.model_validate_json(json.dumps(payload))
