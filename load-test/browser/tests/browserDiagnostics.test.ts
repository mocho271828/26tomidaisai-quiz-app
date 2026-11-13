import { describe, expect, it } from "vitest";
import { browserProbeSchema, callbackEpochDelta, parseServerTiming } from "../src/browserDiagnostics.js";
import { telemetrySchema } from "../src/evidence.js";

describe("separate browser transport and generator processing", () => {
  it("uses browser timing instead of the late Node request callback as the start", () => {
    expect(callbackEpochDelta(100700, 100000, 450)).toBe(250);
    expect(callbackEpochDelta(100700, 0, 450)).toBeUndefined();
    expect(callbackEpochDelta(100700, 100000, -1)).toBeUndefined();
    expect(callbackEpochDelta(100449.5, 100000, 450)).toBe(-0.5);
  });
  it("only retains finite server durations from the fixed metrics", () => {
    expect(parseServerTiming("api_total;dur=150.5, thread_wait;dur=80, participant_metadata;dur=50, recovery_wait;dur=30, common_state_wait;dur=20, dispatch_wait;dur=1, unknown_secret;dur=99, handler;dur=NaN, db_acquire;dur=-1"))
      .toEqual({api_total: 150.5, thread_wait: 80, participant_metadata: 50, recovery_wait: 30, common_state_wait: 20, dispatch_wait: 1});
    expect(parseServerTiming(undefined)).toEqual({});
  });
  it("rejects browser URLs, identifiers, payloads and unrecognized Server-Timing metrics", () => {
    const event = {kind: "resource", route: "answer", complete: true, duration_ms: 20, first_byte_ms: 15,
      response_body_ms: 5, observer_lag_ms: 100, server_timing: {api_total: 12, recovery_wait: 3, common_state_wait: 2},
      monotonic_ms: 1000, wall_time_ms: 100000};
    expect(browserProbeSchema.safeParse(event).success).toBe(true);
    for (const extra of [{url: "https://localhost/private"}, {client_answer_id: "private"},
      {body: "private"}, {server_timing: {secret: 20}}, {duration_ms: Infinity}])
      expect(browserProbeSchema.safeParse({...event, ...extra}).success).toBe(false);
  });
  it("records personal result recovery and its timeout without identity or payload", () => {
    expect(telemetrySchema.safeParse({kind: "api_request", operation: "participant_result", request_sequence: 1,
      phase: "settled", elapsed_ms: 5000, outcome: "timeout", monotonic_ms: 6000, wall_time_ms: 106000}).success).toBe(true);
    const resource = {kind: "resource", route: "personal_result", complete: false, duration_ms: 5000,
      server_timing: {}, monotonic_ms: 6000, wall_time_ms: 106000};
    expect(browserProbeSchema.safeParse(resource).success).toBe(true);
    expect(browserProbeSchema.safeParse({...resource, result: {total_points: 100}}).success).toBe(false);
  });
  it("preserves unavailable aborted resource timings as missing rather than zero", () => {
    const aborted = browserProbeSchema.parse({kind: "resource", route: "answer", complete: false,
      duration_ms: 2500, server_timing: {}, monotonic_ms: 3000, wall_time_ms: 103000});
    expect(aborted).not.toHaveProperty("first_byte_ms");
    expect(aborted).not.toHaveProperty("response_body_ms");
    expect(aborted).not.toHaveProperty("observer_lag_ms");
  });
  it("accepts answer transport/application/DOM diagnostics without accepting secrets", () => {
    const request = {kind: "api_request", operation: "answer_status", request_sequence: 3,
      phase: "settled", elapsed_ms: 250, outcome: "validated", status: 200,
      server_timing: {api_total: 80, participant_metadata: 50, recovery_wait: 3, common_state_wait: 2, dispatch_wait: 1}, monotonic_ms: 1000, wall_time_ms: 100000};
    expect(telemetrySchema.safeParse(request).success).toBe(true);
    expect(telemetrySchema.safeParse({...request, answer: {option_id: "private"}}).success).toBe(false);
    expect(telemetrySchema.safeParse({kind: "answer_timeout", round_number: 1, timeout_ms: 2500,
      timer_lag_ms: 700, monotonic_ms: 1000, wall_time_ms: 100000}).success).toBe(true);
  });
  it("accepts only strict random/HMAC correlations and fixed API refusal codes", () => {
    const request = {kind: "api_request", operation: "answer_post", request_sequence: 1,
      phase: "settled", elapsed_ms: 100, outcome: "http_error", status: 409,
      server_request_id: "a".repeat(32), answer_trace: "b".repeat(64), api_code: "state_conflict",
      monotonic_ms: 1000, wall_time_ms: 100000};
    expect(telemetrySchema.safeParse(request).success).toBe(true);
    expect(telemetrySchema.safeParse({...request, api_code: "other"}).success).toBe(true);
    for (const extra of [
      {server_request_id: "private-participant"}, {server_request_id: "A".repeat(32)},
      {server_request_id: "a".repeat(33)}, {server_request_id: "a".repeat(31) + "\n"},
      {answer_trace: "private-answer"}, {answer_trace: "B".repeat(64)},
      {answer_trace: "b".repeat(65)}, {answer_trace: "b".repeat(63) + "\n"},
      {api_code: "private-code"}, {message: "private-message"}, {participant_id: "private-participant"},
      {question_text: "private-question"}, {answer_body: "private-answer"},
    ]) expect(telemetrySchema.safeParse({...request, ...extra}).success).toBe(false);
  });
});
