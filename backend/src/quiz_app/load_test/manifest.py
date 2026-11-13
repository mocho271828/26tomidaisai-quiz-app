from __future__ import annotations

import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import UUID, uuid5

from pydantic import (
    UUID4,
    AnyHttpUrl,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from quiz_app.config import EVENT_SLUG_PATTERN, is_local_service_host
from quiz_app.domain.rounds import STANDARD_DURATION_MS
from quiz_app.load_test.scenario import LoadTestScenarioManifest
from quiz_app.load_test.scenario_factory import build_fixed_scenario
from quiz_app.schema_revision import SCHEMA_REVISION

MANIFEST_FORMAT = "load-test-manifest-v3"
LOAD_TEST_PREFIX = "[LOAD-TEST]"
OPTION_LABELS = ("A", "B", "C", "D")
MAX_MANIFEST_BYTES = 1_000_000
IMAGE_REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}$")
GitCommit = Annotated[StrictStr, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
Sha256 = Annotated[StrictStr, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ImageReference = Annotated[StrictStr, StringConstraints(max_length=2048)]
PolicyVersion = Annotated[StrictStr, StringConstraints(min_length=1, max_length=64)]
SyntheticText = Annotated[
    StrictStr,
    StringConstraints(strip_whitespace=True, min_length=12, max_length=500),
]
REJECT_PATTERNS_ADAPTER = TypeAdapter(list[StrictStr], config=ConfigDict(strict=True))
PUBLIC_ORIGIN_ADAPTER = TypeAdapter(AnyHttpUrl, config=ConfigDict(strict=True))


class StrictManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class LoadTestArtifactManifest(StrictManifestModel):
    source_commit: GitCommit
    api_image: ImageReference
    gateway_image: ImageReference
    generator_image: ImageReference

    @field_validator("api_image", "gateway_image", "generator_image")
    @classmethod
    def validate_digest_pinned_image(cls, image: str) -> str:
        if IMAGE_REFERENCE_PATTERN.fullmatch(image) is None:
            raise ValueError("container imageはsha256 digest固定で指定してください")
        return image


class LoadTestScoringPolicyManifest(StrictManifestModel):
    version: PolicyVersion
    base_points: StrictInt = Field(ge=0)
    initial_speed_bonus: StrictInt = Field(ge=0)
    speed_bucket_ms: StrictInt = Field(gt=0)
    speed_decrement_per_bucket: StrictInt = Field(ge=0)
    standard_duration_ms: StrictInt = Field(gt=0)
    is_approved: Literal[True]

    @field_validator("standard_duration_ms")
    @classmethod
    def validate_standard_duration(cls, duration_ms: int) -> int:
        if duration_ms != STANDARD_DURATION_MS:
            raise ValueError("通常回答時間は10秒に固定してください")
        return duration_ms


class LoadTestDisplayNamePolicyManifest(StrictManifestModel):
    version: PolicyVersion
    notice_version: PolicyVersion
    max_code_points: StrictInt = Field(ge=1, le=100)
    reject_patterns_json: StrictStr = Field(min_length=2, max_length=10_000)

    @field_validator("reject_patterns_json")
    @classmethod
    def validate_reject_patterns(cls, value: str) -> str:
        REJECT_PATTERNS_ADAPTER.validate_json(value)
        return value


class LoadTestEventManifest(StrictManifestModel):
    id: UUID4
    slug: StrictStr
    title: SyntheticText
    state: Literal["LobbyOpen"]
    ranking_visibility: Literal["HiddenAll"]
    scoring_policy: LoadTestScoringPolicyManifest
    display_name_policy: LoadTestDisplayNamePolicyManifest

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, slug: str) -> str:
        if EVENT_SLUG_PATTERN.fullmatch(slug) is None:
            raise ValueError("Event slugが不正です")
        return slug

    @field_validator("title")
    @classmethod
    def validate_synthetic_title(cls, title: str) -> str:
        if not title.startswith(LOAD_TEST_PREFIX):
            raise ValueError("負荷試験Event titleには[LOAD-TEST] prefixが必要です")
        return title


class LoadTestOptionManifest(StrictManifestModel):
    id: UUID
    label: Literal["A", "B", "C", "D"]
    content: SyntheticText
    display_order: StrictInt = Field(ge=0, le=3)

    @field_validator("content")
    @classmethod
    def validate_synthetic_content(cls, content: str) -> str:
        if not content.startswith(LOAD_TEST_PREFIX):
            raise ValueError("負荷試験選択肢には[LOAD-TEST] prefixが必要です")
        return content


class LoadTestQuestionManifest(StrictManifestModel):
    id: UUID
    version: StrictInt = Field(ge=1, le=10)
    prompt: SyntheticText
    explanation: SyntheticText
    display_order: StrictInt = Field(ge=1, le=10)
    options: tuple[LoadTestOptionManifest, ...] = Field(min_length=4, max_length=4)

    @field_validator("prompt", "explanation")
    @classmethod
    def validate_synthetic_text(cls, value: str) -> str:
        if not value.startswith(LOAD_TEST_PREFIX):
            raise ValueError("負荷試験問題には[LOAD-TEST] prefixが必要です")
        return value

    @model_validator(mode="after")
    def validate_options(self) -> Self:
        if tuple(option.label for option in self.options) != OPTION_LABELS:
            raise ValueError("選択肢labelはA,B,C,Dの順で指定してください")
        if tuple(option.display_order for option in self.options) != (0, 1, 2, 3):
            raise ValueError("選択肢display_orderは0,1,2,3の順で指定してください")
        if len({option.id for option in self.options}) != 4:
            raise ValueError("選択肢IDが重複しています")
        return self


class LoadTestInitialRoundManifest(StrictManifestModel):
    id: UUID
    question_id: UUID
    version: Literal[1]
    state: Literal["PreparingAssets"]
    result_publication: Literal["Withheld"]


class LoadTestManifest(StrictManifestModel):
    format: Literal["load-test-manifest-v3"]
    run_id: UUID4
    created_at: AwareDatetime
    scheduled_start_at: AwareDatetime
    public_origin: StrictStr
    schema_revision: StrictStr
    threshold_mode: Literal["calibration"]
    artifacts: LoadTestArtifactManifest
    event: LoadTestEventManifest
    questions: tuple[LoadTestQuestionManifest, ...] = Field(min_length=10, max_length=10)
    initial_round: LoadTestInitialRoundManifest
    scenario: LoadTestScenarioManifest

    @field_validator("public_origin")
    @classmethod
    def validate_public_origin(cls, origin: str) -> str:
        parsed_origin = PUBLIC_ORIGIN_ADAPTER.validate_python(origin)
        canonical_origin = str(parsed_origin).removesuffix("/")
        if (
            parsed_origin.scheme != "https"
            or is_local_service_host(parsed_origin.host)
            or parsed_origin.username is not None
            or parsed_origin.password is not None
            or parsed_origin.path not in {None, "", "/"}
            or parsed_origin.query is not None
            or parsed_origin.fragment is not None
            or origin != canonical_origin
        ):
            raise ValueError(
                "public originは末尾slashなしのcanonical remote HTTPS originだけを指定してください"
            )
        return origin

    @field_validator("schema_revision")
    @classmethod
    def validate_schema_revision(cls, revision: str) -> str:
        if revision != SCHEMA_REVISION:
            raise ValueError("DB schema revisionが実装と一致しません")
        return revision

    @model_validator(mode="after")
    def validate_manifest_relations(self) -> Self:
        if self.scheduled_start_at <= self.created_at:
            raise ValueError("scheduled_start_atはcreated_atより後にしてください")
        if tuple(question.version for question in self.questions) != tuple(range(1, 11)):
            raise ValueError("問題versionは1から10の順で指定してください")
        if tuple(question.display_order for question in self.questions) != tuple(range(1, 11)):
            raise ValueError("問題display_orderは1から10の順で指定してください")
        if self.initial_round.question_id != self.questions[0].id:
            raise ValueError("初期Roundは1問目を参照してください")

        for number, question in enumerate(self.questions, start=1):
            if question.id != _entity_id(self.run_id, f"question:{number}"):
                raise ValueError("Question IDがrun_idからの規定UUIDv5と一致しません")
            for option in question.options:
                expected_option_id = _entity_id(
                    self.run_id,
                    f"question:{number}:option:{option.label}",
                )
                if option.id != expected_option_id:
                    raise ValueError("Option IDがrun_idからの規定UUIDv5と一致しません")
        if self.initial_round.id != _entity_id(self.run_id, "round:1"):
            raise ValueError("初期Round IDがrun_idからの規定UUIDv5と一致しません")

        entity_ids = [self.run_id, self.event.id, self.initial_round.id]
        entity_ids.extend(question.id for question in self.questions)
        entity_ids.extend(option.id for question in self.questions for option in question.options)
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError("manifest内のIDが重複しています")
        return self


def _entity_id(run_id: UUID, name: str) -> UUID:
    return uuid5(run_id, name)


def build_load_test_manifest(
    *,
    run_id: UUID4,
    event_id: UUID4,
    event_slug: str,
    scheduled_start_at: datetime,
    public_origin: str,
    source_commit: str,
    api_image: str,
    gateway_image: str,
    generator_image: str,
    scoring_policy_version: str,
    scoring_base_points: int,
    scoring_initial_speed_bonus: int,
    scoring_speed_bucket_ms: int,
    scoring_speed_decrement_per_bucket: int,
    scoring_standard_duration_ms: int,
    scoring_is_approved: bool,
    display_name_policy_version: str,
    display_notice_version: str,
    display_max_code_points: int,
    display_reject_patterns_json: str,
    created_at: datetime,
) -> LoadTestManifest:
    """run固有IDから10問分の架空dataと初期Roundを決定的に生成する。"""

    questions: list[LoadTestQuestionManifest] = []
    for number in range(1, 11):
        question_id = _entity_id(run_id, f"question:{number}")
        options = tuple(
            LoadTestOptionManifest(
                id=_entity_id(run_id, f"question:{number}:option:{label}"),
                label=label,
                content=f"{LOAD_TEST_PREFIX} 架空問題{number:02d}の選択肢{label}",
                display_order=display_order,
            )
            for display_order, label in enumerate(OPTION_LABELS)
        )
        questions.append(
            LoadTestQuestionManifest(
                id=question_id,
                version=number,
                prompt=f"{LOAD_TEST_PREFIX} 通信確認用の架空問題{number:02d}です。",
                explanation=f"{LOAD_TEST_PREFIX} 通信確認用の架空問題{number:02d}です。",
                display_order=number,
                options=options,
            )
        )

    return LoadTestManifest(
        format=MANIFEST_FORMAT,
        run_id=run_id,
        created_at=created_at,
        scheduled_start_at=scheduled_start_at,
        public_origin=public_origin,
        schema_revision=SCHEMA_REVISION,
        threshold_mode="calibration",
        artifacts=LoadTestArtifactManifest(
            source_commit=source_commit,
            api_image=api_image,
            gateway_image=gateway_image,
            generator_image=generator_image,
        ),
        event=LoadTestEventManifest(
            id=event_id,
            slug=event_slug,
            title=f"{LOAD_TEST_PREFIX} {event_slug}",
            state="LobbyOpen",
            ranking_visibility="HiddenAll",
            scoring_policy=LoadTestScoringPolicyManifest(
                version=scoring_policy_version,
                base_points=scoring_base_points,
                initial_speed_bonus=scoring_initial_speed_bonus,
                speed_bucket_ms=scoring_speed_bucket_ms,
                speed_decrement_per_bucket=scoring_speed_decrement_per_bucket,
                standard_duration_ms=scoring_standard_duration_ms,
                is_approved=scoring_is_approved,
            ),
            display_name_policy=LoadTestDisplayNamePolicyManifest(
                version=display_name_policy_version,
                notice_version=display_notice_version,
                max_code_points=display_max_code_points,
                reject_patterns_json=display_reject_patterns_json,
            ),
        ),
        questions=tuple(questions),
        initial_round=LoadTestInitialRoundManifest(
            id=_entity_id(run_id, "round:1"),
            question_id=questions[0].id,
            version=1,
            state="PreparingAssets",
            result_publication="Withheld",
        ),
        scenario=build_fixed_scenario(),
    )


def _runtime_root_from_module() -> Path:
    """caller入力と独立に、source checkoutまたはcontainerの実rootを導出する。"""

    module_path = Path(__file__).resolve(strict=True)
    layouts = (
        (
            Path("backend/src/quiz_app/load_test/manifest.py"),
            (Path("backend/pyproject.toml"), Path("compose.yaml")),
        ),
        (
            Path("src/quiz_app/load_test/manifest.py"),
            (Path("pyproject.toml"), Path("alembic.ini")),
        ),
    )
    # source checkoutの内側はcontainer layoutにも見えるため、外側layoutを先に全探索する。
    for module_sentinel, root_sentinels in layouts:
        for candidate_root in module_path.parents:
            candidate_module = candidate_root / module_sentinel
            if (
                candidate_module.is_file()
                and candidate_module.samefile(module_path)
                and all((candidate_root / sentinel).is_file() for sentinel in root_sentinels)
            ):
                return candidate_root.resolve(strict=True)
    raise ValueError("runtime rootをmodule sentinelから導出できません")


def _require_runtime_root(repository_root: Path) -> Path:
    supplied_root = repository_root.resolve(strict=True)
    runtime_root = _runtime_root_from_module()
    if supplied_root != runtime_root:
        raise ValueError("repository rootが実runtime rootと一致しません")
    return runtime_root


def _require_external_safe_parent(path: Path, repository_root: Path) -> None:
    parent_status = path.parent.lstat()
    resolved_parent = path.parent.resolve(strict=True)
    resolved_repository = _require_runtime_root(repository_root)
    if (
        path.parent.is_symlink()
        or not stat.S_ISDIR(parent_status.st_mode)
        or parent_status.st_uid != os.geteuid()
        or stat.S_IMODE(parent_status.st_mode) & 0o022
        or resolved_parent.is_relative_to(resolved_repository)
    ):
        raise ValueError(
            "manifest親directoryはrepository外の所有済み非共有書込directoryにしてください"
        )


def load_manifest(path: Path, repository_root: Path) -> LoadTestManifest:
    """0600の通常fileだけをstrict JSONとして読み込む。"""

    _require_external_safe_parent(path, repository_root)
    path_status = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(path_status.st_mode):
        raise ValueError("manifestは通常fileで指定してください")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    file_status = os.fstat(descriptor)
    if (
        not stat.S_ISREG(file_status.st_mode)
        or stat.S_IMODE(file_status.st_mode) != 0o600
        or file_status.st_size > MAX_MANIFEST_BYTES
    ):
        os.close(descriptor)
        raise ValueError("manifestは1MB以下の0600通常fileで指定してください")
    with os.fdopen(descriptor, "rb") as manifest_file:
        return LoadTestManifest.model_validate_json(manifest_file.read(MAX_MANIFEST_BYTES + 1))


def write_manifest(
    path: Path,
    manifest: LoadTestManifest,
    repository_root: Path,
) -> None:
    """既存artifactを上書きせず0600でmanifestを永続化する。"""

    _require_external_safe_parent(path, repository_root)
    serialized_body = manifest.model_dump_json(indent=2).encode("utf-8")
    serialized_size = len(serialized_body) + 1
    if not serialized_body or serialized_size > MAX_MANIFEST_BYTES:
        raise ValueError("manifestは1MB以下の非空JSONにしてください")
    serialized = serialized_body + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(serialized)
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        path.unlink(missing_ok=True)
        raise
