import { readFile } from "node:fs/promises";
import { chromium, type Browser } from "playwright";
import { controlRunConfigSchema } from "./config.js";
import { Evidence, startWorkerSampling } from "./evidence.js";
import { RunLifecycle } from "./lifecycle.js";
import { createManualParticipant, type ManualParticipant } from "./manualParticipant.js";
import { emptyCounts, parentMessageSchema, preparationFailureSchema, workerInputSchema, type WorkerCounts } from "./controlIpc.js";
import { flushBrowserDiagnostics } from "./browserDiagnostics.js";
import { verifyIsolation } from "./participant.js";

export function countParticipants(participants: ManualParticipant[], expectedRounds = 6): WorkerCounts {
  const counts = emptyCounts(expectedRounds);
  for (const participant of participants) {
    const status = participant.status();
    for (const key of ["ready", "joined", "connected", "paused", "failed"] as const) if (status[key]) counts[key]++;
    if (status.complete) counts.completed++;
    for (const key of ["failures", "timeouts", "disconnects", "reconnects"] as const) counts[key] += status[key];
    for (const row of counts.rounds) {
      if (status.clicked.includes(row.round)) row.clicked++;
      if (status.accepted.includes(row.round)) row.accepted++;
      if (status.results.includes(row.round)) row.results++;
    }
  }
  return counts;
}

async function run(): Promise<void> {
  const path = process.argv[2];
  if (!path || !process.send) throw new Error("worker_requires_local_ipc");
  const input = workerInputSchema.parse(JSON.parse(await readFile(path, "utf8")) as unknown);
  const {settings, worker} = input;
  const config = controlRunConfigSchema.parse({...settings.run, workerId: worker.workerId, count: worker.count, ordinalStart: worker.ordinalStart});
  const evidence = new Evidence(config, "participants", {measurementLevel: settings.measurementLevel});
  const lifecycle = new RunLifecycle(evidence, "participants");
  evidence.event({event: "participant_run_started", phase: "setup", lifecycleVersion: 2,
    cohort: {count: worker.count, ordinalStart: worker.ordinalStart}});
  const stopSampling = startWorkerSampling(evidence, {intervalMs: settings.hostSampleIntervalMs, measurementLevel: settings.measurementLevel});
  const participants: ManualParticipant[] = [];
  let browser: Browser | undefined;
  let finish!: () => void;
  let stopping = false;
  let clean = true;
  const stopped = new Promise<void>(resolve => { finish = resolve; });
  const stop = (): void => { if (stopping) return; stopping = true; finish(); };
  let sending = false;
  let activeDispatches = 0;
  const publish = (): void => {
    if (!process.connected || sending) return;
    sending = true;
    const counts = countParticipants(participants, settings.expectedRounds);
    counts.commandReceipts = evidence.count("manual_command_accepted");
    counts.commandRejections = evidence.count("manual_command_rejected");
    process.send?.({type: "status", workerId: worker.workerId, counts}, error => {
      sending = false;
      if (error) { clean = false; stop(); }
    });
  };
  const statusTimer = setInterval(publish, 1000);
  const deadline = input.deadlineAtMs === null ? undefined :
    setTimeout(() => { clean = false; stop(); }, Math.max(1, input.deadlineAtMs - Date.now()));
  process.once("SIGINT", stop); process.once("SIGTERM", stop); process.once("disconnect", stop);
  process.on("message", (raw: unknown) => {
    const parsed = parentMessageSchema.safeParse(raw);
    if (!parsed.success) { clean = false; stop(); return; }
    const command = parsed.data.command;
    if (command.runId !== config.runId || command.target.ordinalStart !== input.firstOrdinal || command.target.count !== input.totalVus) {
      clean = false; stop(); return;
    }
    if (command.kind === "stop") { stop(); return; }
    if (stopping) return;
    // command受付は同期に行い、各VUの予約/実行を次のpause/stopの受信で塞がない。
    if (activeDispatches >= 4) { evidence.event({event: "command_dispatch_overflow"}); clean = false; stop(); return; }
    activeDispatches++;
    void Promise.all(participants.map(participant => participant.dispatch(command)))
      .catch(() => { clean = false; stop(); })
      .finally(() => { activeDispatches--; });
  });
  try {
    browser = await chromium.launch({headless: config.headless, timeout: 90000,
      ...(config.channel === "chrome" ? {channel: "chrome"} : {}),
      args: ["--disable-background-timer-throttling", "--disable-renderer-backgrounding"]});
    browser.on("disconnected", () => { if (!stopping) { clean = false; stop(); } });
    evidence.save("browser.json", {version: browser.version()});
    // 独立Contextのフォント転送を重ねるとLAN上の初回API取得が詰まるため、読込完了まで次のVUを作らない。
    for (let offset = 0; offset < worker.count && !stopping; offset++) {
      const serial = worker.ordinalStart + offset;
      const participant = await createManualParticipant(browser, serial, config, evidence, {
        expectedRounds: settings.expectedRounds,
        operationTimeoutMs: settings.operationTimeoutMs, deadlineAtMs: input.deadlineAtMs, lifecycle,
        detailedDiagnostics: settings.measurementLevel === "normal" && serial - input.firstOrdinal < settings.detailedVuCount});
      participants.push(participant);
      if (stopping) break;
      // 準備待ちを抜けてfinallyのstopへ進み、段階ごとのtimeoutが終了猶予に積み上がるのを防ぐ。
      await Promise.race([participant.prepare(), stopped]);
      if (participants.some(participant => participant.status().failed)) { clean = false; stop(); }
    }
    if (!stopping) { lifecycle.startMeasurement(); publish(); await stopped; }
  } catch { clean = false; stop(); }
  finally {
    stopping = true;
    clearInterval(statusTimer); clearTimeout(deadline);
    if (!clean && process.connected) {
      // 自由な例外本文をIPCへ渡さず、準備段階の固定語彙だけを抽出する。
      const preparationFailure = participants.flatMap(participant => participant.actor.failures
        .filter(failure => failure.round === 0)
        .map(failure => preparationFailureSchema.safeParse({serial: participant.actor.serial, stage: failure.stage})))
        .find(result => result.success)?.data;
      process.send?.({type: "failed", workerId: worker.workerId, preparationFailure});
    }
    const stoppingParticipants = participants.map(participant => participant.stop());
    await Promise.allSettled(stoppingParticipants);
    if (evidence.count("manual_observation_interrupted") > 0) clean = false;
    try {
      const joined = participants.filter(participant => participant.actor.joined);
      if (joined.length && !await verifyIsolation(joined.map(participant => participant.actor), evidence)) clean = false;
      for (const participant of participants) {
        if (settings.measurementLevel === "normal" && participant.actor.serial - input.firstOrdinal < settings.detailedVuCount)
          await flushBrowserDiagnostics(participant.actor.page);
      }
    } catch { evidence.event({event: "browser_probe_flush_failed"}); clean = false; }
    lifecycle.finish(clean ? "completed" : "run_failed");
    try { await browser?.close(); } catch { evidence.event({event: "browser_close_failed", phase: "shutdown"}); clean = false; }
    evidence.save("manual-summary.json", {status: "manual_stopped", planned: worker.count,
      participants: participants.map(participant => participant.status())});
    stopSampling(); lifecycle.shutdownFinished(clean ? "completed" : "run_failed");
    try { await evidence.close(); } catch { clean = false; }
    clean = clean && evidence.health().complete;
    if (process.connected) await new Promise<void>(resolve => process.send?.({type: "finished", workerId: worker.workerId, clean}, () => resolve()));
    process.disconnect?.();
    process.exitCode = clean ? 0 : 1;
  }
}

if (process.argv[1]?.endsWith("controlWorker.js")) run().catch(() => {
  process.exitCode = 1;
  process.disconnect?.();
});
