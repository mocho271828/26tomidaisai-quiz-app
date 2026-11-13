import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, expect, it } from "vitest";
import { controlSettingsSchema, createControlPlan } from "../src/controlPlan.js";
import { generateControlReport } from "../src/controlReport.js";
import { Evidence, type SafeEvent } from "../src/evidence.js";

const directories: string[] = [];
afterEach(async () => { await Promise.all(directories.splice(0).map(directory => rm(directory, {recursive: true}))); });

async function fixture(expectedRounds = 6) {
  const directory = await mkdtemp(join(tmpdir(), "quiz-control-report-"));
  directories.push(directory);
  const settings = controlSettingsSchema.parse({expectedRounds, workerSize: 1, manualParticipantCount: 1, expectedOperatorConnections: 2,
    run: {runId: "report-fixture", sourceCommit: "0".repeat(40), baseUrl: "https://127.0.0.1", count: 1,
      ordinalStart: 1, startAt: new Date().toISOString(), outputDirectory: directory,
      durationSeconds: 180, joinSpreadMs: 0, operatorStartDelaySeconds: 10, preScheduleMarginMs: 1000,
      resultHoldMs: 3000, answerDelayMs: 0, scenario: "normal", faultAfterSeconds: 5, faultDurationMs: 100,
      channel: "chromium", headless: true}});
  const manifest = createControlPlan(settings, 2);
  await writeFile(join(directory, "control.json"), JSON.stringify(settings));
  const controller = new Evidence(settings.run, "control");
  controller.event({event: "simultaneous_ws", reason: "worker_snapshot_missing"});
  controller.event({event: "control_ready", count: 2});
  controller.event({event: "host_sample_lag_ms", durationMs: 1, scope: "host"});
  controller.event({event: "generator_cpu_percent", durationMs: 25, scope: "host"});
  controller.event({event: "generator_memory_used_bytes", count: 123, scope: "host"});
  controller.event({event: "generator_swap_used_bytes", count: 0, scope: "host"});
  controller.event({event: "worker_status", stage: "worker-1", count: 1});
  controller.event({event: "worker_status", stage: "worker-2", count: 0});
  controller.event({event: "simultaneous_ws", count: 1});
  controller.event({event: "worker_status", stage: "worker-1", count: 0});
  controller.event({event: "worker_status", stage: "worker-2", count: 1});
  controller.event({event: "simultaneous_ws", count: 1});
  const makeWorker = (serial: number): Evidence => new Evidence({...settings.run, ordinalStart: serial}, "participants");
  const finish = async (evidence: Evidence, controller = false): Promise<void> => {
    if (controller) await writeFile(join(directory, "termination.json"), JSON.stringify({reason: "requested_stop",
      expectedWorkers: 2, workers: manifest.workers.map(worker => ({workerId: worker.workerId, exitCode: 0, clean: true}))}));
    else {
      const serial = Number(evidence.directory.split("-").at(-1));
      const rounds = Array.from({length: expectedRounds}, (_, index) => index + 1);
      evidence.save("manual-summary.json", {status: "manual_stopped", planned: 1, participants: [{serial,
        ready: true, joined: true, connected: false, paused: false, stopped: true, complete: evidence.count("accepted_dom") === expectedRounds,
        failed: false, currentRound: expectedRounds, clicked: evidence.count("manual_answer_clicked") === expectedRounds ? rounds : [],
        accepted: evidence.count("accepted_dom") === expectedRounds ? rounds : [], results: evidence.count("result_dom") === expectedRounds ? rounds : [],
        answeredResults: evidence.count("result_dom") === expectedRounds ? rounds : [], unansweredResults: [],
        failures: 0, timeouts: 0, disconnects: 0, reconnects: 0, pending: 0}]});
    }
    evidence.event({event: controller ? "control_stopped" : "participant_shutdown_finished"});
    await evidence.close();
  };
  return {directory, settings, manifest, controller, makeWorker, finish};
}

function completedWorker(evidence: Evidence, serial: number, durationMs: number, expectedRounds = 6): void {
  evidence.event({event: "manual_prepared", serial});
  evidence.event({event: "joined", serial});
  evidence.event({event: "ui_observation", serial, telemetry: {kind: "ws", audience: "participant", phase: "open",
    monotonic_ms: 0, wall_time_ms: 0}});
  for (let round = 1; round <= expectedRounds; round++) {
    evidence.event({event: "manual_answer_clicked", serial, round, commandId: `command-${round}`,
      plannedAtMs: 100, actualAtMs: 110, operationLagMs: 10});
    evidence.event({event: "ui_observation", serial, telemetry: {kind: "answer", phase: "sending", round_number: round,
      monotonic_ms: 100, wall_time_ms: 1000000}});
    evidence.event({event: "accepted_dom", serial, round});
    evidence.event({event: "answer_send_to_accepted_dom", serial, round, durationMs});
    evidence.event({event: "result_dom", serial, round, reason: "answered"});
  }
}

it.each([7, 10, 12])("reports all %i planned rounds with the original participant denominator", async expectedRounds => {
  const {directory, manifest, controller, makeWorker, finish} = await fixture(expectedRounds);
  for (const serial of [1, 2]) {
    const worker = makeWorker(serial);
    completedWorker(worker, serial, 20, expectedRounds);
    await finish(worker);
  }
  await finish(controller, true);
  const report = await generateControlReport(directory, manifest, "requested_stop");
  expect(report).toMatchObject({expectedRounds, expectedPairs: 2 * expectedRounds,
    measurementComplete: true, allRoundsBrowserComplete: true});
  expect(report.rounds).toHaveLength(expectedRounds);
  expect(report.rounds.at(-1)).toMatchObject({round: expectedRounds, expected: 2, accepted: 2, answeredResults: 2});
});

it("does not shrink a ten-round plan when the operator stops after seven rounds", async () => {
  const {directory, manifest, controller, makeWorker, finish} = await fixture(10);
  for (const serial of [1, 2]) {
    const worker = makeWorker(serial);
    completedWorker(worker, serial, 20, 7);
    await finish(worker);
  }
  await finish(controller, true);
  const report = await generateControlReport(directory, manifest, "requested_stop");
  expect(report).toMatchObject({expectedRounds: 10, expectedPairs: 20, normalTermination: true,
    measurementComplete: true, allRoundsBrowserComplete: false, unfinishedParticipants: 2});
  expect(report.rounds[6]).toMatchObject({accepted: 2, answeredResults: 2});
  expect(report.rounds[7]).toMatchObject({expected: 2, accepted: 0, incomplete: 2});
});

it("rejects a report plan that changes the configured round denominator", async () => {
  const {directory, manifest, controller, makeWorker, finish} = await fixture(10);
  for (const serial of [1, 2]) {
    const worker = makeWorker(serial);
    completedWorker(worker, serial, 20, 7);
    await finish(worker);
  }
  await finish(controller, true);
  const report = await generateControlReport(directory, {...manifest, expectedRounds: 7}, "requested_stop");
  expect(report).toMatchObject({measurementComplete: false, allRoundsBrowserComplete: false});
  expect(report.incompleteSources).toContain("control_settings");
});

it("pools browser latency samples and never sums worker peaks or mixes planned human counts", async () => {
  const {directory, manifest, controller, makeWorker, finish} = await fixture();
  for (const serial of [1, 2]) {
    const worker = makeWorker(serial);
    completedWorker(worker, serial, serial === 1 ? 10 : 1000);
    await finish(worker);
  }
  await finish(controller, true);
  const report = await generateControlReport(directory, manifest, "requested_stop");
  expect(report).toMatchObject({plannedVus: 2, expectedPairs: 12, registered: 2,
    manualParticipants: {planned: 1, observed: null}, operatorConnections: {planned: 2, observed: null},
    measurementComplete: true, allRoundsBrowserComplete: true, peakSimultaneousConnections: 1,
    sendingToAcceptedDomMs: {count: 12, p50: 10, p95: 1000, max: 1000}, operationDriftMs: {count: 12, p95: 10}});
  expect(report.rounds[0]).toMatchObject({expected: 2, accepted: 2, sendingToAcceptedDomMs: {p95: 1000}});
  expect(await readFile(join(directory, "operations.csv"), "utf8")).toContain("observed_click_completion_node_wall_ms");
  expect(await readFile(join(directory, "connections.csv"), "utf8")).toContain("worker_snapshot_missing");
  expect(report.sampling.host.cpuPercent.p95).toBe(25);
  await expect(generateControlReport(directory, manifest, "repeat")).rejects.toMatchObject({code: "EEXIST"});
  const recovered = await generateControlReport(directory, manifest, "offline_recovery", join(directory, "recovered"));
  expect(recovered.allRoundsBrowserComplete).toBe(true);
  expect(await readFile(join(directory, "recovered", "report.json"), "utf8")).toContain('"reportReason": "offline_recovery"');
});

it("reports long-running connection histories without exceeding the function argument limit", async () => {
  const {directory, manifest, controller, makeWorker, finish} = await fixture();
  for (const serial of [1, 2]) {
    const worker = makeWorker(serial);
    completedWorker(worker, serial, 10);
    await finish(worker);
  }
  await finish(controller, true);
  const eventPath = join(controller.directory, "events.jsonl");
  const existing = await readFile(eventPath, "utf8");
  const previousCount = existing.trim().split("\n").length;
  const samples = 150000;
  const startedAt = Date.now();
  const history = Array.from({length: samples}, (_, index) => JSON.stringify({sequence: previousCount + index,
    event: "simultaneous_ws", wallMs: startedAt + index * 1000, count: index === samples - 1 ? 2 : 1})).join("\n");
  await writeFile(eventPath, existing + history + "\n");
  const finalCount = previousCount + samples;
  await writeFile(join(controller.directory, "evidence-health.json"), JSON.stringify({...controller.health(),
    attemptedRecords: finalCount, acceptedRecords: finalCount, writtenRecords: finalCount, lastWrittenSequence: finalCount - 1}));
  const report = await generateControlReport(directory, manifest, "requested_stop");
  expect(report).toMatchObject({peakSimultaneousConnections: 2, connectionSnapshotCount: samples + 2,
    measurementComplete: true, allRoundsBrowserComplete: true});
}, 15000);

it("retains missing workers and intentionally unanswered rounds in the original denominator", async () => {
  const {directory, manifest, controller, makeWorker, finish} = await fixture();
  const worker = makeWorker(1);
  worker.event({event: "joined", serial: 1});
  worker.event({event: "result_dom", serial: 1, round: 1, reason: "unanswered"});
  await finish(worker);
  await finish(controller, true);
  const report = await generateControlReport(directory, manifest, "signal");
  expect(report).toMatchObject({plannedVus: 2, expectedPairs: 12, registered: 1, neverRegistered: 1,
    unfinishedParticipants: 2, unavailableWorkers: ["worker-2"], incompleteWorkers: ["worker-2"],
    measurementComplete: false, allRoundsBrowserComplete: false});
  expect(report.rounds[0]).toMatchObject({expected: 2, accepted: 0, results: 1, answeredResults: 0, unanswered: 2});
  expect(report.sendingToAcceptedDomMs).toEqual({count: 0, p50: null, p95: null, max: null});
});

it("keeps browser answer drift separate from Node completion and other browser clocks", async () => {
  const {directory, manifest, controller, makeWorker, finish} = await fixture();
  for (const serial of [1, 2]) {
    const worker = makeWorker(serial);
    const browserPlannedMonotonicMs = serial === 1 ? 100 : 900000;
    worker.event({event: "manual_join_clicked", serial, commandId: "join", round: 0,
      plannedAtMs: 1000000, actualAtMs: 1000050, operationLagMs: 50});
    worker.event({event: "manual_answer_clicked", serial, commandId: "answer", round: 1,
      actualAtMs: 2000000, browserPlannedMonotonicMs,
      browserMonotonicMs: browserPlannedMonotonicMs + serial * 5, browserOperationLagMs: serial * 5});
    await finish(worker);
  }
  await finish(controller, true);
  const report = await generateControlReport(directory, manifest, "requested_stop");
  expect(report.operationDriftMs).toEqual({count: 2, p50: 50, p95: 50, max: 50});
  expect(report.browserAnswerDriftMs).toEqual({count: 2, p50: 5, p95: 10, max: 10});
  const csv = await readFile(join(directory, "operations.csv"), "utf8");
  expect(csv).toContain("planned_browser_monotonic_ms,clicked_browser_monotonic_ms,browser_lag_ms");
  expect(csv).toContain('"worker-1",1,"answer","answer",1,,2000000,,100,105,5');
  expect(csv).toContain('"worker-2",2,"answer","answer",1,,2000000,,900000,900010,10');
});

it.each(["missing_receipt", "event_truncation", "stale_snapshot", "resource_failure", "probe_overflow", "forced_worker_stop", "interrupted_operation"])("cannot claim complete measurement with %s", async mode => {
  const {directory, manifest, controller, makeWorker, finish} = await fixture();
  for (const serial of [1, 2]) {
    const worker = makeWorker(serial);
    completedWorker(worker, serial, 10);
    if (serial === 1 && mode === "interrupted_operation") worker.event({event: "manual_observation_interrupted", serial, count: 1});
    if (serial === 1 && mode === "probe_overflow") worker.event({event: "browser_probe_overflow", serial, count: 2});
    await finish(worker);
    if (serial === 2 && mode === "missing_receipt") await rm(join(worker.directory, "evidence-health.json"));
    if (serial === 2 && mode === "event_truncation") await writeFile(join(worker.directory, "events.jsonl"), "{partial");
  }
  const defect: SafeEvent | undefined = mode === "stale_snapshot" ? {event: "simultaneous_ws", reason: "worker_snapshot_missing"} :
    mode === "resource_failure" ? {event: "resource_sample_failed", scope: "host"} : undefined;
  if (defect) controller.event(defect);
  await finish(controller, true);
  if (mode === "forced_worker_stop") await writeFile(join(directory, "termination.json"), JSON.stringify({reason: "worker_unexpected_exit",
    expectedWorkers: 2, workers: [{workerId: "worker-1", exitCode: 0, clean: true}, {workerId: "worker-2", exitCode: 1, clean: false}]}));
  const report = await generateControlReport(directory, manifest, "requested_stop");
  expect(report.measurementComplete).toBe(false);
  expect(report.allRoundsBrowserComplete).toBe(false);
});

it("rejects six-round success after a browser failure even when completion events exist", async () => {
  const {directory, manifest, controller, makeWorker, finish} = await fixture();
  for (const serial of [1, 2]) {
    const worker = makeWorker(serial);
    completedWorker(worker, serial, 10);
    if (serial === 1) worker.event({event: "page_error", serial, phase: "measurement"});
    await finish(worker);
  }
  await finish(controller, true);
  const report = await generateControlReport(directory, manifest, "requested_stop");
  expect(report.failedParticipants).toBe(1);
  expect(report.allRoundsBrowserComplete).toBe(false);
});

it("separates expected shutdown disconnections from active-session failures", async () => {
  const {directory, manifest, controller, makeWorker, finish} = await fixture();
  for (const serial of [1, 2]) {
    const worker = makeWorker(serial);
    completedWorker(worker, serial, 10);
    if (serial === 1) worker.event({event: "ws_disconnected", serial, phase: "measurement"});
    worker.event({event: "ws_disconnected", serial, phase: "shutdown"});
    await finish(worker);
  }
  await finish(controller, true);
  const report = await generateControlReport(directory, manifest, "requested_stop");
  expect(report).toMatchObject({disconnects: 1, shutdownDisconnects: 2});
});
