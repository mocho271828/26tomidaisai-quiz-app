import { afterEach, describe, expect, it, vi } from "vitest";

import { beginRequestObservation } from "./requestObservation";
import { API_OBSERVATION_CODES } from "../quiz/_utils/browserObservation";

const observation = vi.hoisted(() => ({ observeBrowser: vi.fn() }));
vi.mock("../quiz/_utils/browserObservation", async (original) => ({
  ...await original<typeof import("../quiz/_utils/browserObservation")>(),
  observeBrowser: observation.observeBrowser,
}));

describe("API browser request timing", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("通信完了・JS処理・JSON読み取り・契約検証を分離し固定Server-Timingだけを残す", () => {
    const getEntriesByName = vi.fn().mockReturnValue([{ startTime: 11, responseEnd: 24 }]);
    vi.stubGlobal("performance", {
      now: vi.fn().mockReturnValueOnce(10).mockReturnValueOnce(30)
        .mockReturnValueOnce(50).mockReturnValueOnce(55),
      getEntriesByName,
    });
    const request = beginRequestObservation("answer_post", "/api/v1/participant/rounds/private-id/answers");
    request?.headers(new Response(null, { status: 200, headers: {
      "Server-Timing": 'api_total;dur=12.5, participant_metadata;dur=5, recovery_wait;dur=3, common_state_wait;dur=2, dispatch_wait;dur=0.5, handler;dur=8;desc="private-description", secret;dur=1',
    } }));
    request?.body();
    request?.settled("validated");

    expect(observation.observeBrowser).toHaveBeenCalledTimes(2);
    expect(observation.observeBrowser.mock.calls[1]?.[0]).toMatchObject({
      kind: "api_request", operation: "answer_post", phase: "settled", outcome: "validated",
      elapsed_ms: 45, headers_elapsed_ms: 20, body_elapsed_ms: 40, validation_ms: 5,
      response_end_elapsed_ms: 14, delivery_lag_ms: 31,
      server_timing: { api_total: 12.5, participant_metadata: 5, recovery_wait: 3, common_state_wait: 2, dispatch_wait: 0.5, handler: 8 }, status: 200,
    });
    expect(JSON.stringify(observation.observeBrowser.mock.calls)).not.toContain("private-");
    expect(JSON.stringify(observation.observeBrowser.mock.calls)).not.toContain("secret");
  });

  it("中断と欠測ResourceTimingを0の受信時間として補完しない", () => {
    vi.stubGlobal("performance", { now: vi.fn().mockReturnValueOnce(10).mockReturnValueOnce(20) });
    const request = beginRequestObservation("answer_status", "/api/v1/participant/answers/private-id/status");
    request?.settled("aborted");

    const settled = observation.observeBrowser.mock.calls[1]?.[0];
    expect(settled).toMatchObject({ operation: "answer_status", outcome: "aborted", elapsed_ms: 10 });
    expect(settled).not.toHaveProperty("response_end_elapsed_ms");
    expect(settled).not.toHaveProperty("delivery_lag_ms");
    expect(settled).not.toHaveProperty("status");
    expect(settled).not.toHaveProperty("server_request_id");
    expect(settled).not.toHaveProperty("answer_trace");
    expect(settled).not.toHaveProperty("api_code");
  });

  it("POSTとstatusを検証済み回答traceで照合し、HTTP拒否の固定codeだけを残す", () => {
    const answerTrace = "a".repeat(64);
    const post = beginRequestObservation("answer_post", "/api/v1/participant/rounds/private-round/answers");
    post?.headers(new Response(null, { status: 409, headers: {
      "x-quiz-request-id": "b".repeat(32), "x-quiz-answer-trace": answerTrace,
    } }));
    post?.settled("http_error", "state_conflict");
    const status = beginRequestObservation("answer_status", "/api/v1/participant/answers/private-answer/status");
    status?.headers(new Response(null, { status: 200, headers: {
      "x-quiz-request-id": "c".repeat(32), "x-quiz-answer-trace": answerTrace,
    } }));
    status?.settled("validated");

    expect(observation.observeBrowser.mock.calls[1]?.[0]).toMatchObject({
      operation: "answer_post", outcome: "http_error", api_code: "state_conflict",
      server_request_id: "b".repeat(32), answer_trace: answerTrace,
    });
    expect(observation.observeBrowser.mock.calls[3]?.[0]).toMatchObject({
      operation: "answer_status", outcome: "validated", server_request_id: "c".repeat(32), answer_trace: answerTrace,
    });
    expect(observation.observeBrowser.mock.calls[3]?.[0]).not.toHaveProperty("api_code");
    expect(JSON.stringify(observation.observeBrowser.mock.calls)).not.toContain("private-");
  });

  it.each(API_OBSERVATION_CODES)("固定API拒否code %sを記録する", (apiCode) => {
    const request = beginRequestObservation("answer_post", "/private-url");
    request?.settled("http_error", apiCode);
    expect(observation.observeBrowser.mock.calls[1]?.[0]).toHaveProperty("api_code", apiCode);
  });

  it.each(["private-participant", "B".repeat(32), "b".repeat(31), "b".repeat(33), "g".repeat(32)])(
    "不正な相関header %sと任意のAPI codeをイベントへ漏らさない", (header) => {
      const request = beginRequestObservation("answer_post", "/private-url");
      request?.headers(new Response(null, { status: 409, headers: {
        "x-quiz-request-id": header, "x-quiz-answer-trace": header,
      } }));
      request?.settled("http_error", "private-participant-answer");

      const settled = observation.observeBrowser.mock.calls[1]?.[0];
      expect(settled).toHaveProperty("api_code", "other");
      expect(settled).not.toHaveProperty("server_request_id");
      expect(settled).not.toHaveProperty("answer_trace");
      expect(JSON.stringify(observation.observeBrowser.mock.calls)).not.toContain(header);
      expect(JSON.stringify(observation.observeBrowser.mock.calls)).not.toContain("private-participant-answer");
    },
  );

  it("headers受信済み中断は実際の相関だけを残し、未読API codeを補完しない", () => {
    const request = beginRequestObservation("answer_post", "/private-url");
    request?.headers(new Response(null, { status: 409, headers: {
      "x-quiz-request-id": "d".repeat(32), "x-quiz-answer-trace": "e".repeat(64),
    } }));
    request?.settled("aborted", "private-error");

    const settled = observation.observeBrowser.mock.calls[1]?.[0];
    expect(settled).toMatchObject({ outcome: "aborted", server_request_id: "d".repeat(32), answer_trace: "e".repeat(64) });
    expect(settled).not.toHaveProperty("api_code");
    expect(JSON.stringify(observation.observeBrowser.mock.calls)).not.toContain("private-error");
  });

  it("別リクエストのresourceと不正なdurationを採用しない", () => {
    vi.stubGlobal("performance", {
      now: vi.fn().mockReturnValueOnce(10).mockReturnValueOnce(30).mockReturnValueOnce(50),
      getEntriesByName: vi.fn().mockReturnValue([{ startTime: 9, responseEnd: 24 }]),
    });
    const request = beginRequestObservation("participant_state", "/api/v1/participant/session");
    request?.headers(new Response(null, { status: 200, headers: {
      "Server-Timing": "api_total;dur=, handler;dur=-1, db_acquire;dur=NaN",
    } }));
    request?.settled("contract_error");

    expect(observation.observeBrowser.mock.calls[1]?.[0]).toMatchObject({
      server_timing: {}, outcome: "contract_error",
    });
    expect(observation.observeBrowser.mock.calls[1]?.[0]).not.toHaveProperty("response_end_elapsed_ms");
  });
});
