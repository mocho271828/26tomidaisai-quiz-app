import { fork, type ChildProcess } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { Evidence, startResourceSampling, startWorkerSampling } from "./evidence.js";
import { parseControlCommand } from "./controlCommands.js";
import { createControlPlan, type ControlSettings, type WorkerPlan } from "./controlPlan.js";
import { workerMessageSchema, type WorkerCounts } from "./controlIpc.js";
import type { ManualCommand } from "./manualProtocol.js";
import { generateControlReport } from "./controlReport.js";
import { terminateWorkerTree } from "./controlTermination.js";
import { MAX_MANUAL_COMMANDS } from "./controlRounds.js";

type Child = {plan: WorkerPlan; process: ChildProcess; exited: Promise<void>; exitCode?: number | null;
  finished: boolean; counts?: WorkerCounts; updatedAt: number; sent: number};
const saveJson = (path: string, value: unknown): Promise<void> =>
  writeFile(path, JSON.stringify(value, null, 2) + "\n", {mode: 0o600, flag: "wx"});

/** 同一PC内のIPCのみを使い、共有filesystem heartbeatへ操作命令を混在させない。 */
export async function superviseControl(settings: ControlSettings, totalVus: number): Promise<string> {
  const manifest = createControlPlan(settings, totalVus);
  await mkdir(settings.run.outputDirectory, {recursive: true, mode: 0o700});
  const directory = resolve(settings.run.outputDirectory, `${manifest.runId}-control`);
  await mkdir(directory, {mode: 0o700});
  await saveJson(resolve(directory, "manifest.json"), manifest);
  const run = {...settings.run, outputDirectory: directory, startAt: new Date().toISOString()};
  settings = {...settings, run};
  await saveJson(resolve(directory, "control.json"), settings);
  const evidence = new Evidence({...run, count: 1}, "control", {measurementLevel: settings.measurementLevel});
  const stopHost = startResourceSampling(evidence, {intervalMs: settings.hostSampleIntervalMs});
  const stopWorker = startWorkerSampling(evidence, {intervalMs: settings.hostSampleIntervalMs, measurementLevel: settings.measurementLevel});
  const children: Child[] = [];
  let reason: string | undefined;
  let resolveStop!: () => void;
  const stopped = new Promise<void>(resolve => { resolveStop = resolve; });
  const stop = (next: string): void => { if (reason !== undefined) return; reason = next; resolveStop(); };
  const signal = (): void => stop("signal");
  process.once("SIGINT", signal);
  process.once("SIGTERM", signal);
  const input = createInterface({input: process.stdin, terminal: false});
  const send = (child: Child, command: ManualCommand): void => {
    if (!child.process.connected || child.sent >= 8) { stop("worker_ipc_unavailable"); return; }
    child.sent++;
    try {
      child.process.send({type: "command", command}, error => {
        child.sent--;
        if (error) stop("worker_ipc_failed");
      });
    } catch { child.sent--; stop("worker_ipc_failed"); }
  };
  let ready = false;
  let commandCount = 0;
  let deadline: ReturnType<typeof setTimeout> | undefined;
  let statusTimer: ReturnType<typeof setInterval> | undefined;
  const setupDeadline = setTimeout(() => stop("setup_timeout"), settings.setupTimeoutSeconds * 1000);
  const displayStatus = (): void => {
    const snapshot = children.map(child => ({workerId: child.plan.workerId, expected: child.plan.count,
      ageMs: Math.round(performance.now() - child.updatedAt), stale: !child.counts || performance.now() - child.updatedAt > 2500,
      counts: performance.now() - child.updatedAt > 2500 ? null : child.counts ?? null}));
    process.stdout.write(JSON.stringify({ready, plannedVus: totalVus, expectedRounds: manifest.expectedRounds,
      manualParticipants: {planned: settings.manualParticipantCount, observed: null},
      operatorConnections: {planned: settings.expectedOperatorConnections, observed: null}, workers: snapshot}) + "\n");
  };
  input.on("line", line => {
    if (reason !== undefined || line.trim() === "") return;
    try {
      if (line.length > 512) throw new Error("command_limit");
      const command = parseControlCommand(line, manifest);
      if (command === "status") { displayStatus(); return; }
      if (command.kind === "stop") { stop("requested_stop"); return; }
      if (commandCount >= MAX_MANUAL_COMMANDS) throw new Error("command_limit");
      if (!ready) { process.stdout.write("準備中です。status または stop を使用してください。\n"); return; }
      commandCount++;
      evidence.event({event: "command_dispatched", commandId: command.commandId, stage: command.kind,
        round: command.kind === "answer" ? command.round : undefined, plannedAtMs: command.issuedAtMs,
        count: command.target.count, targetOrdinalStart: command.target.ordinalStart,
        spreadMs: command.kind === "join" || command.kind === "answer" ? command.spreadMs : undefined,
        delayMs: command.kind === "answer" ? command.delayMs : undefined,
        mode: command.kind === "answer" ? command.mode : undefined, scope: "supervisor"});
      children.forEach(child => send(child, command));
      process.stdout.write(`命令配布 ${command.kind} ${command.commandId}（クリック・受付とは別）\n`);
    } catch { process.stdout.write("コマンド不正: join [--spread 30s], status, answer --round 1 --now|--when-open [--delay 500ms] [--spread 2s], pause, resume, stop\n"); }
  });
  input.once("close", () => stop("input_closed"));
  try {
    for (const worker of manifest.workers) {
      if (reason !== undefined) break;
      const path = resolve(directory, `${worker.workerId}.json`);
      await saveJson(path, {settings, worker, totalVus, firstOrdinal: run.ordinalStart,
        deadlineAtMs: run.durationSeconds === null ? null : Date.now() + (settings.setupTimeoutSeconds + run.durationSeconds) * 1000});
      const childProcess = fork(fileURLToPath(new URL("./controlWorker.js", import.meta.url)), [path],
        {stdio: ["ignore", "ignore", "ignore", "ipc"], detached: false});
      let resolveExit!: () => void;
      const child: Child = {plan: worker, process: childProcess, exited: new Promise(resolve => { resolveExit = resolve; }),
        finished: false, updatedAt: performance.now(), sent: 0};
      children.push(child);
      childProcess.once("error", () => { stop("worker_spawn_failed"); resolveExit(); });
      childProcess.once("exit", code => {
        child.exitCode = code;
        if (reason === undefined) stop("worker_unexpected_exit");
        resolveExit();
      });
      childProcess.on("message", (raw: unknown) => {
        const parsed = workerMessageSchema.safeParse(raw);
        if (!parsed.success || parsed.data.workerId !== worker.workerId) { stop("worker_invalid_message"); return; }
        child.updatedAt = performance.now();
        const message = parsed.data;
        if (message.type === "failed") {
          const failure = message.preparationFailure;
          if (failure) {
            if (failure.serial < worker.ordinalStart || failure.serial >= worker.ordinalStart + worker.count) {
              stop("worker_invalid_message"); return;
            }
            process.stderr.write(`準備失敗: ${worker.workerId} / VU ${failure.serial} / ${failure.stage}\n`);
          }
          stop("worker_failed"); return;
        }
        if (message.type === "finished") { child.finished = message.clean; return; }
        if (message.counts.rounds.length !== manifest.expectedRounds) { stop("worker_invalid_counts"); return; }
        if ([message.counts.ready, message.counts.joined, message.counts.connected, message.counts.completed,
          message.counts.failed, message.counts.paused, ...message.counts.rounds.flatMap(row => [row.clicked, row.accepted, row.results])]
          .some(count => count > worker.count)) { stop("worker_invalid_counts"); return; }
        child.counts = message.counts;
        if (!ready && children.length === manifest.workers.length && children.every(item => item.counts?.ready === item.plan.count)) {
          ready = true;
          clearTimeout(setupDeadline);
          if (run.durationSeconds !== null) deadline = setTimeout(() => stop("run_deadline"), run.durationSeconds * 1000);
          evidence.event({event: "control_ready", count: totalVus});
          const duration = run.durationSeconds === null ? "stopまで継続（AWS環境は停止しません）" : `制御期限 ${run.durationSeconds}秒`;
          process.stdout.write(`準備完了 ${totalVus} VU / ${children.length} workers。joinで参加します。${duration}。\n`);
        }
      });
    }
    statusTimer = setInterval(() => {
      const now = performance.now();
      evidence.event({event: "planned_vus", count: totalVus, scope: "supervisor"});
      const stale = children.filter(child => !child.counts || now - child.updatedAt > 2500);
      if (children.some(child => now - child.updatedAt > 60000)) stop("worker_heartbeat_timeout");
      for (const child of children) {
        evidence.event({event: "worker_status", stage: child.plan.workerId, count: child.counts && now - child.updatedAt <= 2500 ? child.counts.connected : undefined,
          reason: !child.counts || now - child.updatedAt > 2500 ? "worker_snapshot_missing" : undefined,
          durationMs: now - child.updatedAt, scope: "supervisor"});
      }
      const sum = (key: "ready" | "joined" | "connected"): number => children.reduce((total, child) => total + (child.counts?.[key] ?? 0), 0);
      for (const [event, key] of [["simultaneous_ws", "connected"], ["simultaneous_joined", "joined"], ["simultaneous_ready", "ready"]] as const)
        evidence.event({event, count: stale.length === 0 ? sum(key) : undefined,
          reason: stale.length ? "worker_snapshot_missing" : undefined, scope: "supervisor"});
    }, 1000);
    process.stdout.write(`起動中 ${totalVus} VU。証跡: ${directory}\n`);
    await stopped;
  } catch { stop("supervisor_failed"); }
  finally {
    clearTimeout(setupDeadline); clearTimeout(deadline); clearInterval(statusTimer);
    input.close();
    const command = parseControlCommand("stop", manifest);
    if (command !== "status") for (const child of children) if (child.process.connected) send(child, command);
    let grace: ReturnType<typeof setTimeout> | undefined;
    const allExited = Promise.all(children.map(child => child.exited));
    await Promise.race([allExited, new Promise<void>(resolve => { grace = setTimeout(resolve, 45000); })]);
    clearTimeout(grace);
    await Promise.all(children.filter(child => child.exitCode === undefined).map(async child => {
      evidence.event({event: "worker_forced_stop", stage: child.plan.workerId, scope: "supervisor"});
      try { await terminateWorkerTree(child.process); }
      catch { reason = "worker_cleanup_failed"; child.process.kill("SIGKILL"); }
    }));
    let finalTimer: ReturnType<typeof setTimeout> | undefined;
    await Promise.race([allExited, new Promise<void>(resolve => { finalTimer = setTimeout(resolve, 9000); })]);
    clearTimeout(finalTimer);
    for (const child of children) if (child.exitCode === undefined) {
      reason = "worker_cleanup_failed";
      child.process.unref();
      if (child.process.connected) child.process.disconnect();
    }
    await stopHost(); stopWorker();
    evidence.event({event: "control_stopped", reason: reason ?? "supervisor_failed"});
    try { await evidence.close(); if (!evidence.health().complete) reason = "recording_failed"; }
    catch { reason = "recording_failed"; }
    await saveJson(resolve(directory, "termination.json"), {reason, workers: children.map(child => ({workerId: child.plan.workerId,
      exitCode: child.exitCode ?? null, clean: child.finished})), expectedWorkers: manifest.workers.length});
    process.removeListener("SIGINT", signal); process.removeListener("SIGTERM", signal);
  }
  await generateControlReport(directory, manifest, reason ?? "supervisor_failed");
  if (!["requested_stop", "signal", "input_closed"].includes(reason ?? "") || children.some(child => !child.finished || child.exitCode !== 0))
    process.exitCode = 1;
  return directory;
}
