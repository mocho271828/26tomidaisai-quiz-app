import { afterEach, describe, expect, it, vi } from "vitest";

import { getOperatorResponseTimes } from "./operatorResponseTimeClient";
import { operatorResponseTimesSchema } from "./operatorResponseTimeContracts";

const roundId = "60000000-0000-4000-8000-000000000001";
const row = { rank: 1, public_label: "AAA", response_ms: 1234, is_correct: true };
const group = {
  answer_count: 1, average_response_ms: 1234, median_response_ms: 1234,
  fastest_response_ms: 1234, slowest_response_ms: 1234, rows: [row],
};
const data = {
  timing_basis: "client_monotonic_v1",
  rounds: [{ round_id: roundId, round_number: 1, is_practice: true, prompt: "練習" }],
  selected_round_id: roundId, all_answers: group, correct_answers: group,
};

describe("回答速度API境界", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("認証cookieと取消signal付きで検証済み条件を送る", async () => {
    const fetch = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(data))));
    vi.stubGlobal("fetch", fetch);
    const controller = new AbortController();
    await expect(getOperatorResponseTimes(roundId, controller.signal)).resolves.toEqual(data);
    expect(fetch).toHaveBeenCalledWith(
      `/api/v1/operator/response-times?round_id=${roundId}`,
      expect.objectContaining({ credentials: "same-origin", signal: expect.any(AbortSignal) }),
    );
    const signal = fetch.mock.calls[0]![1].signal as AbortSignal;
    controller.abort();
    expect(signal.aborted).toBe(true);
    await getOperatorResponseTimes(null);
    expect(fetch.mock.calls[1]![0]).toBe("/api/v1/operator/response-times");
  });

  it("不正な問題IDは通信前に拒否する", () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    expect(() => getOperatorResponseTimes("../state")).toThrow();
    expect(fetch).not.toHaveBeenCalled();
  });

  it.each([
    { ...data, token: "unexpected" },
    { ...data, selected_round_id: null },
    { ...data, all_answers: { ...group, average_response_ms: null } },
    { ...data, all_answers: { ...group, answer_count: 2 } },
    { ...data, all_answers: { ...group, rows: [{ ...row, response_ms: -1 }] } },
    { ...data, correct_answers: { ...group, rows: [{ ...row, is_correct: false }] } },
    { ...data, correct_answers: { ...group, rows: [{ ...row, response_ms: 1235 }] } },
  ])("不整合な応答を表示へ渡さない %#", (value) => {
    expect(operatorResponseTimesSchema.safeParse(value).success).toBe(false);
  });

  it("確定問題がない空状態と0msの有効回答を区別して受理する", () => {
    const empty = { answer_count: 0, average_response_ms: null, median_response_ms: null,
      fastest_response_ms: null, slowest_response_ms: null, rows: [] };
    expect(operatorResponseTimesSchema.safeParse({ ...data, rounds: [], selected_round_id: null,
      all_answers: empty, correct_answers: empty }).success).toBe(true);
    const zero = { ...group, average_response_ms: 0, median_response_ms: 0,
      fastest_response_ms: 0, slowest_response_ms: 0, rows: [{ ...row, response_ms: 0 }] };
    expect(operatorResponseTimesSchema.safeParse({ ...data, all_answers: zero, correct_answers: zero }).success).toBe(true);
  });
});
