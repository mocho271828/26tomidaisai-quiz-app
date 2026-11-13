import { describe, expect, it } from "vitest";
import { analyzeRequestFailures } from "../src/requestFailureAnalysis.js";
import type { AnalysisEvent } from "../src/analyze.js";

type ApiRequest = Extract<NonNullable<AnalysisEvent["telemetry"]>, {kind: "api_request"}>;
function failed(serial: number, sequence?: number): AnalysisEvent {
  return {event: "request_failed", serial, requestSequence: sequence, route: "personal_result", monotonicMs: 100};
}
function settled(serial: number, sequence: number, outcome: ApiRequest["outcome"],
  reason?: ApiRequest["cancellation_reason"]): AnalysisEvent {
  return {event: "ui_observation", serial, monotonicMs: 101,
    telemetry: {kind: "api_request", operation: "participant_result", request_sequence: sequence,
      phase: "settled", outcome, cancellation_reason: reason, elapsed_ms: 100,
      monotonic_ms: 200, wall_time_ms: 1000}};
}

describe("request failure classification preserves the strict raw criterion", () => {
  it("classifies correlated expected cancellation separately without passing strict zero failures", () => {
    const events = [failed(1, 1), settled(1, 1, "aborted", "result_received"),
      failed(1, 2), settled(1, 2, "aborted", "deadline"),
      failed(2, 1), settled(2, 1, "transport_error"),
      failed(3, 1), settled(3, 1, "aborted"), failed(4)];
    const raw = [...events, failed(4, 2)];
    expect(analyzeRequestFailures(events, raw, true)).toMatchObject({recordedRawCount: 6,
      measurementRawCount: 5, outsideMeasurementRawCount: 1, strictZeroRequestFailuresSatisfied: false,
      classifications: {expected_cancel: {requests: 1, participants: 1}, timeout: {requests: 1},
        transport: {requests: 1}, unknown: {requests: 2, participants: 2}}});
  });

  it("does not infer expected cancellation from adjacent events or ambiguous correlation", () => {
    const mismatch = settled(1, 1, "aborted", "hidden");
    if (mismatch.telemetry?.kind === "api_request") mismatch.telemetry.operation = "answer_status";
    const duplicate = settled(2, 1, "aborted", "superseded");
    const events = [failed(1, 1), mismatch, failed(2, 1), duplicate, duplicate,
      failed(3, 1), settled(4, 1, "aborted", "unmount"),
      failed(5, 1), failed(5, 1), settled(5, 1, "aborted", "hidden")];
    expect(analyzeRequestFailures(events, events, true)).toMatchObject({
      classifications: {expected_cancel: {requests: 0}, unknown: {requests: 5}}});
  });

  it("does not call missing measurement evidence a successful strict criterion", () => {
    expect(analyzeRequestFailures([], [], false)).toMatchObject({strictZeroRequestFailuresSatisfied: null,
      classifiedZeroUnexpectedFailuresSatisfied: null});
  });

  it("keeps the strict failure alongside a classified pass and never exempts lost authentication", () => {
    const cancelled = [failed(1, 1), settled(1, 1, "aborted", "superseded")];
    expect(analyzeRequestFailures(cancelled, cancelled, true)).toMatchObject({
      classificationVersion: "browser-request-failures-v1", strictZeroRequestFailuresSatisfied: false,
      classifiedZeroUnexpectedFailuresSatisfied: true, unexpectedFailureCount: 0});
    const unauthorized = [failed(1, 1), settled(1, 1, "aborted", "auth_invalidated")];
    expect(analyzeRequestFailures(unauthorized, unauthorized, true)).toMatchObject({
      classifiedZeroUnexpectedFailuresSatisfied: false, classifications: {unknown: {requests: 1}}});
  });
});
