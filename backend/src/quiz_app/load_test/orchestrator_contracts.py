from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import UUID

import logfire
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from quiz_app.load_test.controller_contracts import manifest_context
from quiz_app.load_test.controller_journal import require_secure_artifact_directory
from quiz_app.load_test.db_verifier_artifacts import read_secure_external_file
from quiz_app.load_test.manifest import LoadTestManifest, load_manifest
from quiz_app.load_test.verification_contracts import ArtifactIdentity, Sha256
from quiz_app.load_test_aws.models import AwsPreparationEvidence, CleanupInput, SchemaReceipt

MAX_METRICS_ARTIFACT_BYTES = 256 * 1024 * 1024


class OrchestratorError(RuntimeError):
    """秘密値を含まない自動実行の固定失敗理由。"""


class StrictRunModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RunnerConfiguration(StrictRunModel):
    format: Literal["load-test-orchestrator-v1"]
    run_id: UUID
    postgres_ca_sha256: Sha256


class AppWorkerConfiguration(StrictRunModel):
    format: Literal["load-test-app-worker-v1"]
    run_id: UUID
    release_file: Path
    rollback_file: Path


class OperatorCredentials(StrictRunModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: SecretStr = Field(min_length=1, max_length=1024)


class RunResult(StrictRunModel):
    format: Literal["load-test-orchestrator-result-v1"] = "load-test-orchestrator-result-v1"
    run_id: UUID
    manifest_sha256: Sha256
    decision: Literal["PASS", "FAIL", "INTERRUPTED"]
    completed_rounds: int = Field(ge=0, le=10)
    artifacts: tuple[ArtifactIdentity, ...]
    finished_at: datetime

    @model_validator(mode="after")
    def require_completed_run_for_pass(self) -> RunResult:
        if self.decision == "PASS" and self.completed_rounds != 10:
            raise ValueError("orchestrator_pass_requires_ten_rounds")
        return self


def require_private_directory(directory: Path, repository_root: Path) -> None:
    """親を含むsymlinkとrepository内の実行データを拒否する。"""
    descriptor = require_secure_artifact_directory(
        directory, repository_root, allowed_names={item.name for item in directory.iterdir()}
    )
    os.close(descriptor)


class RunFiles:
    """実行時入力・出力の名前を固定し、任意コマンドやartifact上書きを許さない。"""

    def __init__(self, root: Path, repository_root: Path) -> None:
        require_private_directory(root, repository_root)
        require_private_directory(root / "inputs", repository_root)
        self.root = root
        self.repository_root = repository_root

    def input(self, name: str) -> Path:
        return self.root / "inputs" / name

    def output(self, name: str) -> Path:
        directory = self.root / name
        directory.mkdir(mode=0o700, exist_ok=True)
        require_private_directory(directory, self.repository_root)
        return directory

    def load_binding(self, *, now: datetime) -> tuple[LoadTestManifest, AwsPreparationEvidence]:
        """作成済みAWS環境、CA、manifest、最大期限を同じrunへ束縛する。"""
        manifest = load_manifest(self.input("manifest.json"), self.repository_root)
        raw, _ = read_secure_external_file(self.input("preparation.json"))
        prepared = AwsPreparationEvidence.model_validate_json(raw)
        ca, _ = read_secure_external_file(self.input("application-ca.pem"))
        if (
            self.root.name != str(manifest.run_id)
            or prepared.run_id != manifest.run_id
            or prepared.manifest_sha256 != manifest_context(manifest).manifest_sha256
            or prepared.source_commit != manifest.artifacts.source_commit
            or prepared.public_origin != manifest.public_origin
            or prepared.ca_sha256 != sha256(ca).hexdigest()
            or prepared.prepared_at > now
            or prepared.expires_at <= now
            or manifest.scheduled_start_at + timedelta(seconds=5_100) >= prepared.expires_at
        ):
            raise OrchestratorError("orchestrator_preparation_binding_invalid")
        return manifest, prepared


def cleanup_input(prepared: AwsPreparationEvidence) -> CleanupInput:
    return CleanupInput(
        run_id=prepared.run_id,
        account_id=prepared.account_id,
        region=prepared.region,
        expires_at=prepared.expires_at,
    )


def load_runner_configuration(
    files: RunFiles, prepared: AwsPreparationEvidence, manifest: LoadTestManifest
) -> RunnerConfiguration:
    raw, _ = read_secure_external_file(files.input("orchestrator.json"))
    config = RunnerConfiguration.model_validate_json(raw)
    raw, _ = read_secure_external_file(files.input("schema.json"))
    receipt = SchemaReceipt.model_validate_json(raw)
    if (
        config.run_id != prepared.run_id
        or receipt.run_id != prepared.run_id
        or receipt.account_id != prepared.account_id
        or receipt.schema_revision != manifest.schema_revision
        or not manifest.created_at <= receipt.verified_at <= datetime.now(UTC)
    ):
        raise OrchestratorError("orchestrator_schema_receipt_invalid")
    return config


def log_failure(code: str, error: BaseException) -> None:
    """境界入力を含む例外本文を捨て、元の発生位置だけを構造化ログへ残す。"""
    safe = OrchestratorError(code)
    logfire.exception(
        "load-test execution failed",
        _exc_info=(type(safe), safe, error.__traceback__),
        error_code=code,
    )
    # AWS撤去がhostを終了しても、SSMに秘密を含まない発生位置を残す。
    # traceback.format_exceptionは例外本文・source行を含むため使用しない。
    source_root = Path(__file__).resolve().parents[1]
    locations = []
    for frame, line in traceback.walk_tb(error.__traceback__):
        path = Path(frame.f_code.co_filename)
        if path.is_relative_to(source_root):
            locations.append({"module": str(path.relative_to(source_root)), "line": line})
    try:
        print(
            json.dumps({"status": "failed", "code": code, "locations": locations[-8:]}),
            file=sys.stderr,
            flush=True,
        )
    except (OSError, ValueError):
        # 閉じたSSM出力は再試行できない。診断不能を記録して撤去処理へ進む。
        logfire.warning("load-test failure diagnostic unavailable", error_code=code)
