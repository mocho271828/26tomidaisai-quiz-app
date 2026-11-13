import { createReadStream } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createInterface } from "node:readline";
import { resolve } from "node:path";
import { z } from "zod";
import { distribution } from "./analyze.js";
import { controlManifestSchema, controlSettingsSchema, type ControlManifest } from "./controlPlan.js";
import { connectionMetricNames, csvCell, metricCsv, resourceMetricNames, summarizeResourceMetrics, type MetricSample } from "./controlReportMetrics.js";
import { manualParticipantStatusSchema } from "./manualProtocol.js";

const eventSchema = z.object({event: z.string(), sequence: z.number().int().nonnegative(),
  wallMs: z.number().finite(), serial: z.number().int().optional(), round: z.number().int().optional(),
  durationMs: z.number().finite().optional(), count: z.number().finite().optional(),
  reason: z.string().optional(), commandId: z.string().optional(), stage: z.string().optional(),
  phase: z.enum(["setup", "measurement", "shutdown"]).optional(),
  plannedAtMs: z.number().finite().optional(), actualAtMs: z.number().finite().optional(),
  operationLagMs: z.number().finite().optional(),
  browserMonotonicMs: z.number().finite().optional(), browserPlannedMonotonicMs: z.number().finite().optional(),
  browserOperationLagMs: z.number().finite().optional(),
  telemetry: z.object({kind: z.string(), phase: z.string().optional(), round_number: z.number().optional()}).optional()});
const nonnegative = z.number().finite().nonnegative();
const healthSchema = z.object({state: z.enum(["open", "closing", "closed", "failed"]), complete: z.boolean(),
  attemptedRecords: nonnegative.int(), acceptedRecords: nonnegative.int(), writtenRecords: nonnegative.int(),
  droppedRecords: nonnegative.int(), failedRecords: nonnegative.int(), pendingRecords: nonnegative.int(),
  pendingBytes: nonnegative.int(), peakPendingRecords: nonnegative.int(), peakPendingBytes: nonnegative.int(),
  batches: nonnegative.int(), writeMs: nonnegative, serializationMs: nonnegative,
  queueWaitTotalMs: nonnegative, queueWaitMaxMs: nonnegative,
  firstLossMonotonicMs: nonnegative.nullable(), lastLossMonotonicMs: nonnegative.nullable(),
  failure: z.literal("evidence_write_failed").nullable(), measurementLevel: z.enum(["minimal", "normal"]),
  lastWrittenSequence: nonnegative.int().nullable(), lossClock: z.literal("process_monotonic_ms")});
type RoundSets = {clicked: Set<number>; sending: Set<number>; accepted: Set<number>; results: Set<number>; answeredResults: Set<number>};
type Operation = {source: string; serial: number; commandId: string; operation: string; round: number | null;
  plannedAtMs: number | null; actualAtMs: number | null; lagMs: number | null;
  browserPlannedMonotonicMs: number | null; browserMonotonicMs: number | null; browserLagMs: number | null};
const errorEvents = new Set(["manual_observation_interrupted", "actor_failed", "manual_operation_failed", "page_error", "page_crash", "http_error",
  "request_failed", "invalid_ui_observation", "browser_probe_flush_failed", "answer_latency_sample_missing",
  "browser_probe_overflow", "browser_probe_delivery_failure", "invalid_browser_probe", "http_observation_failed",
  "manual_stop_guard_failed"]);
const observationLossEvents = new Set(["manual_observation_interrupted", "invalid_ui_observation", "browser_probe_flush_failed", "answer_latency_sample_missing",
  "browser_probe_overflow", "browser_probe_delivery_failure", "invalid_browser_probe", "http_observation_failed"]);
const participantFailureEvents = new Set(["actor_failed", "manual_operation_failed", "page_error", "page_crash", "http_error"]);
const summarySchema = z.object({status: z.literal("manual_stopped"), planned: nonnegative.int(),
  participants: z.array(manualParticipantStatusSchema)}).strict();
const terminationSchema = z.object({reason: z.string(), expectedWorkers: nonnegative.int(),
  workers: z.array(z.object({workerId: z.string(), exitCode: z.number().int().nullable(), clean: z.boolean()}).strict())}).strict();

export async function generateControlReport(directory: string, suppliedManifest: ControlManifest, reason: string, outputDirectory = directory) {
  const manifest = controlManifestSchema.parse(suppliedManifest);
  if (resolve(outputDirectory) !== resolve(directory)) await mkdir(outputDirectory, {mode: 0o700});
  const rounds = Array.from({length: manifest.expectedRounds}, (): RoundSets => ({clicked: new Set(), sending: new Set(), accepted: new Set(),
    results: new Set(), answeredResults: new Set()}));
  const registered = new Set<number>();
  const prepared = new Set<number>();
  const everConnected = new Set<number>();
  const failed = new Set<number>();
  const unavailableSources: string[] = [];
  const incompleteSources: string[] = [];
  const health: Record<string, z.infer<typeof healthSchema>> = {};
  const latencies = new Map<string, {serial: number; round: number; durationMs: number}>();
  const operations: Operation[] = [];
  const resources: MetricSample[] = [];
  const connections: MetricSample[] = [];
  const eventErrors: Record<string, number> = {};
  let timeouts = 0;
  let disconnects = 0;
  let shutdownDisconnects = 0;
  let reconnects = 0;
  let invalidRecords = 0;
  let rejectedCommands = 0;
  let skippedOperations = 0;
  let acceptedCommands = 0;
  let measurementStartedAt: number | null = null;
  let expectedOperatorConnections: number | null = null;
  let hostIntervalMs: number | null = null;
  let termination: z.infer<typeof terminationSchema> | null = null;
  let terminationVerified = false;
  try {
    termination = terminationSchema.parse(JSON.parse(await readFile(resolve(directory, "termination.json"), "utf8")) as unknown);
    terminationVerified = termination.expectedWorkers === manifest.workers.length &&
      termination.workers.length === manifest.workers.length &&
      new Set(termination.workers.map(worker => worker.workerId)).size === manifest.workers.length &&
      termination.workers.every(worker => worker.clean && worker.exitCode === 0 && manifest.workers.some(plan => plan.workerId === worker.workerId));
    if (!terminationVerified) incompleteSources.push("termination");
  } catch { incompleteSources.push("termination"); }
  try {
    const settings = controlSettingsSchema.parse(JSON.parse(await readFile(resolve(directory, "control.json"), "utf8")) as unknown);
    if (settings.run.runId !== manifest.runId || settings.expectedRounds !== manifest.expectedRounds)
      throw new Error("control_settings_mismatch");
    expectedOperatorConnections = settings.expectedOperatorConnections;
    hostIntervalMs = settings.hostSampleIntervalMs;
  } catch { incompleteSources.push("control_settings"); }
  const sources = [{workerId: "controller", ordinalStart: manifest.workers[0]!.ordinalStart, count: 0}, ...manifest.workers];
  for (const worker of sources) {
    const isController = worker.workerId === "controller";
    const base = resolve(directory, `${manifest.runId}-${isController ? "control" : "participants"}-${worker.ordinalStart}`);
    let shutdown = false;
    let sequence = 0;
    let validRows = 0;
    try {
      const lines = createInterface({input: createReadStream(resolve(base, "events.jsonl")), crlfDelay: Infinity});
      for await (const line of lines) {
        let event: z.infer<typeof eventSchema>;
        try { event = eventSchema.parse(JSON.parse(line) as unknown); }
        catch { invalidRecords++; continue; }
        validRows++;
        if (event.sequence !== sequence) invalidRecords++;
        sequence = event.sequence + 1;
        if (event.event === (isController ? "control_stopped" : "participant_shutdown_finished")) shutdown = true;
        if (isController && event.event === "control_ready") measurementStartedAt = event.wallMs;
        if (resourceMetricNames.has(event.event)) resources.push({source: worker.workerId, event: event.event,
          wallMs: event.wallMs, value: event.durationMs ?? event.count ?? null, reason: event.reason});
        if (isController && connectionMetricNames.has(event.event)) connections.push({
          source: event.event === "worker_status" ? event.stage ?? "unknown" : "all_workers",
          event: event.event, wallMs: event.wallMs, value: event.count ?? null, reason: event.reason});
        if (errorEvents.has(event.event)) eventErrors[event.event] = (eventErrors[event.event] ?? 0) + 1;
        const serial = event.serial;
        if (serial === undefined) continue;
        if (isController || serial < worker.ordinalStart || serial >= worker.ordinalStart + worker.count) { invalidRecords++; continue; }
        if (event.event === "manual_prepared") prepared.add(serial);
        if (event.event === "joined") registered.add(serial);
        if (participantFailureEvents.has(event.event) && event.phase !== "shutdown") failed.add(serial);
        if (event.reason === "timeout") timeouts++;
        if (event.event === "ws_disconnected") {
          if (event.phase === "shutdown") shutdownDisconnects++;
          else disconnects++;
        }
        if (event.event === "manual_command_rejected") rejectedCommands++;
        if (event.event === "manual_command_accepted") acceptedCommands++;
        if (event.event === "manual_operation_skipped") skippedOperations++;
        const telemetry = event.telemetry;
        if (telemetry?.kind === "ws" && telemetry.phase === "open") {
          if (everConnected.has(serial)) reconnects++;
          everConnected.add(serial);
        }
        if (event.event === "manual_join_clicked" || event.event === "manual_answer_clicked") operations.push({
          source: worker.workerId, serial, commandId: event.commandId ?? "", operation: event.event === "manual_join_clicked" ? "join" : "answer",
          round: event.round === 0 ? null : event.round ?? null, plannedAtMs: event.plannedAtMs ?? null,
          actualAtMs: event.actualAtMs ?? null, lagMs: event.operationLagMs ?? null,
          browserPlannedMonotonicMs: event.browserPlannedMonotonicMs ?? null,
          browserMonotonicMs: event.browserMonotonicMs ?? null, browserLagMs: event.browserOperationLagMs ?? null});
        const round = event.round ?? telemetry?.round_number;
        if (round === undefined || round < 1 || round > manifest.expectedRounds || !Number.isInteger(round)) continue;
        const sets = rounds[round - 1]!;
        if (event.event === "manual_answer_clicked") sets.clicked.add(serial);
        if (event.event === "accepted_dom") sets.accepted.add(serial);
        if (event.event === "result_dom") {
          sets.results.add(serial);
          if (event.reason === "answered") sets.answeredResults.add(serial);
        }
        if (telemetry?.kind === "answer" && telemetry.phase === "sending") sets.sending.add(serial);
        if (event.event === "answer_send_to_accepted_dom" && event.durationMs !== undefined) {
          const key = `${serial}:${round}`;
          if (event.durationMs < 0 || latencies.has(key)) invalidRecords++;
          else latencies.set(key, {serial, round, durationMs: event.durationMs});
        }
      }
    } catch { unavailableSources.push(worker.workerId); }
    if (!shutdown) incompleteSources.push(worker.workerId);
    try {
      const parsed = healthSchema.parse(JSON.parse(await readFile(resolve(base, "evidence-health.json"), "utf8")) as unknown);
      health[worker.workerId] = parsed;
      if (!parsed.complete || parsed.state !== "closed" || parsed.droppedRecords > 0 || parsed.failedRecords > 0 ||
        parsed.pendingRecords > 0 || parsed.failure !== null || parsed.writtenRecords !== validRows ||
        parsed.attemptedRecords !== parsed.writtenRecords) incompleteSources.push(worker.workerId);
    } catch { incompleteSources.push(worker.workerId); }
    if (!isController) {
      try {
        const summary = summarySchema.parse(JSON.parse(await readFile(resolve(base, "manual-summary.json"), "utf8")) as unknown);
        if (summary.planned !== worker.count || summary.participants.length !== worker.count ||
          new Set(summary.participants.map(participant => participant.serial)).size !== worker.count)
          incompleteSources.push(worker.workerId);
        for (const participant of summary.participants) {
          if (participant.serial < worker.ordinalStart || participant.serial >= worker.ordinalStart + worker.count) invalidRecords++;
          else if (participant.failed) failed.add(participant.serial);
        }
      } catch { incompleteSources.push(worker.workerId); }
    }
  }
  const latencySamples = [...latencies.values()];
  const perRound = rounds.map((row, index) => ({round: index + 1, expected: manifest.totalVus,
    clicked: row.clicked.size, sending: row.sending.size, accepted: row.accepted.size, results: row.results.size,
    answeredResults: row.answeredResults.size, unanswered: manifest.totalVus - row.accepted.size,
    incomplete: manifest.totalVus - row.results.size,
    sendingToAcceptedDomMs: distribution(latencySamples.filter(sample => sample.round === index + 1).map(sample => sample.durationMs))}));
  const completeParticipants = [...registered].filter(serial => !failed.has(serial) &&
    rounds.every(round => round.clicked.has(serial) && round.accepted.has(serial) && round.answeredResults.has(serial))).length;
  const globalConnections = connections.filter(sample => sample.event === "simultaneous_ws" &&
    measurementStartedAt !== null && sample.wallMs >= measurementStartedAt);
  const peakSimultaneousConnections = globalConnections.reduce<number | null>((peak, sample) =>
    sample.value === null ? peak : peak === null ? sample.value : Math.max(peak, sample.value), null);
  const missingConnectionSnapshots = globalConnections.filter(sample => sample.value === null).length;
  const sampling = summarizeResourceMetrics(resources, hostIntervalMs);
  const observationLosses = [...observationLossEvents].reduce((sum, event) => sum + (eventErrors[event] ?? 0), 0);
  const measurementComplete = unavailableSources.length === 0 && incompleteSources.length === 0 && invalidRecords === 0 &&
    observationLosses === 0 &&
    missingConnectionSnapshots === 0 && globalConnections.length > 0 && sampling.host.sampleCount > 0 &&
    sampling.host.unavailableSamples === 0 && sampling.host.largeIntervalGaps === 0;
  const normalTermination = terminationVerified && ["requested_stop", "signal", "input_closed"].includes(termination?.reason ?? "");
  const report = {format: "browser-control-report-v1", runId: manifest.runId, stopReason: termination?.reason ?? reason,
    reportReason: reason, terminationVerified, normalTermination,
    plannedVus: manifest.totalVus, expectedRounds: manifest.expectedRounds, expectedPairs: manifest.totalVus * manifest.expectedRounds,
    manualParticipants: {planned: manifest.manualParticipantCount, observed: null},
    operatorConnections: {planned: expectedOperatorConnections, observed: null},
    prepared: prepared.size, registered: registered.size, neverRegistered: manifest.totalVus - registered.size,
    neverConnected: manifest.totalVus - everConnected.size, peakSimultaneousConnections,
    missingConnectionSnapshots, connectionSnapshotCount: globalConnections.length,
    connectionWindowStartedAt: measurementStartedAt,
    failedParticipants: failed.size, unfinishedParticipants: manifest.totalVus - completeParticipants,
    timeouts, disconnects, shutdownDisconnects, reconnects, errorEvents: eventErrors, rounds: perRound, measurementComplete,
    commands: {acceptedPerVu: acceptedCommands, rejectedPerVu: rejectedCommands, skippedOperations},
    allRoundsBrowserComplete: measurementComplete && normalTermination && failed.size === 0 && completeParticipants === manifest.totalVus,
    databaseMembershipVerified: false, sendingToAcceptedDomMs: distribution(latencySamples.map(sample => sample.durationMs)),
    operationDriftMs: distribution(operations.flatMap(sample => sample.lagMs === null ? [] : [sample.lagMs])),
    browserAnswerDriftMs: distribution(operations.flatMap(sample => sample.operation !== "answer" || sample.browserLagMs === null ? [] : [sample.browserLagMs])),
    unavailableWorkers: unavailableSources.filter(source => source !== "controller"),
    incompleteWorkers: [...new Set(incompleteSources)].filter(source => manifest.workers.some(worker => worker.workerId === source)),
    unavailableSources, incompleteSources: [...new Set(incompleteSources)], invalidRecords, recordingHealth: health, sampling,
    scope: "受付時間は同一browser monotonicの送信→可視受付DOM。browserAnswerDriftMsはwhen-openの同一browser予定→DOMクリック。operationDriftMsは参加操作/now/旧証跡のNode予定→クリック完了観測であり、両者を混ぜない。実描画/マウス操作の遅延ではない。接続peakは同一supervisor標本の最大で、別時刻worker peakを合算しない。手動参加者/運営の実数・server内部・DB保存は未検証。途中停止と予定全問完了は別判定。"};
  const save = (name: string, value: string): Promise<void> => writeFile(resolve(outputDirectory, name), value, {flag: "wx", mode: 0o600});
  await save("rounds.csv", "round,expected,clicked,sending,accepted,results,answered_results,unanswered,incomplete,latency_samples,latency_p50_ms,latency_p95_ms,latency_max_ms\n" +
    perRound.map(row => [row.round, row.expected, row.clicked, row.sending, row.accepted, row.results, row.answeredResults,
      row.unanswered, row.incomplete, row.sendingToAcceptedDomMs.count, row.sendingToAcceptedDomMs.p50,
      row.sendingToAcceptedDomMs.p95, row.sendingToAcceptedDomMs.max].map(csvCell).join(",")).join("\n") + "\n");
  await save("latencies.csv", "serial,round,sending_to_accepted_dom_ms\n" + latencySamples
    .map(sample => `${sample.serial},${sample.round},${sample.durationMs}`).join("\n") + "\n");
  await save("operations.csv", "worker,serial,command_id,operation,round,planned_node_wall_ms,observed_click_completion_node_wall_ms,lag_ms,planned_browser_monotonic_ms,clicked_browser_monotonic_ms,browser_lag_ms\n" +
    operations.map(sample => [sample.source, sample.serial, sample.commandId, sample.operation, sample.round,
      sample.plannedAtMs, sample.actualAtMs, sample.lagMs, sample.browserPlannedMonotonicMs,
      sample.browserMonotonicMs, sample.browserLagMs].map(csvCell).join(",")).join("\n") + "\n");
  await save("resources.csv", metricCsv(resources));
  await save("connections.csv", metricCsv(connections));
  await save("report.json", JSON.stringify(report, null, 2) + "\n");
  return report;
}

async function main(): Promise<void> {
  const directory = process.argv[2];
  if (!directory) throw new Error("control_directory_required");
  const manifest = controlManifestSchema.parse(JSON.parse(await readFile(resolve(directory, "manifest.json"), "utf8")) as unknown);
  await generateControlReport(directory, manifest, "offline_recovery", process.argv[3] ?? directory);
}
if (process.argv[1]?.endsWith("controlReport.js")) void main().catch(() => {
  process.stderr.write("control_report_failed; 入力と未使用の出力先を確認してください\n");
  process.exitCode = 1;
});
