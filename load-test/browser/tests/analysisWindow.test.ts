import { describe, expect, it } from "vitest";
import { analysisWindow, windowSummary } from "../src/analysisWindow.js";
import type { AnalysisEvent } from "../src/analyze.js";

const start: AnalysisEvent = {event: "participant_run_started", monotonicMs: 10,
  lifecycleVersion: 2, cohort: {count: 2, ordinalStart: 1}};
const measurement: AnalysisEvent = {event: "participant_measurement_started", monotonicMs: 100, lifecycleVersion: 2};
const end: AnalysisEvent = {event: "participant_run_finished", monotonicMs: 200, lifecycleVersion: 2};

describe("one observation window for all browser metrics", () => {
  it("separates setup, measurement and shutdown while retaining raw events", () => {
    const result = analysisWindow([start, end, measurement,
      {event: "request_failed", serial: 1, monotonicMs: 50},
      {event: "request_failed", serial: 1, monotonicMs: 150},
      {event: "request_failed", serial: 1, monotonicMs: 250},
      {event: "actor_failed", serial: 3, monotonicMs: 160},
    ]);
    expect(windowSummary(result)).toMatchObject({mode: "lifecycle-v2", available: true,
      setupStartedMs: 10, measurementStartedMs: 100, measurementFinishedMs: 200,
      setupEvents: 2, measurementEvents: 4, shutdownEvents: 1, outsideCohortEvents: 1});
  });

  it("does not turn a browser setup failure into a measured zero-error run", () => {
    const result = analysisWindow([start, {event: "actor_created", serial: 1, monotonicMs: 50}, end]);
    expect(result).toMatchObject({mode: "lifecycle-v2", available: false,
      reason: "measurement_not_started", measurement: [], measurementStartedMs: null});
  });

  it("marks old evidence explicitly and uses its known end rather than cleanup", () => {
    const result = analysisWindow([{...start, lifecycleVersion: undefined},
      {event: "request_failed", serial: 1, monotonicMs: 150}, {...end, lifecycleVersion: undefined},
      {event: "request_failed", serial: 1, monotonicMs: 250}]);
    expect(result).toMatchObject({mode: "legacy-run-boundaries", available: true,
      reason: "setup_and_measurement_not_separable", measurementStartedMs: 10, measurementFinishedMs: 200});
    expect(result.measurement.filter(event => event.event === "request_failed")).toHaveLength(1);
    expect(analysisWindow([{event: "request_failed", monotonicMs: 50}])).toMatchObject({
      mode: "legacy-unscoped", available: false, reason: "lifecycle_markers_missing"});
  });

  it("censors a missing end and rejects duplicate or inverted lifecycle markers", () => {
    expect(analysisWindow([start, measurement, {event: "http", monotonicMs: 190}]))
      .toMatchObject({finishedObserved: false, measurementFinishedMs: 190});
    expect(analysisWindow([start, start, measurement, end]))
      .toMatchObject({mode: "invalid", reason: "duplicate_lifecycle_marker", measurement: []});
    expect(analysisWindow([start, measurement, {...end, monotonicMs: 90}]))
      .toMatchObject({mode: "invalid", reason: "lifecycle_marker_order_invalid", measurement: []});
  });

  it("uses operator measurement markers without inventing a participant cohort", () => {
    expect(analysisWindow([
      {event: "operator_run_started", monotonicMs: 0, lifecycleVersion: 2},
      {event: "operator_measurement_started", monotonicMs: 10, lifecycleVersion: 2},
      {event: "operator_http", monotonicMs: 20},
      {event: "operator_run_finished", monotonicMs: 30, lifecycleVersion: 2},
      {event: "operator_http", monotonicMs: 40},
    ])).toMatchObject({mode: "lifecycle-v2", available: true, cohort: null,
      measurementStartedMs: 10, measurementFinishedMs: 30, shutdown: [{event: "operator_http", monotonicMs: 40}]});
  });
});
