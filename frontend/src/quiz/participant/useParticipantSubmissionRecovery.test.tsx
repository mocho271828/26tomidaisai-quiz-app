import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AnswerStatus } from "../../api/contracts";
import { ApiContractError, ApiResponseError, ApiTransportError } from "../../api/client";
import { cancellationReason } from "../../api/cancellation";
import type { ParticipantSubmission } from "./participantUi";
import { useParticipantSubmissionRecovery } from "./useParticipantSubmissionRecovery";

const api = vi.hoisted(() => ({ getAnswerStatus: vi.fn() }));
vi.mock("../../api/audienceClient", () => api);

const target: ParticipantSubmission = {
  clientElapsedMs: 2000, timingBasis: "client_monotonic_v1",
  roundId: "60000000-0000-4000-8000-000000000001",
  clientAnswerId: "60000000-0000-4000-8000-000000000002",
  optionId: "60000000-0000-4000-8000-000000000003",
  roundVersion: 3, receivedAt: null,
  status: "unknown", message: "保存状態を確認しています。",
};
const notStored: AnswerStatus = { status: "NotStored", client_answer_id: target.clientAnswerId };
const accepted: AnswerStatus = {
  status: "AcceptedLocked",
  answer: {
    client_elapsed_ms: 2000, timing_basis: "client_monotonic_v1",
    answer_id: "60000000-0000-4000-8000-000000000004",
    round_id: target.roundId, client_answer_id: target.clientAnswerId, option_id: target.optionId,
    received_at: "2026-08-08T01:00:03.500Z",
  },
};

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

function setup() {
  const context = { generation: 1, roundId: target.roundId, roundNumber: 1,
    submission: { ...target } as ParticipantSubmission | null };
  const update = vi.fn((next: ParticipantSubmission) => { context.submission = next; });
  const hook = renderHook(() => useParticipantSubmissionRecovery(() => context, update));
  return { ...hook, context, update };
}

async function start(result: ReturnType<typeof setup>["result"]): Promise<ReturnType<typeof result.current.verifySubmission>[]> {
  let flight: ReturnType<typeof result.current.verifySubmission> = Promise.resolve("unresolved");
  await act(async () => { flight = result.current.verifySubmission(target); });
  // async関数にflight自体を返すと、未完了GETをここで待ってしまう。
  return [flight];
}

describe("participant submission recovery", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.useFakeTimers();
    vi.spyOn(Math, "random").mockReturnValue(0);
    window.sessionStorage.clear();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("NotStoredの後のcommitを同じIDのGETで確認し、受付済みにする", async () => {
    api.getAnswerStatus.mockResolvedValueOnce(notStored).mockResolvedValueOnce(accepted);
    const { result, context, update } = setup();
    const [flight] = await start(result);
    expect(context.submission?.status).toBe("not-stored");
    await act(async () => { await vi.advanceTimersByTimeAsync(250); });
    expect(await flight).toBe("accepted");
    expect(context.submission?.status).toBe("accepted");
    expect(update).toHaveBeenCalledTimes(2);
    expect(api.getAnswerStatus.mock.calls.map(([id]) => id)).toEqual([target.clientAnswerId, target.clientAnswerId]);
  });

  it("永久NotStoredは最大6照会で止まり、手動同一回答再送用の状態を保つ", async () => {
    api.getAnswerStatus.mockResolvedValue(notStored);
    const { result, context } = setup();
    const [flight] = await start(result);
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(await flight).toBe("unresolved");
    expect(api.getAnswerStatus).toHaveBeenCalledTimes(6);
    expect(context.submission?.status).toBe("not-stored");
    expect(context.submission?.clientAnswerId).toBe(target.clientAnswerId);
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(api.getAnswerStatus).toHaveBeenCalledTimes(6);
  });

  it("遅いGETも総10秒でabortし、deadline後の応答では受付済みにしない", async () => {
    let signal: AbortSignal | undefined;
    api.getAnswerStatus.mockImplementation((_id: string, candidate: AbortSignal) => {
      signal = candidate;
      return new Promise<AnswerStatus>((_resolve, reject) => {
        candidate.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
      });
    });
    const { result, update } = setup();
    const [flight] = await start(result);
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(signal?.aborted).toBe(true);
    expect(await flight).toBe("unresolved");
    expect(api.getAnswerStatus).toHaveBeenCalledOnce();
    expect(update).not.toHaveBeenCalled();
  });

  it("同時の手動照会を進行中の回復へ合流し、GETを並列化しない", async () => {
    const response = deferred<AnswerStatus>();
    api.getAnswerStatus.mockReturnValue(response.promise);
    const { result } = setup();
    const [first] = await start(result);
    const second = result.current.verifySubmission(target);
    expect(second).toBe(first);
    expect(api.getAnswerStatus).toHaveBeenCalledOnce();
    await act(async () => { response.resolve(accepted); });
    expect(await first).toBe("accepted");
    expect(await second).toBe("accepted");
  });

  it.each([
    new ApiTransportError(new TypeError("offline")),
    new ApiResponseError(503, { code: "temporary_unavailable", message: "応答を確認できません。" }),
  ])("一時的な通信/5xx失敗も同じ有限budgetで再照会する", async (error) => {
    api.getAnswerStatus.mockRejectedValueOnce(error).mockResolvedValueOnce(accepted);
    const { result, context } = setup();
    const [flight] = await start(result);
    expect(context.submission?.status).toBe("unknown");
    await act(async () => { await vi.advanceTimersByTimeAsync(250); });
    expect(await flight).toBe("accepted");
    expect(api.getAnswerStatus).toHaveBeenCalledTimes(2);
  });

  it.each([
    new ApiResponseError(404, { code: "answer_not_found", message: "回答を確認できません。" }),
    new ApiContractError("invalid schema"),
  ])("4xx/契約検証失敗は再照会せず止める", async (error) => {
    api.getAnswerStatus.mockRejectedValue(error);
    const { result, context } = setup();
    const [flight] = await start(result);
    expect(await flight).toBe("unresolved");
    expect(context.submission?.status).toBe("unknown");
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(api.getAnswerStatus).toHaveBeenCalledOnce();
  });

  it("401は追加GETをせずPageのsession再照会へ戻す", async () => {
    api.getAnswerStatus.mockRejectedValue(new ApiResponseError(401, {
      code: "participant_authentication_required", message: "セッションを確認してください。",
    }));
    const { result } = setup();
    const [flight] = await start(result);
    expect(await flight).toBe("unauthorized");
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(api.getAnswerStatus).toHaveBeenCalledOnce();
  });

  it.each(["round", "session"] as const)("%s切替で旧照会をabortし、遅い成功を適用しない", async (change) => {
    const response = deferred<AnswerStatus>();
    api.getAnswerStatus.mockReturnValue(response.promise);
    const { result, context, update } = setup();
    const [flight] = await start(result);
    if (change === "round") context.roundId = "60000000-0000-4000-8000-000000000005";
    else context.generation += 1;
    act(() => result.current.cancelRecovery());
    expect(api.getAnswerStatus.mock.calls[0]?.[1].aborted).toBe(true);
    await act(async () => { response.resolve(accepted); });
    expect(await flight).toBe("unresolved");
    expect(update).not.toHaveBeenCalled();
  });

  it("unmountで照会と待機を中断し、遅い応答では状態を更新しない", async () => {
    const response = deferred<AnswerStatus>();
    api.getAnswerStatus.mockReturnValue(response.promise);
    const { result, update, unmount } = setup();
    const [flight] = await start(result);
    unmount();
    expect(api.getAnswerStatus.mock.calls[0]?.[1].aborted).toBe(true);
    await act(async () => { response.resolve(accepted); });
    expect(await flight).toBe("unresolved");
    expect(update).not.toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(api.getAnswerStatus).toHaveBeenCalledOnce();
  });

  it.each(["auth_invalidated", "revision_changed", "result_received", undefined] as const)(
    "%sによる取消理由を保持し、不明な取消を正常差替えと推定しない", async (reason) => {
      const response = deferred<AnswerStatus>();
      api.getAnswerStatus.mockReturnValue(response.promise);
      const { result, update } = setup();
      const [flight] = await start(result);
      const signal = api.getAnswerStatus.mock.calls[0]?.[1] as AbortSignal;
      act(() => result.current.cancelRecovery(reason));
      expect(signal.aborted).toBe(true);
      expect(cancellationReason(signal)).toBe(reason);
      await act(async () => { response.resolve(accepted); });
      expect(await flight).toBe("unresolved");
      expect(update).not.toHaveBeenCalled();
    },
  );

  it("手動再送のsubmittingへ切り替えた後は旧GETの成功を適用しない", async () => {
    const response = deferred<AnswerStatus>();
    api.getAnswerStatus.mockReturnValue(response.promise);
    const { result, context, update } = setup();
    const [flight] = await start(result);
    context.submission = { ...target, status: "submitting" };
    act(() => result.current.cancelRecovery());
    await act(async () => { response.resolve(accepted); });
    expect(await flight).toBe("unresolved");
    expect(update).not.toHaveBeenCalled();
    expect(context.submission.status).toBe("submitting");
  });

  it.each(["AcceptedLocked", "NotStored"] as const)("%sの識別子不一致は再試行せず、安全な分類telemetryを残す", async (status) => {
    if (accepted.status !== "AcceptedLocked") throw new Error("invalid fixture");
    api.getAnswerStatus.mockResolvedValue(status === "AcceptedLocked"
      ? { ...accepted, answer: { ...accepted.answer, option_id: "60000000-0000-4000-8000-000000000009" } }
      : { ...notStored, client_answer_id: "60000000-0000-4000-8000-000000000009" });
    const observe = vi.fn();
    window.addEventListener("quiz:observation", observe);
    const { result, context } = setup();
    const [flight] = await start(result);
    expect(await flight).toBe("unresolved");
    expect(context.submission?.message).toBe("保存状態の識別子が一致しません。運営へ確認してください。");
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(api.getAnswerStatus).toHaveBeenCalledOnce();
    const event = observe.mock.calls[0]?.[0] as CustomEvent<unknown>;
    expect(event.detail).toMatchObject({ kind: "answer_apply", source: "status", outcome: "identity_mismatch" });
    expect(JSON.stringify(event.detail)).not.toContain(target.clientAnswerId);
    window.removeEventListener("quiz:observation", observe);
  });
});
