import { chromium, type Browser } from "playwright";
import { resolve } from "node:path";
import { loadConfig } from "./config.js";
import { Evidence, startWorkerSampling, startResourceSampling } from "./evidence.js";
import { flushBrowserDiagnostics } from "./browserDiagnostics.js";
import { createActor, runActor, verifyIsolation, type Actor } from "./participant.js";
import { runOperator } from "./operator.js";
import { cancellableDelay, fixedFailureReason, RunLifecycle } from "./lifecycle.js";
import { ParticipantCoordinator } from "./coordinator.js";
import { analyzeRequestFailureFile } from "./analyze.js";

async function main(): Promise<void> {
  const [role, path, credentials] = process.argv.slice(2);
  if ((role !== "participants" && role !== "operator") || !path || (role === "operator" && !credentials))
    throw new Error("usage: participants config.json | operator config.json credentials.json");
  const config = await loadConfig(path);
  if (Date.parse(config.startAt) < Date.now() - 5000) throw new Error("start_time_already_passed");
  const evidence = new Evidence(config, role);
  const lifecycle = new RunLifecycle(evidence, role);
  evidence.event({event: role === "participants" ? "participant_run_started" : "operator_run_started",
    phase: "setup", lifecycleVersion: 2,
    ...(role === "participants" ? {cohort: {count: config.count, ordinalStart: config.ordinalStart}} : {})});
  if (config.coordination === undefined) evidence.event({event: "coordination_unconfigured", phase: "setup"});
  const stopSampling = startResourceSampling(evidence);
  const stopLoopSampling = startWorkerSampling(evidence);
  const actors: Actor[] = [];
  let browser: Browser | undefined;
  let closing: Promise<void> | undefined;
  const closeBrowser = (): Promise<void> => browser === undefined ? Promise.resolve() : closing ??= browser.close();
  const closeOnAbort = (): void => { void closeBrowser().catch(() => undefined); };
  lifecycle.signal.addEventListener("abort", closeOnAbort, {once: true});
  const stop = (): void => lifecycle.finish("signal");
  process.once("SIGINT", stop);
  process.once("SIGTERM", stop);
  let timer: ReturnType<typeof setInterval> | undefined;
  let deadlineTimer: ReturnType<typeof setTimeout> | undefined;
  let fault: Promise<void> = Promise.resolve();
  let peakConnected = 0;
  let peakJoined = 0;
  let minimumConnectedAfterAllConnected = config.count;
  let beforeFault: {opens: number; closes: number}[] = [];
  let stage = "browser_launch";
  let participantsCompleted = false;
  let pendingSummary: Record<string, unknown> | undefined;
  let coordinator: ParticipantCoordinator | undefined;
  try {
    if (role === "participants" && config.coordination !== undefined) {
      coordinator = new ParticipantCoordinator(config, () => lifecycle.finish("run_failed"));
      await coordinator.start();
    }
    browser = await chromium.launch({headless: config.headless,
      ...(config.channel === "chrome" ? {channel: "chrome"} : {}),
      args: ["--disable-background-timer-throttling", "--disable-renderer-backgrounding"],
    });
    lifecycle.signal.throwIfAborted();
    evidence.save("browser.json", {version: browser.version()});
    stage = "run";
    if (role === "operator") {
      await runOperator(browser, config, evidence, credentials!, lifecycle);
      pendingSummary = {status: "operator_completed", rounds: 6, lifecycleVersion: 2,
        coordination: config.coordination === undefined ? "uncoordinated" : "lease"};
      return;
    }
    for (let index = 0; index < config.count; index++) {
      lifecycle.signal.throwIfAborted();
      actors.push(await createActor(browser, config.ordinalStart + index, config, evidence, lifecycle));
    }
    await cancellableDelay(Date.parse(config.startAt) - Date.now(), lifecycle.signal);
    lifecycle.startMeasurement();
    coordinator?.measurementReady(false);
    const runStarted = performance.now();
    const deadline = runStarted + config.durationSeconds * 1000;
    deadlineTimer = setTimeout(() => lifecycle.finish("run_deadline"), config.durationSeconds * 1000);
    const sampleConnections = (): void => {
      const connected = actors.filter(actor => actor.connected && !actor.page.isClosed()).length;
      const joined = actors.filter(actor => actor.joined && !actor.page.isClosed()).length;
      peakConnected = Math.max(peakConnected, connected);
      peakJoined = Math.max(peakJoined, joined);
      if (peakConnected === config.count)
        minimumConnectedAfterAllConnected = Math.min(minimumConnectedAfterAllConnected, connected);
      evidence.event({event: "simultaneous_ws", count: connected, phase: "measurement"});
      evidence.event({event: "simultaneous_joined", count: joined, phase: "measurement"});
      if (connected === config.count && joined === config.count) coordinator?.measurementReady(true);
    };
    timer = setInterval(sampleConnections, 1000);
    if (config.rampStages.length === 0) {
      for (const [index, actor] of actors.entries()) {
        actor.pending = (async () => {
          try {
            await cancellableDelay(config.count === 1 ? 0 : index * config.joinSpreadMs / (config.count - 1), lifecycle.signal);
            await runActor(actor, config, evidence, lifecycle.signal);
          } catch (error: unknown) {
            if (!lifecycle.signal.aborted) throw error;
          }
        })();
      }
    } else {
      let launched = 0;
      for (const count of config.rampStages) {
        evidence.event({event: "stage_started", count});
        for (const actor of actors.slice(launched, count))
          actor.pending = runActor(actor, config, evidence, lifecycle.signal);
        launched = count;
        const stageActors = actors.slice(0, count);
        while (true) {
          lifecycle.signal.throwIfAborted();
          if (stageActors.some(actor => actor.failed || actor.page.isClosed())) throw new Error("ramp_stage_failed");
          const remaining = deadline - performance.now();
          if (remaining <= 0) throw new Error("ramp_deadline_exceeded");
          if (stageActors.every(actor => actor.joined && actor.connected)) break;
          await cancellableDelay(Math.min(100, remaining), lifecycle.signal);
        }
        sampleConnections();
        evidence.event({event: "stage_ready", count});
        if (count < config.count) {
          if (deadline - performance.now() < config.rampHoldMs) throw new Error("ramp_deadline_exceeded");
          await cancellableDelay(config.rampHoldMs, lifecycle.signal);
        }
      }
    }
    if (config.scenario === "reconnect") fault = (async () => {
      try {
        await cancellableDelay(config.faultAfterSeconds * 1000, lifecycle.signal);
        beforeFault = actors.map(actor => ({opens: actor.wsOpens, closes: actor.wsCloses}));
        evidence.event({event: "offline_start"});
        await Promise.all(actors.map(actor => actor.context.setOffline(true)));
        await cancellableDelay(config.faultDurationMs, lifecycle.signal);
        await Promise.all(actors.map(actor => actor.context.setOffline(false)));
        evidence.event({event: "offline_end"});
      } catch {
        if (!lifecycle.signal.aborted) lifecycle.finish("run_failed");
      }
    })();
    await Promise.all(actors.map(actor => actor.pending));
    await fault;
    lifecycle.signal.throwIfAborted();
    await Promise.all(actors.map(async actor => {
      try { await flushBrowserDiagnostics(actor.page); }
      catch { evidence.event({event: "browser_probe_flush_failed", serial: actor.serial}); }
    }));
    const isolated = await verifyIsolation(actors, evidence);
    const observationsComplete = ["resource_sample_failed", "invalid_ui_observation", "http_observation_failed",
      "invalid_browser_probe", "browser_probe_flush_failed", "browser_probe_overflow", "browser_probe_delivery_failure"]
      .every(event => evidence.count(event) === 0);
    const reconnectVerified = config.scenario !== "reconnect" || actors.every((actor, index) => {
      const before = beforeFault[index];
      return before !== undefined && actor.wsOpens > before.opens && actor.wsCloses > before.closes && actor.connected;
    });
    const normalHealthy = config.scenario !== "normal" ||
      (evidence.count("request_failed") === 0 && minimumConnectedAfterAllConnected === config.count);
    participantsCompleted = actors.every(actor => actor.complete && !actor.failed);
    const commonConditionsPassed = !lifecycle.signal.aborted && isolated && observationsComplete &&
      reconnectVerified && peakConnected === config.count && participantsCompleted &&
      (config.scenario !== "normal" || minimumConnectedAfterAllConnected === config.count);
    const passed = commonConditionsPassed && normalHealthy;
    clearInterval(timer);
    clearTimeout(deadlineTimer);
    stopLoopSampling();
    const measurement = {
      peakConnected, peakJoined, minimumConnectedAfterAllConnected,
      requestFailures: evidence.count("request_failed"), resourceFailures: evidence.count("resource_sample_failed"),
      elapsedMs: performance.now() - runStarted,
      completed: actors.filter(actor => actor.complete).length,
      sessions: actors.map(actor => ({serial: actor.serial, acceptedRounds: [...actor.rounds],
        resultRounds: [...actor.results], complete: actor.complete, failed: actor.failed,
        wsOpens: actor.wsOpens, wsCloses: actor.wsCloses, failures: [...actor.failures]})),
    };
    lifecycle.finish("completed");
    await evidence.flush();
    const requestFailureDiagnostics = await analyzeRequestFailureFile(resolve(evidence.directory, "events.jsonl"));
    const measurementPassed = commonConditionsPassed && (config.scenario !== "normal" ||
      requestFailureDiagnostics.classifiedZeroUnexpectedFailuresSatisfied === true);
    pendingSummary = {status: passed ? "passed" : "failed", count: config.count,
      measurementStatus: measurementPassed ? "passed" : "failed", requestFailureDiagnostics,
      verdictVersion: "strict-v1", lifecycleVersion: 2,
      coordination: config.coordination === undefined ? "uncoordinated" : "lease",
      ...measurement, isolated, observationsComplete, reconnectVerified,
      failurePolicy: "strict-v1; receipt/result observations are independent of later answers; cancellations after shutdown are not actor failures",
      evidenceWriteMs: evidence.overhead(), physicalDevices: "not measured by this runner"};
    if (!passed) process.exitCode = 1;
  } catch (error: unknown) {
    const reason = fixedFailureReason(error, lifecycle.signal);
    lifecycle.finish("run_failed");
    evidence.save("failure.json", {status: "failed", stage, reason,
      lifecycleVersion: 2, createdContexts: actors.length, peakConnected, peakJoined,
      sessions: actors.map(actor => ({serial: actor.serial, acceptedRounds: actor.rounds,
        resultRounds: actor.results, failed: actor.failed, failures: actor.failures}))});
    process.exitCode = 1;
  } finally {
    if (timer) clearInterval(timer);
    if (deadlineTimer) clearTimeout(deadlineTimer);
    lifecycle.finish("completed");
    try { await coordinator?.stop(participantsCompleted); }
    catch { evidence.event({event: "coordination_publish_failed", phase: "shutdown"}); process.exitCode = 1; }
    try { await closeBrowser(); }
    catch { evidence.event({event: "browser_close_failed", phase: "shutdown"}); process.exitCode = 1; }
    await Promise.allSettled([...actors.map(actor => actor.pending), fault]);
    await stopSampling();
    stopLoopSampling();
    lifecycle.shutdownFinished();
    try { await evidence.close(); if (!evidence.health().complete) process.exitCode = 1; }
    catch { process.exitCode = 1; }
    if (pendingSummary) evidence.save("summary.json", {...pendingSummary, recordingHealth: evidence.health(),
      ...(!evidence.health().complete ? {status: "failed", measurementStatus: "failed", observationsComplete: false} : {})});
    process.removeListener("SIGINT", stop);
    process.removeListener("SIGTERM", stop);
  }
}

main().catch(() => {
  process.stderr.write("browser_runner_failed; inspect non-secret run evidence and prerequisites\n");
  process.exitCode = 1;
});
