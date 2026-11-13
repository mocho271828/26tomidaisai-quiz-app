import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { analyze, analyzeRequestFailureFile, type AnalysisEvent } from "../src/analyze.js";
import type { Telemetry } from "../src/evidence.js";

type Observation = {event: "ui_observation"; serial: number; monotonicMs: number; telemetry: Telemetry};
type FixtureEvent = AnalysisEvent;

function projection(serial: number, round: number, at: number, hasQuestion = true): Observation {
  return {event: "ui_observation", serial, monotonicMs: at, telemetry: {
    kind: "projection_received", round_number: round, state: "ReadyToSchedule",
    has_question: hasQuestion, monotonic_ms: at, wall_time_ms: at,
  }};
}

function display(serial: number, round: number, at: number, hasQuestion = true): Observation {
  return {event: "ui_observation", serial, monotonicMs: at, telemetry: {
    kind: "participant_display", round_number: round, phase: "waiting",
    synchronized: true, has_question: hasQuestion, monotonic_ms: at, wall_time_ms: at,
  }};
}

async function analyzeFixture(events: FixtureEvent[], analyzer = analyze): Promise<object> {
  const directory = await mkdtemp(join(tmpdir(), "quiz-analyze-test-"));
  try {
    const path = join(directory, "events.jsonl");
    await writeFile(path, events.map(event => JSON.stringify(event)).join("\n") + "\n");
    return await analyzer(path);
  } finally { await rm(directory, {recursive: true}); }
}

describe("initial projection to React state commit evidence", () => {
  it("requires a sealed measurement before returning an acceptance criterion to the runner", async () => {
    const started: FixtureEvent[] = [
      {event: "participant_run_started", monotonicMs: 0, lifecycleVersion: 2, cohort: {count: 1, ordinalStart: 1}},
      {event: "participant_measurement_started", monotonicMs: 1000, lifecycleVersion: 2},
    ];
    expect(await analyzeFixture(started, analyzeRequestFailureFile)).toMatchObject({
      measurementWindowAvailable: false, strictZeroRequestFailuresSatisfied: null,
      classifiedZeroUnexpectedFailuresSatisfied: null});
    expect(await analyzeFixture([...started,
      {event: "participant_run_finished", monotonicMs: 2000, lifecycleVersion: 2},
      {event: "request_failed", monotonicMs: 2100, serial: 1},
    ], analyzeRequestFailureFile)).toMatchObject({measurementWindowAvailable: true,
      recordedRawCount: 1, measurementRawCount: 0, strictZeroRequestFailuresSatisfied: true,
      classifiedZeroUnexpectedFailuresSatisfied: true});
  });

  it("excludes setup, shutdown and outside-cohort samples from every measurement family", async () => {
    const metrics = (serial: number, at: number, value: number): FixtureEvent[] => [
      {event: "http", serial, route: "answer", status: 500, monotonicMs: at, durationMs: value, networkDurationMs: value},
      {event: "http_response_headers", serial, route: "answer", monotonicMs: at, serverTiming: {api_total: value}},
      {event: "actor_failed", serial, stage: "answer_click", monotonicMs: at},
      {event: "request_failed", serial, monotonicMs: at},
      {event: "round_expected", serial, round: 1, monotonicMs: at},
      {event: "accepted_dom", serial, round: 1, monotonicMs: at},
      {event: "result_dom", serial, round: 1, monotonicMs: at},
      {event: "generator_cpu_percent", serial, durationMs: value, monotonicMs: at},
      {event: "generator_memory_used_bytes", serial, count: value, monotonicMs: at},
      {event: "generator_swap_used_bytes", serial, count: value, monotonicMs: at},
      {event: "generator_node_loop_p95_ms", serial, durationMs: value, monotonicMs: at},
      {event: "browser_probe", serial, monotonicMs: at, browserProbe: {
        kind: "longtask", duration_ms: value, monotonic_ms: at, wall_time_ms: at}},
    ];
    const result = await analyzeFixture([
      {event: "participant_run_started", monotonicMs: 0, lifecycleVersion: 2, cohort: {count: 1, ordinalStart: 1}},
      ...metrics(1, 600, 999),
      {event: "participant_measurement_started", monotonicMs: 1000, lifecycleVersion: 2},
      ...metrics(1, 1100, 10), ...metrics(2, 1200, 888),
      {event: "participant_run_finished", monotonicMs: 2000, lifecycleVersion: 2},
      ...metrics(1, 2100, 777),
    ]);
    expect(result).toMatchObject({measurementWindow: {mode: "lifecycle-v2", measurementStartedMs: 1000, measurementFinishedMs: 2000},
      http: {answer: {count: 1, max: 10}}, browserTransportMs: {answer: {count: 1, max: 10}},
      serverTimingMs: {api_total: {count: 1, max: 10}}, actorFailureStages: {answer_click: 1},
      attemptCounts: {roundExpected: 1, acceptedDom: 1, resultDom: 1},
      requestFailures: 1, serverErrors: 1, cpuPercent: {count: 1, max: 10},
      memoryUsedBytes: {count: 1, max: 10}, swapUsedBytes: {count: 1, max: 10},
      generatorNodeLoopMs: {intervalP95: {count: 1, max: 10}}, browserLongTaskMs: {count: 1, max: 10},
      requestFailureDiagnostics: {recordedRawCount: 4, measurementRawCount: 1, outsideMeasurementRawCount: 3,
        strictZeroRequestFailuresSatisfied: false},
    });
  });

  it("counts result recovery timeouts against the fixed cohort without outside-run or outside-cohort requests", async () => {
    const request = (serial: number, at: number, outcome: "timeout" | "validated"): Observation => ({
      event: "ui_observation", serial, monotonicMs: at,
      telemetry: {kind: "api_request", operation: "participant_result", phase: "settled", outcome,
        request_sequence: at, elapsed_ms: outcome === "timeout" ? 5000 : 50, monotonic_ms: at, wall_time_ms: at},
    });
    const result = await analyzeFixture([
      request(11, 10, "timeout"),
      {event: "participant_run_started", monotonicMs: 100, cohort: {ordinalStart: 11, count: 3}},
      request(11, 200, "validated"), request(12, 300, "timeout"), request(12, 400, "timeout"),
      request(14, 500, "timeout"),
      {event: "participant_run_finished", monotonicMs: 600},
      request(11, 700, "timeout"),
    ]);
    expect(result).toMatchObject({participantRecoveryRequests: {
      participant_result: {available: true, denominator: 3, requests: 3, participants: 2, timeoutParticipants: 1,
        outcomes: {validated: 1, timeout: 2}, timeoutElapsedMs: {count: 2, max: 5000}},
      participant_state: {available: true, denominator: 3, requests: 0},
    }});
  });

  it("pairs only the first question receipt and first eligible display per round", async () => {
    const result = await analyzeFixture([
      projection(1, 1, 5, false), display(1, 1, 8, false),
      projection(1, 1, 10), display(1, 1, 20),
      projection(1, 1, 100), display(1, 1, 200), display(1, 1, 300),
    ]);
    expect(result).toMatchObject({
      receivedToDisplayCommitMs: {count: 1, p50: 10, p95: 10, max: 10},
      receivedToDisplayCommitScope: "初回projection受信→初回React状態表示commit（問題本文の可視化や予定待機の測定ではない）",
    });
  });

  it("uses matching session and round even when delivery order differs", async () => {
    const result = await analyzeFixture([
      display(1, 1, 120), projection(1, 1, 100),
      projection(1, 2, 200), display(2, 2, 500), display(1, 2, 210),
      projection(2, 1, 600), display(2, 1, 630),
    ]);
    expect(result).toMatchObject({receivedToDisplayCommitMs: {count: 3, p50: 20, p95: 30, max: 30}});
  });

  it("excludes a display earlier than receipt without replacing it with a later transition", async () => {
    const result = await analyzeFixture([
      display(1, 1, 90), projection(1, 1, 100), display(1, 1, 120),
      projection(1, 2, 200), display(1, 2, 220),
      projection(2, 1, 300), display(3, 1, 310),
    ]);
    expect(result).toMatchObject({receivedToDisplayCommitMs: {count: 1, p50: 20, p95: 20, max: 20}});
  });

  it("excludes the old immediate CPU sample while retaining the bounded interval boundary", async () => {
    const result = await analyzeFixture([
      {event: "generator_cpu_percent", monotonicMs: 499.9, durationMs: 100},
      {event: "generator_cpu_percent", monotonicMs: 500, durationMs: 30},
      {event: "generator_cpu_percent", monotonicMs: 1500, durationMs: 40},
    ]);
    expect(result).toMatchObject({cpuPercent: {count: 2, p50: 30, p95: 40, max: 40}});
  });

  it("keeps browser DOM timings separate from delayed probe delivery and API durations", async () => {
    const result = await analyzeFixture([
      {event: "ui_observation", serial: 1, monotonicMs: 1000, deliveryLagMs: 800,
        telemetry: {kind: "answer", phase: "sending", round_number: 1, monotonic_ms: 200, wall_time_ms: 1200}},
      {event: "http_response_headers", serial: 1, monotonicMs: 1700, route: "answer", status: 200,
        callbackLagMs: 1000, serverTiming: {api_total: 100, thread_wait: 80}},
      {event: "http", serial: 1, monotonicMs: 1800, route: "answer", status: 200,
        durationMs: 1200, networkDurationMs: 150, callbackLagMs: 1000},
      {event: "browser_probe", serial: 1, monotonicMs: 5000, deliveryLagMs: 900,
        browserProbe: {kind: "dom", phase: "accepted", round_number: 1, visible: true,
          unanswered: false, monotonic_ms: 500, wall_time_ms: 1500}},
      {event: "actor_failed", serial: 1, round: 1, stage: "accepted_dom", monotonicMs: 14000},
    ]);
    expect(result).toMatchObject({
      browserSendingToReceiptDomMs: {count: 1, p95: 300},
      browserTransportMs: {answer: {count: 1, p95: 150}},
      serverTimingMs: {api_total: {count: 1, p95: 100}, thread_wait: {count: 1, p95: 80}},
      playwrightCallbackLatencyMs: {available: false},
      playwrightCallbackEpochDeltaUncalibratedMs: {http_response_headers: {count: 1, max: 1000}, http: {count: 1, max: 1000}},
      actorFailureStages: {accepted_dom: 1},
    });
  });
  it("pairs browser receipt with sending despite reverse binding delivery and excludes aborted resource zeros", async () => {
    const result = await analyzeFixture([
      {event: "browser_probe", serial: 1, monotonicMs: 1000, deliveryLagMs: -0.4,
        browserProbe: {kind: "dom", phase: "accepted", round_number: 1, visible: true,
          unanswered: false, monotonic_ms: 300, wall_time_ms: 10300}},
      {event: "ui_observation", serial: 1, monotonicMs: 2000, deliveryLagMs: -0.2,
        telemetry: {kind: "answer", phase: "sending", round_number: 1, monotonic_ms: 100, wall_time_ms: 10100}},
      {event: "http", serial: 1, monotonicMs: 2000, route: "answer", callbackLagMs: -0.5},
      {event: "browser_probe", serial: 1, monotonicMs: 3000,
        browserProbe: {kind: "resource", route: "answer", complete: false, duration_ms: 2500,
          server_timing: {}, monotonic_ms: 2700, wall_time_ms: 12700}},
    ]);
    expect(result).toMatchObject({
      browserSendingToReceiptDomMs: {count: 1, p95: 200},
      uiBindingDeliveryLagMs: {count: 1, p95: -0.2}, probeBindingDeliveryLagMs: {count: 1, p95: -0.4},
      playwrightCallbackEpochDeltaUncalibratedMs: {http: {count: 1, min: -0.5, negativeCount: 1}},
      browserIncompleteResources: 1, browserResourceObserverLagMs: {count: 0, p95: null},
      browserResourceTimingMs: {answer: {duration_ms: {count: 0, p95: null}}},
    });
  });
  it("keeps finish origin mismatch and uncalibrated headers out of measured latency percentiles", async () => {
    const result = await analyzeFixture([
      {event: "http", monotonicMs: 2000, callbackEpochDeltaUncalibratedMs: -228,
        callbackEpochDeltaReason: "playwright_finish_relative_origin_mismatch"},
      {event: "http_response_headers", monotonicMs: 1990, callbackEpochDeltaUncalibratedMs: 13,
        callbackEpochDeltaReason: "node_browser_epoch_not_calibrated"},
    ]);
    expect(result).toMatchObject({playwrightCallbackLatencyMs: {available: false},
      playwrightCallbackEpochDeltaUncalibratedMs: {
        http: {count: 1, min: -228, negativeCount: 1, reason: "playwright_finish_relative_origin_mismatch"},
        http_response_headers: {count: 1, max: 13, reason: "node_browser_epoch_not_calibrated"},
      }});
    const summary = result as {playwrightCallbackEpochDeltaUncalibratedMs: Record<string, object>};
    expect(summary.playwrightCallbackEpochDeltaUncalibratedMs.http).not.toHaveProperty("p95");
    expect(summary.playwrightCallbackEpochDeltaUncalibratedMs.http_response_headers).not.toHaveProperty("p95");
    expect(result).not.toHaveProperty("playwrightCallbackLagMs");
  });
});
