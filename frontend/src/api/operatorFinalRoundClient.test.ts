import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiResponseError } from "./client";
import { getOperatorFinalRound, setOperatorFinalRound } from "./operatorFinalRoundClient";
import { operatorFinalRoundMutationSchema, operatorFinalRoundSchema } from "./operatorFinalRoundContracts";

const eventId = "79000000-0000-4000-8000-000000000001";
const roundId = "79000000-0000-4000-8000-000000000002";
const mutation = { expected_version: 5, idempotency_key: "79000000-0000-4000-8000-000000000003" };
const response = {
  operation: "set_final_round", event_id: eventId, event_version: 6,
  round_id: null, round_version: null, round_state: null,
  result_visibility: { distribution: false, personal_ranking: false, overall_ranking: false },
  open_at: null, standard_close_at: null, valid_answer_count: null, ranking_row_count: null, is_replay: false,
};

afterEach(() => {
  vi.unstubAllGlobals();
  document.cookie = "quiz_operator_csrf=; Max-Age=0; Path=/";
});

describe("operator final round API", () => {
  it.each([null, roundId])("指定状況%sを運営Cookieの固定endpointから読み取る", async (finalRoundId) => {
    const value = { event_id: eventId, event_version: 5, final_round_id: finalRoundId };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(value)));
    vi.stubGlobal("fetch", fetchMock);
    const signal = new AbortController().signal;
    await expect(getOperatorFinalRound(signal)).resolves.toEqual(value);
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/operator/final-round",
      expect.objectContaining({ credentials: "same-origin", signal }));
  });

  it.each([true, false])("最終指定%sをEvent版・冪等キー・CSRF付きで送る", async (isFinal) => {
    document.cookie = "quiz_operator_csrf=test-final-round-csrf; Path=/";
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(response)));
    vi.stubGlobal("fetch", fetchMock);
    const signal = new AbortController().signal;
    await expect(setOperatorFinalRound(roundId, isFinal, mutation, signal)).resolves.toEqual(response);
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/operator/final-round", expect.objectContaining({
      method: "POST", credentials: "same-origin", signal,
      body: JSON.stringify({ ...mutation, round_id: roundId, is_final: isFinal }),
    }));
    expect(new Headers(fetchMock.mock.calls[0]![1].headers).get("X-CSRF-Token")).toBe("test-final-round-csrf");
  });

  it("指定状況はUUID・正整数版・必須nullable・余分なfieldなしで検証する", () => {
    const valid = { event_id: eventId, event_version: 5, final_round_id: null };
    for (const invalid of [
      { ...valid, event_id: "not-uuid" }, { ...valid, event_version: 0 }, { ...valid, event_version: "5" },
      { event_id: eventId, event_version: 5 }, { ...valid, final_round_id: "unknown" }, { ...valid, debug: "private" },
    ]) expect(operatorFinalRoundSchema.safeParse(invalid).success).toBe(false);
    expect(operatorFinalRoundMutationSchema.safeParse({ ...mutation, round_id: roundId, is_final: "true" }).success).toBe(false);
    expect(operatorFinalRoundMutationSchema.safeParse({ ...mutation, round_id: roundId, is_final: true, reason: "旧入力" }).success).toBe(false);
  });

  it("不正入力とCSRF不足を通信前に拒否する", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    expect(() => setOperatorFinalRound("invalid", true, mutation)).toThrow();
    expect(() => setOperatorFinalRound(roundId, true, mutation)).toThrow("CSRF");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("401を呼出元へ返してログイン遷移を可能にする", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ code: "operator_session_invalid", message: "expired" }), { status: 401 })));
    await expect(getOperatorFinalRound()).rejects.toBeInstanceOf(ApiResponseError);
  });
});
