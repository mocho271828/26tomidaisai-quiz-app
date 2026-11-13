from __future__ import annotations

from pathlib import Path

from quiz_app.load_test.controller_contracts import ControllerStatus
from quiz_app.load_test.db_verifier import run_db_verifier
from quiz_app.load_test.db_verifier_artifacts import (
    load_db_verifier_request,
)
from quiz_app.load_test.db_verifier_database import (
    VerifierDatabase,
    create_verifier_engine,
    load_verifier_database_settings,
)
from quiz_app.load_test.db_verifier_models import DbVerifierRunResult
from quiz_app.load_test.evidence_handoff import (
    DbInputMode,
    seal_db_verifier_input,
    seal_lock_observation_input,
)
from quiz_app.load_test.manifest import LoadTestManifest
from quiz_app.load_test.orchestrator_contracts import (
    OrchestratorError,
    RunFiles,
    RunnerConfiguration,
)
from quiz_app.load_test.orchestrator_process import RunClock
from quiz_app.load_test.verification_contracts import ArtifactIdentity


class RunDatabase:
    """read-only verifierを各観測の独立接続で実行する。"""

    def __init__(
        self,
        files: RunFiles,
        manifest: LoadTestManifest,
        config: RunnerConfiguration,
        clock: RunClock,
    ) -> None:
        self.files = files
        self.manifest = manifest
        self.clock = clock
        self.settings = load_verifier_database_settings(
            files.input("verifier-database-url"),
            files.input("postgres-ca.pem"),
            config.postgres_ca_sha256,
            files.repository_root,
        )

    def connection(self) -> VerifierDatabase:
        return VerifierDatabase(create_verifier_engine(self.settings), self.settings)

    def preflight(self) -> None:
        database = self.connection()
        try:
            with database.read_only_snapshot(
                expected_schema_revision=self.manifest.schema_revision
            ):
                pass
        finally:
            database.dispose()

    def verify(
        self, artifact: ArtifactIdentity, *, generator: Path | None = None
    ) -> DbVerifierRunResult:
        loaded = load_db_verifier_request(self.files.output("db-inputs") / artifact.name)
        if loaded.artifact != artifact:
            raise OrchestratorError("orchestrator_db_input_changed")
        database = self.connection()
        try:
            result = run_db_verifier(
                self.manifest,
                loaded,
                database,
                self.files.output("db-reports"),
                generator_evidence_path=generator,
                clock=self.clock.now,
                sleeper=self.clock.sleep,
            )
        finally:
            database.dispose()
        if result.decision != "PASS":
            raise OrchestratorError("orchestrator_db_verification_failed")
        return result

    def observe_locks(self, number: int) -> DbVerifierRunResult:
        return self.verify(
            seal_lock_observation_input(
                self.manifest, self.files.output("db-inputs"), round_number=number
            )
        )

    def seal(self, status: ControllerStatus, *, mode: DbInputMode) -> ArtifactIdentity:
        """手動CLIと同じmanifest/status/head束縛を自動進行でも維持する。"""
        return seal_db_verifier_input(
            self.manifest,
            status,
            self.files.output("requests"),
            self.files.output("db-inputs"),
            mode=mode,
        )

    def verify_round(self, status: ControllerStatus, generator: Path) -> DbVerifierRunResult:
        return self.verify(self.seal(status, mode="per-round"), generator=generator)
