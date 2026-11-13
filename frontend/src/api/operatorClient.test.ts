import { afterEach, describe, expect, it, vi } from "vitest";

import {
  advanceRound,
  completeEvent,
  getOperatorRanking,
  getOperatorState,
  mutateRound,
  retryRoundResults,
  setResultVisibility,
} from "./operatorClient";

const eventId = "90000000-0000-4000-8000-000000000001";
const roundId = "90000000-0000-4000-8000-000000000002";
const questionId = "90000000-0000-4000-8000-000000000003";
const idempotencyKey = "90000000-0000-4000-8000-000000000004";

describe("operator API client", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    document.cookie = "quiz_operator_csrf=; Max-Age=0; Path=/";
  });

  it("正解公開を版・冪等キー・CSRF付きの専用endpointへ送る", async () => {
    document.cookie = "quiz_operator_csrf=test-csrf-token; Path=/";
    const response = {
      operation: "publish_round_results", event_id: eventId, event_version: 3,
      round_id: roundId, round_version: 3, round_state: "Reveal",
      result_visibility: { distribution: true, personal_ranking: true, overall_ranking: true },
      open_at: null, standard_close_at: null, valid_answer_count: 2, ranking_row_count: 2,
      is_replay: false,
    };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(response)));
    vi.stubGlobal("fetch", fetchMock);
    const mutation = { expected_version: 2, idempotency_key: idempotencyKey };
    await expect(mutateRound(roundId, "publish-results", mutation)).resolves.toEqual(response);
    expect(fetchMock).toHaveBeenCalledWith(`/api/v1/operator/rounds/${roundId}/publish-results`,
      expect.objectContaining({ method: "POST", credentials: "same-origin", body: JSON.stringify(mutation) }));
    expect(new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get("X-CSRF-Token")).toBe("test-csrf-token");
  });

  it("単一開催のstateとrankingを固定endpointから取得する", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("null", {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(getOperatorRanking()).resolves.toBeNull();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/operator/ranking",
      expect.objectContaining({ credentials: "same-origin" }),
    );

    await expect(getOperatorState()).rejects.toThrow();
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/v1/operator/state",
      expect.objectContaining({ credentials: "same-origin" }),
    );
  });

  it("次Round作成とEvent完了をCSRF付きの確定endpointへ送る", async () => {
    document.cookie = "quiz_operator_csrf=test-csrf-token; Path=/";
    const fetchMock = vi
      .fn<(path: string, init: RequestInit) => Promise<Response>>()
      .mockImplementation((path: string) =>
        Promise.resolve(
          new Response(
            JSON.stringify({
              operation: path.endsWith("/advance")
                ? "advance_round"
                : "complete_event",
              event_id: eventId,
              event_version: 2,
              round_id: roundId,
              round_version: 2,
              round_state: "RoundBackupValidated",
              result_visibility: { distribution: true, personal_ranking: false, overall_ranking: false },
              open_at: null,
              standard_close_at: null,
              valid_answer_count: null,
              ranking_row_count: null,
              is_replay: false,
            }),
            {
              status: 200,
              headers: { "Content-Type": "application/json" },
            },
          ),
        ),
      );
    vi.stubGlobal("fetch", fetchMock);
    const mutation = {
      expected_version: 1,
      idempotency_key: idempotencyKey,
    } as const;

    await advanceRound(roundId, questionId, mutation);
    await completeEvent(roundId, mutation);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      `/api/v1/operator/rounds/${roundId}/advance`,
      expect.objectContaining({
        body: JSON.stringify({ ...mutation, question_id: questionId }),
        credentials: "same-origin",
        method: "POST",
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `/api/v1/operator/rounds/${roundId}/complete-event`,
      expect.objectContaining({
        body: JSON.stringify(mutation),
        credentials: "same-origin",
        method: "POST",
      }),
    );
    for (const call of fetchMock.mock.calls) {
      const headers = new Headers(call[1]?.headers);
      expect(headers.get("X-CSRF-Token")).toBe("test-csrf-token");
    }
  });

  it.each(["overall_ranking", "rankings"] as const)("結果表示の対象%sと可否を単一開催の固定endpointへ送る", async (target) => {
    document.cookie = "quiz_operator_csrf=test-csrf-token; Path=/";
    const response = {
      operation: "set_result_visibility",
      event_id: eventId,
      event_version: 2,
      round_id: roundId,
      round_version: 1,
      round_state: "Reveal",
      result_visibility: { distribution: true, personal_ranking: true, overall_ranking: true },
      open_at: null,
      standard_close_at: null,
      valid_answer_count: null,
      ranking_row_count: null,
      is_replay: false,
    } as const;
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(response), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const mutation = {
      expected_version: 1,
      idempotency_key: idempotencyKey,
    } as const;

    await setResultVisibility(target, true, mutation);

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/operator/result-visibility",
      expect.objectContaining({
        body: JSON.stringify({ ...mutation, target, visible: true }),
        method: "POST",
      }),
    );
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get("X-CSRF-Token")).toBe("test-csrf-token");
  });

  it("自動集計の復旧操作をCSRF付きの専用endpointへ送る", async () => {
    document.cookie = "quiz_operator_csrf=test-csrf-token; Path=/";
    const response = {
      operation: "retry_round_results",
      event_id: eventId,
      event_version: 2,
      round_id: roundId,
      round_version: 2,
      round_state: "AwaitingValidation",
      result_visibility: { distribution: true, personal_ranking: false, overall_ranking: false },
      open_at: null,
      standard_close_at: null,
      valid_answer_count: null,
      ranking_row_count: null,
      is_replay: false,
    } as const;
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(response), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const mutation = {
      expected_version: 1,
      idempotency_key: idempotencyKey,
    } as const;

    await retryRoundResults(roundId, mutation);

    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/operator/rounds/${roundId}/retry-results`,
      expect.objectContaining({
        body: JSON.stringify(mutation),
        method: "POST",
      }),
    );
    const request = fetchMock.mock.calls[0]?.[1];
    expect(new Headers(request?.headers).get("X-CSRF-Token")).toBe(
      "test-csrf-token",
    );
  });
});
