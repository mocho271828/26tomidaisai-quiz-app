import { describe, expect, it } from "vitest";
import { analyzeContinuity } from "../src/continuityAnalysis.js";
import type { AnalysisEvent } from "../src/analyze.js";

const start: AnalysisEvent = {event: "participant_run_started", monotonicMs: 0, cohort: {count: 3, ordinalStart: 10}};
const end: AnalysisEvent = {event: "participant_run_finished", monotonicMs: 1000};
function ws(serial: number, phase: "open" | "closed" | "retry" | "stable", at: number): AnalysisEvent {
  return {event: "ui_observation", serial, monotonicMs: at,
    telemetry: {kind: "ws", audience: "participant", phase, monotonic_ms: at + 9000, wall_time_ms: at + 10000}};
}
function timeout(serial: number, at: number): AnalysisEvent {
  return {event: "ui_observation", serial, monotonicMs: at, telemetry: {kind: "answer_timeout",
    round_number: 1, timeout_ms: 2500, timer_lag_ms: 0, monotonic_ms: at + 9000, wall_time_ms: at + 10000}};
}

describe("continuity diagnostics without changing acceptance", () => {
  it("separates preparation, unstarted actors, initial connection wait and established disconnection", () => {
    const v2start: AnalysisEvent = {...start, lifecycleVersion: 2, cohort: {count: 4, ordinalStart: 10}};
    const result = analyzeContinuity([v2start,
      {event: "actor_created", serial: 10, monotonicMs: 10},
      {event: "actor_created", serial: 11, monotonicMs: 20},
      {event: "actor_created", serial: 12, monotonicMs: 30},
      {event: "participant_measurement_started", lifecycleVersion: 2, monotonicMs: 100},
      {event: "actor_started", serial: 10, monotonicMs: 150},
      {event: "actor_started", serial: 11, monotonicMs: 180},
      ws(10, "open", 200), ws(10, "closed", 500), end,
      {event: "actor_started", serial: 12, monotonicMs: 1100},
      {event: "actor_failed", serial: 13, monotonicMs: 1200},
    ]);
    expect(result).toMatchObject({denominator: 4, runDurationMs: 900, neverConnectedParticipants: 3,
      connectionIssueParticipants: 2, initialConnectionPendingParticipants: 1, failedParticipants: 0,
      lifecycle: {available: true, createdParticipants: 3, setupIncompleteParticipants: 1,
        startedParticipants: 2, notStartedParticipants: 2, createdNotStartedParticipants: 1},
      initialConnectionWaitIntervals: [{serial: 10, durationMs: 50, recovered: true},
        {serial: 11, durationMs: 820, recovered: false}],
      disconnectedIntervals: [{serial: 10, durationMs: 500, recovered: false}],
    });
  });

  it("retains the fixed cohort when setup fails before any actor starts", () => {
    expect(analyzeContinuity([{...start, lifecycleVersion: 2},
      {event: "actor_created", serial: 10, monotonicMs: 10}, end])).toMatchObject({
      available: true, denominator: 3, measurementAvailable: false, runDurationMs: null,
      connectionIssueParticipants: null,
      lifecycle: {createdParticipants: 1, setupIncompleteParticipants: 2, startedParticipants: 0, notStartedParticipants: 3},
    });
  });

  it("keeps missing sessions in the denominator and deduplicates repeated connection failures", () => {
    const result = analyzeContinuity([start, ws(10, "open", 100), ws(11, "open", 110),
      ws(10, "closed", 200), ws(10, "retry", 300), ws(10, "stable", 500), ws(10, "open", 510),
      ws(11, "closed", 800), end, ws(10, "closed", 1100)]);
    expect(result).toMatchObject({denominator: 3, neverConnectedParticipants: 1,
      disconnectedParticipants: 2, connectionIssueParticipants: 3, runFinishedObserved: true,
      neverConnectedIntervals: [{serial: 12, durationMs: 1000, recovered: false}],
      disconnectedIntervals: [{serial: 10, durationMs: 300, recovered: true}, {serial: 11, durationMs: 200, recovered: false}]});
  });
  it("counts missing questions and timeout people per round using the fixed cohort", () => {
    const result = analyzeContinuity([start,
      {event: "round_expected", serial: 10, round: 1, monotonicMs: 100},
      {event: "ui_observation", serial: 10, monotonicMs: 200, telemetry: {
        kind: "projection_received", round_number: 1, state: "ReadyToSchedule", has_question: true, monotonic_ms: 9000, wall_time_ms: 10000}},
      timeout(10, 300), timeout(10, 350), {event: "accepted_dom", serial: 10, round: 1, monotonicMs: 450},
      timeout(11, 600), {event: "actor_failed", serial: 11, round: 1, stage: "accepted_dom", monotonicMs: 700},
      {event: "actor_failed", serial: 11, round: 1, stage: "result_dom", monotonicMs: 710}, end]);
    expect(result).toMatchObject({denominator: 3, timeoutParticipants: 2, failedParticipants: 1,
      questionMissingParticipants: 2, affectedParticipants: 3,
      perRound: [{round: 1, denominator: 3, questionMissingParticipants: 2, timeoutParticipants: 2, failedParticipants: 1,
        questionMissingIntervals: [{serial: 11, durationMs: 900}, {serial: 12, durationMs: 900}]}],
      timeoutIntervals: [{serial: 10, durationMs: 150, recovered: true}, {serial: 11, durationMs: 400, recovered: false}]});
    expect(result).not.toHaveProperty("passed");
  });
  it("does not infer a denominator from survivors or merge conflicting cohorts", () => {
    expect(analyzeContinuity([ws(1, "open", 20)])).toMatchObject({available: false, denominator: null});
    expect(analyzeContinuity([start, start, end])).toMatchObject({available: false, denominator: null});
  });
  it("marks unfinished observations as censored and excludes outside-cohort identities", () => {
    expect(analyzeContinuity([start, ws(999, "open", 30), ws(10, "open", 10), ws(10, "closed", 20)]))
      .toMatchObject({denominator: 3, outsideCohortEvents: 1, runFinishedObserved: false,
        disconnectedIntervals: [{serial: 10, durationMs: 10, recovered: false}]});
  });
});
