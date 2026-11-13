from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID

import logfire

from quiz_app.load_test.controller import LoadTestController
from quiz_app.load_test.controller_contracts import (
    ControllerAction,
    ControllerStatus,
    manifest_context,
)
from quiz_app.load_test.controller_http import ControllerHttpClient
from quiz_app.load_test.controller_journal import ControllerJournal
from quiz_app.load_test.controller_verification import FileVerificationProvider
from quiz_app.load_test.db_verifier_artifacts import (
    read_secure_external_file,
    write_canonical_artifact,
)
from quiz_app.load_test.db_verifier_models import DbVerifierRunResult
from quiz_app.load_test.fault_controller_contracts import (
    fault_recovery_deadline,
    fault_recovery_offset_for_round,
)
from quiz_app.load_test.manifest import LoadTestManifest
from quiz_app.load_test.orchestrator_acceptance import seal_acceptance
from quiz_app.load_test.orchestrator_contracts import (
    MAX_METRICS_ARTIFACT_BYTES,
    OperatorCredentials,
    OrchestratorError,
    RunFiles,
    RunnerConfiguration,
    RunResult,
    StrictRunModel,
)
from quiz_app.load_test.orchestrator_database import RunDatabase
from quiz_app.load_test.orchestrator_process import GeneratorProcess, RunClock
from quiz_app.load_test.orchestrator_remote import (
    app_worker_status,
    download_archive,
    monitor_app_worker,
    start_app_worker,
)
from quiz_app.load_test.runtime_configuration import matches_formal_runtime_configuration
from quiz_app.load_test.verification_contracts import ArtifactIdentity
from quiz_app.load_test_aws.aws import AwsApi
from quiz_app.load_test_aws.models import AwsPreparationEvidence


class RampReady(StrictRunModel):
    format: Literal["load-test-generator-ramp-ready-v1"]
    run_id: UUID
    event_id: UUID
    manifest_sha256: str
    sessions: Literal[600]
    websocket_connected: Literal[600]
    observed_at_unix_ms: int


class LoadTestOrchestrator:
    """固定10問を進行し、検証証跡が揃うまで次のRoundへ進めない。"""

    def __init__(
        self,
        files: RunFiles,
        manifest: LoadTestManifest,
        prepared: AwsPreparationEvidence,
        config: RunnerConfiguration,
        aws: AwsApi,
        clock: RunClock,
    ) -> None:
        self.files = files
        self.manifest = manifest
        self.prepared = prepared
        self.aws = aws
        self.clock = clock
        body, _ = read_secure_external_file(files.input("operator.json"))
        self.credentials = OperatorCredentials.model_validate_json(body)
        self.http = ControllerHttpClient(
            manifest,
            reauthenticate=lambda client: client.login(
                self.credentials.username, self.credentials.password
            ),
            ca_file=files.input("application-ca.pem"),
        )
        self.database = RunDatabase(files, manifest, config, clock)
        self.generator = GeneratorProcess(files)
        self.artifacts: list[ArtifactIdentity] = []
        self.completed_rounds = 0
        self.fault_command: str | None = None
        self.fault_monitor: Future[bool] | None = None

    def preflight(self) -> None:
        if self.clock.now() >= self.manifest.scheduled_start_at - timedelta(seconds=30):
            raise OrchestratorError("orchestrator_start_window_missed")
        live = self.http.get_liveness()
        ready = self.http.get_readiness()
        server_time = self.http.get_server_time().server_time
        if (
            live.version != self.manifest.artifacts.source_commit
            or ready.status != "ready"
            or ready.database != "ok"
            or ready.migration != "current"
            or ready.redis != "ok"
            or abs((self.clock.now() - server_time).total_seconds()) > 0.5
        ):
            raise OrchestratorError("orchestrator_read_only_preflight_failed")
        self.http.login(self.credentials.username, self.credentials.password)
        state = self.http.get_event_state()
        if state.event_id != self.manifest.event.id or state.participant_count != 0:
            raise OrchestratorError("orchestrator_fresh_event_required")
        if not matches_formal_runtime_configuration(state):
            raise OrchestratorError("orchestrator_quiz_configuration_mismatch")
        self.database.preflight()

    def _check(self, *, allow_generator_completed: bool = False) -> None:
        self.clock.check()
        self.generator.check(allow_completed=allow_generator_completed)
        if self.fault_monitor is not None and self.fault_monitor.done():
            self.fault_monitor.result()

    def _wait_until(self, offset: float, *, allow_completed: bool = False) -> None:
        target = self.manifest.scheduled_start_at + timedelta(seconds=offset)
        while self.clock.now() < target:
            self._check(allow_generator_completed=allow_completed)
            self.clock.sleep(min(0.25, (target - self.clock.now()).total_seconds()))

    def _wait_for(
        self,
        predicate: Callable[[], bool],
        deadline_offset: float,
        *,
        allow_completed: bool = False,
        inclusive_deadline: bool = False,
    ) -> None:
        deadline = self.manifest.scheduled_start_at + timedelta(seconds=deadline_offset)

        def has_expired() -> bool:
            current = self.clock.now()
            return current > deadline if inclusive_deadline else current >= deadline

        while True:
            self._check(allow_generator_completed=allow_completed)
            if has_expired():
                raise OrchestratorError("orchestrator_phase_deadline_exceeded")
            if predicate():
                if has_expired():
                    raise OrchestratorError("orchestrator_phase_deadline_exceeded")
                return
            self.clock.sleep(0.25)

    def _ramp_ready(self) -> bool:
        path = self.files.output("generator") / "generator-ramp-ready.json"
        if not path.exists():
            return False
        raw, identity = read_secure_external_file(path)
        evidence = RampReady.model_validate_json(raw)
        start_ms = int(self.manifest.scheduled_start_at.timestamp() * 1000)
        now_ms = int(self.clock.now().timestamp() * 1000)
        if (
            evidence.run_id != self.manifest.run_id
            or evidence.event_id != self.manifest.event.id
            or evidence.manifest_sha256 != manifest_context(self.manifest).manifest_sha256
            or not start_ms + 600_000 <= evidence.observed_at_unix_ms <= start_ms + 660_000
            or not 0 <= now_ms - evidence.observed_at_unix_ms <= 2000
            or self.http.get_event_state().participant_count != 600
        ):
            raise OrchestratorError("orchestrator_ramp_evidence_invalid")
        self.artifacts.append(identity)
        return True

    def _action(
        self,
        controller: LoadTestController,
        action: ControllerAction,
        *,
        evidence: Path | None = None,
        receipt: Path | None = None,
    ) -> ControllerStatus:
        self._check()
        return controller.execute(
            action,
            username=self.credentials.username,
            password=self.credentials.password,
            confirmation_phrase=controller.confirmation_prompt(action, receipt_path=receipt),
            generator_evidence_path=evidence,
            receipt_path=receipt,
        )

    def _round(
        self, controller: LoadTestController, workers: ThreadPoolExecutor, number: int
    ) -> ArtifactIdentity | None:
        slot = self.manifest.scenario.rounds[number - 1]
        deadline = (
            self.manifest.scenario.rounds[number].start_offset_seconds - 10 if number < 10 else 4490
        )
        self._wait_until(slot.start_offset_seconds - 10)
        locks: Future[DbVerifierRunResult] = workers.submit(self.database.observe_locks, number)
        self._wait_until(slot.start_offset_seconds - 5.5)
        self._action(controller, "schedule")
        evidence = self.files.output("generator") / f"generator-round-{number:02d}.json"

        def evidence_ready() -> bool:
            if not locks.done():
                return False
            locks.result()
            return evidence.exists()

        self._wait_for(evidence_ready, deadline)
        lock_result = locks.result()
        self.artifacts.append(lock_result.report)
        status = self._action(controller, "publish")
        while status.next_action == "publish":
            if self.clock.now() >= self.manifest.scheduled_start_at + timedelta(seconds=deadline):
                raise OrchestratorError("orchestrator_round_results_deadline_exceeded")
            self.clock.sleep(1)
            status = self._action(controller, "publish")
        status = self._action(controller, "wait-results", evidence=evidence)
        while status.verification_request_artifact is None:
            if self.clock.now() >= self.manifest.scheduled_start_at + timedelta(seconds=deadline):
                raise OrchestratorError("orchestrator_round_results_deadline_exceeded")
            self.clock.sleep(1)
            status = self._action(controller, "wait-results", evidence=evidence)
        verified = self.database.verify_round(status, evidence)
        if verified.receipt is None:
            raise OrchestratorError("orchestrator_verification_receipt_missing")
        self.artifacts.extend((verified.report, verified.receipt))
        # Event完了前の検証済みstatus/headを固定し、完了後に別入力を生成しない。
        final_input = self.database.seal(status, mode="final") if number == 10 else None
        recovery = fault_recovery_offset_for_round(self.manifest, number)
        if recovery is not None:
            self._wait_until(recovery + 1)
            if slot.condition == "redis-unavailable":
                recovery_at = self.manifest.scheduled_start_at + timedelta(seconds=recovery)
                deadline = fault_recovery_deadline("redis", recovery_at)
                self._wait_for(
                    self._redis_ready,
                    (deadline - self.manifest.scheduled_start_at).total_seconds(),
                    inclusive_deadline=True,
                )
        self._action(
            controller,
            "advance",
            receipt=self.files.output("db-reports") / verified.receipt.name,
        )
        if number < 10:
            # 新しいRoundにも対象600人のfreezeが必要で、advanceだけでは準備は開始されない。
            self._action(controller, "prepare")
        self.completed_rounds = number
        logfire.info("load-test automated round completed", round_number=number)
        return final_input

    def _redis_ready(self) -> bool:
        ready = self.http.get_readiness()
        return (
            ready.status == "ready"
            and ready.database == "ok"
            and ready.migration == "current"
            and ready.redis == "ok"
        )

    def run(self) -> RunResult:
        self.preflight()
        self.fault_command = start_app_worker(self.aws, self.prepared, "faults")
        self.fault_monitor = monitor_app_worker(
            self.aws, self.prepared, self.fault_command, self.clock.stop
        )
        self.generator.start()
        self._wait_until(self.manifest.scenario.lifecycle.ramp_end_seconds)
        self._wait_for(self._ramp_ready, 660)
        with ControllerJournal(
            self.files.output("journal") / "controller.jsonl", self.files.repository_root
        ) as journal:
            controller = LoadTestController(
                self.manifest,
                journal,
                self.files.output("checkpoints"),
                self.files.output("requests"),
                self.files.repository_root,
                self.http,
                FileVerificationProvider(),
            )
            self._action(controller, "prepare")
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="load-test-locks") as workers:
                final_input = None
                try:
                    for number in range(1, 11):
                        final_input = self._round(controller, workers, number)
                finally:
                    if self.completed_rounds < 10:
                        self.clock.stop.set()
                if final_input is None:
                    raise OrchestratorError("orchestrator_rounds_missing")
                final = self.database.verify(
                    final_input,
                    generator=self.files.output("generator") / "generator-round-10.json",
                )
                self.artifacts.append(final.report)
        self._wait_until(4800, allow_completed=True)
        self._wait_for(
            lambda: self.generator.check(allow_completed=True), 4920, allow_completed=True
        )
        self._wait_for(
            lambda: app_worker_status(self.aws, self.prepared, self.fault_command),
            4920,
            allow_completed=True,
        )
        export_command = start_app_worker(self.aws, self.prepared, "export")
        self._wait_for(
            lambda: app_worker_status(self.aws, self.prepared, export_command),
            5100,
            allow_completed=True,
        )
        for action, folder in (("faults", "app-faults"), ("export", "app-export")):
            paths = download_archive(self.aws, self.prepared, action, self.files.output(folder))
            self.artifacts.extend(
                read_secure_external_file(path, maximum_bytes=MAX_METRICS_ARTIFACT_BYTES)[1]
                for path in paths
            )
        self.artifacts.append(
            read_secure_external_file(
                self.files.output("generator") / "generator-metrics-summary.json"
            )[1]
        )
        self.artifacts.append(
            seal_acceptance(
                self.manifest,
                self.files.output("generator"),
                self.files.output("app-export"),
                self.files.output("app-faults"),
                self.files.output("results"),
            )
        )
        return self.result("PASS")

    def result(self, decision: Literal["PASS", "FAIL", "INTERRUPTED"]) -> RunResult:
        result = RunResult(
            run_id=self.manifest.run_id,
            manifest_sha256=manifest_context(self.manifest).manifest_sha256,
            decision=decision,
            completed_rounds=self.completed_rounds,
            artifacts=tuple(self.artifacts),
            finished_at=self.clock.now(),
        )
        write_canonical_artifact(self.files.output("results"), "result.json", result)
        return result
