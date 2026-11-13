import { afterEach, describe, expect, it, vi } from "vitest";

import { awardCeremonyMutationSchema, setAwardCeremony } from "./operatorAwardCeremonyClient";
import { operatorStateSchema } from "./operatorContracts";
import { finalRoundProjection } from "../quiz/operator/final-round/finalRoundTestFixtures";

const mutation = { expected_version: 5, idempotency_key: "79000000-0000-4000-8000-000000000003" };
const response = {
  operation: "set_award_ceremony", event_id: "79000000-0000-4000-8000-000000000001", event_version: 6,
  round_id: null, round_version: null, round_state: null,
  result_visibility: { distribution: false, personal_ranking: false, overall_ranking: false },
  open_at: null, standard_close_at: null, valid_answer_count: null, ranking_row_count: null, is_replay: false,
};

afterEach(() => {
  vi.unstubAllGlobals();
  document.cookie = "quiz_operator_csrf=; Max-Age=0; Path=/";
});

describe("表彰操作API契約", () => {
  it.each(["Announced", "Finished"] as const)("%sを版・冪等キー・CSRFと送る", async (phase) => {
    document.cookie = "quiz_operator_csrf=test-award-csrf; Path=/";
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(response)));
    vi.stubGlobal("fetch", fetchMock);
    const signal = new AbortController().signal;
    await expect(setAwardCeremony(phase, mutation, signal)).resolves.toEqual(response);
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/operator/award-ceremony", expect.objectContaining({
      method: "POST", credentials: "same-origin", signal, body: JSON.stringify({ ...mutation, phase }),
    }));
    expect(new Headers(fetchMock.mock.calls[0]![1].headers).get("X-CSRF-Token")).toBe("test-award-csrf");
  });

  it("不正な段階・余分なfieldとCSRF不足は送信しない", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    for (const phase of ["Waiting", "unknown", null, true]) {
      expect(awardCeremonyMutationSchema.safeParse({ ...mutation, phase }).success).toBe(false);
    }
    expect(awardCeremonyMutationSchema.safeParse({ ...mutation, phase: "Announced", reason: "旧入力" }).success).toBe(false);
    expect(() => setAwardCeremony("Announced", mutation)).toThrow("CSRF");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("終了済みだけ表彰状態を必須とする", () => {
    const state = finalRoundProjection();
    expect(operatorStateSchema.safeParse(state).success).toBe(true);
    expect(operatorStateSchema.safeParse({ ...state, award_ceremony_phase: undefined }).success).toBe(false);
    expect(operatorStateSchema.safeParse({ ...state, event_state: "Completed" }).success).toBe(false);
    expect(operatorStateSchema.safeParse({ ...state, award_ceremony_phase: "Waiting" }).success).toBe(false);
    for (const phase of ["Waiting", "Announced", "Finished"]) {
      expect(operatorStateSchema.safeParse({ ...state, event_state: "Completed", award_ceremony_phase: phase }).success).toBe(true);
    }
  });
});
