import type { AnalysisEvent } from "./analyze.js";

export type AnalysisWindow = {
  mode: "lifecycle-v2" | "legacy-run-boundaries" | "legacy-unscoped" | "invalid";
  available: boolean;
  reason: string | null;
  setupStartedMs: number | null;
  measurementStartedMs: number | null;
  measurementFinishedMs: number | null;
  finishedObserved: boolean;
  cohort: {count: number; ordinalStart: number} | null;
  measurement: AnalysisEvent[];
  setup: AnalysisEvent[];
  shutdown: AnalysisEvent[];
  outside: AnalysisEvent[];
};

function invalid(input: AnalysisEvent[], reason: string): AnalysisWindow {
  return {mode: "invalid", available: false, reason, setupStartedMs: null,
    measurementStartedMs: null, measurementFinishedMs: null, finishedObserved: false,
    cohort: null, measurement: [], setup: [], shutdown: [], outside: input};
}

export function analysisWindow(input: AnalysisEvent[]): AnalysisWindow {
  const events = [...input].sort((left, right) => left.monotonicMs - right.monotonicMs);
  const participant = events.some(event => event.event.startsWith("participant_") && event.lifecycleVersion === 2 ||
    event.event === "participant_run_started");
  const operator = events.some(event => event.event.startsWith("operator_") && event.lifecycleVersion === 2 ||
    event.event === "operator_run_started");
  if (participant && operator) return invalid(events, "mixed_run_roles");
  const role = operator ? "operator" : "participant";
  const starts = events.filter(event => event.event === `${role}_run_started`);
  const measurements = events.filter(event => event.event === `${role}_measurement_started`);
  const finishes = events.filter(event => event.event === `${role}_run_finished`);
  const isVersioned = measurements.length > 0 || events.some(event => event.lifecycleVersion === 2);
  if (starts.length > 1 || measurements.length > 1 || finishes.length > 1)
    return invalid(events, "duplicate_lifecycle_marker");
  const start = starts[0];
  const measurement = measurements[0];
  const finish = finishes[0];
  if (start === undefined) {
    if (isVersioned || finish !== undefined) return invalid(events, "run_start_missing");
    // 境界のない旧証跡は読み出せるが、測定区間を確認できた結果とは区別する。
    return {mode: "legacy-unscoped", available: false, reason: "lifecycle_markers_missing",
      setupStartedMs: null, measurementStartedMs: events[0]?.monotonicMs ?? null,
      measurementFinishedMs: events.at(-1)?.monotonicMs ?? null, finishedObserved: false,
      cohort: null, measurement: events, setup: [], shutdown: [], outside: []};
  }
  if (measurement !== undefined && measurement.monotonicMs < start.monotonicMs ||
    finish !== undefined && finish.monotonicMs < (measurement ?? start).monotonicMs)
    return invalid(events, "lifecycle_marker_order_invalid");
  const cohort = start.cohort;
  if (role === "participant" && (cohort === undefined || cohort.ordinalStart + cohort.count - 1 > 17576))
    return invalid(events, "single_fixed_cohort_missing");
  const mode = isVersioned ? "lifecycle-v2" : "legacy-run-boundaries";
  const from = isVersioned ? measurement?.monotonicMs ?? null : start.monotonicMs;
  const through = finish?.monotonicMs ?? events.at(-1)?.monotonicMs ?? start.monotonicMs;
  const beforeRun = events.filter(event => event.monotonicMs < start.monotonicMs);
  return {
    mode, available: from !== null,
    reason: from === null ? "measurement_not_started" : isVersioned ? null : "setup_and_measurement_not_separable",
    setupStartedMs: start.monotonicMs, measurementStartedMs: from, measurementFinishedMs: from === null ? null : through,
    finishedObserved: finish !== undefined, cohort: cohort ?? null,
    measurement: from === null ? [] : events.filter(event => event.monotonicMs >= from && event.monotonicMs <= through),
    setup: events.filter(event => event.monotonicMs >= start.monotonicMs &&
      (from === null ? event.monotonicMs <= through : event.monotonicMs < from)),
    shutdown: finish === undefined ? [] : events.filter(event => event.monotonicMs > through),
    outside: beforeRun,
  };
}

export function inCohort(event: AnalysisEvent, window: AnalysisWindow): boolean {
  const cohort = window.cohort;
  return cohort === null || event.serial === undefined ||
    event.serial >= cohort.ordinalStart && event.serial < cohort.ordinalStart + cohort.count;
}

export function windowSummary(window: AnalysisWindow): object {
  return {
    mode: window.mode, available: window.available, reason: window.reason,
    setupStartedMs: window.setupStartedMs, measurementStartedMs: window.measurementStartedMs,
    measurementFinishedMs: window.measurementFinishedMs, finishedObserved: window.finishedObserved,
    measurementEvents: window.measurement.length, setupEvents: window.setup.length,
    shutdownEvents: window.shutdown.length, outsideRunEvents: window.outside.length,
    outsideCohortEvents: window.measurement.filter(event => !inCohort(event, window)).length,
    scope: "指標の窓はNode観測時刻で統一。終了後に届いたbrowser観測もshutdownへ分離。legacyは準備と測定の区別を推測しない",
  };
}
