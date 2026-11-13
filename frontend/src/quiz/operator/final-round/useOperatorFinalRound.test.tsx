import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiResponseError } from "../../../api/client";
import type { OperatorFinalRound } from "../../../api/operatorFinalRoundClient";
import { finalRoundIds, finalRoundProjection } from "./finalRoundTestFixtures";
import { useOperatorFinalRound } from "./useOperatorFinalRound";

const api = vi.hoisted(() => ({ get: vi.fn(), set: vi.fn() }));
vi.mock("../../../api/operatorFinalRoundClient", () => ({ getOperatorFinalRound: api.get, setOperatorFinalRound: api.set }));
const initial: OperatorFinalRound = { event_id: finalRoundIds.event, event_version: 5, final_round_id: null };
function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  let reject: (reason: unknown) => void = () => undefined;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function context(): Parameters<typeof useOperatorFinalRound>[0] {
  return { projection: finalRoundProjection(),
    mutationInFlightRef: { current: false }, mutationKeysRef: { current: new Map() }, sessionGenerationRef: { current: 1 },
    refresh: vi.fn().mockResolvedValue(true), onAuthenticationLost: vi.fn(), onBusy: vi.fn(), onError: vi.fn(), onMessage: vi.fn() };
}

describe("useOperatorFinalRound", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    api.get.mockResolvedValue(initial);
    api.set.mockResolvedValue({ event_version: 6 });
  });
  afterEach(() => vi.restoreAllMocks());

  it("projectionがなければ取得せず、統計更新だけでGETを増やさない", async () => {
    const controls = context();
    const { result, rerender } = renderHook(useOperatorFinalRound, { initialProps: { ...controls, projection: null } as typeof controls });
    expect(result.current.status).toBe("idle");
    expect(api.get).not.toHaveBeenCalled();
    rerender(controls);
    await waitFor(() => expect(result.current.status).toBe("ready"));
    rerender({ ...controls, projection: { ...finalRoundProjection(), participant_count: 12, projected_at: "2026-10-22T03:00:01Z" } });
    expect(api.get).toHaveBeenCalledOnce();
  });

  it.each([{ ...initial, event_id: finalRoundIds.question }, { ...initial, event_version: 4 }])("開催・版の不一致を未指定として採用しない", async (value) => {
    api.get.mockResolvedValue(value);
    const { result } = renderHook(useOperatorFinalRound, { initialProps: context() });
    await waitFor(() => expect(result.current.status).toBe("error"));
    expect(result.current.value).toBeNull();
    await act(async () => result.current.onChange(true));
    expect(api.set).not.toHaveBeenCalled();
  });

  it("非表示中は最新Event版の取得を保留し、復帰時だけ取得する", async () => {
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    const controls = context();
    const { result, rerender } = renderHook(useOperatorFinalRound, { initialProps: controls });
    rerender({ ...controls, projection: { ...finalRoundProjection(), event_version: 6 } });
    expect(api.get).not.toHaveBeenCalled();
    api.get.mockResolvedValue({ ...initial, event_version: 6, final_round_id: finalRoundIds.round });
    visibility.mockReturnValue("visible");
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(api.get).toHaveBeenCalledOnce();
    expect(result.current.value?.final_round_id).toBe(finalRoundIds.round);
  });

  it("新しいEvent版では古い指定を隠し、遅い応答で巻き戻さない", async () => {
    const pending = deferred<OperatorFinalRound>();
    api.get.mockReturnValueOnce(pending.promise);
    const controls = context();
    const { result, rerender } = renderHook(useOperatorFinalRound, { initialProps: controls });
    const oldSignal: AbortSignal = api.get.mock.calls[0]![0];
    api.get.mockResolvedValue({ ...initial, event_version: 6, final_round_id: finalRoundIds.round });
    rerender({ ...controls, projection: { ...finalRoundProjection(), event_version: 6 } });
    expect(result.current.value).toBeNull();
    expect(oldSignal.aborted).toBe(true);
    await waitFor(() => expect(result.current.status).toBe("ready"));
    await act(async () => { pending.resolve(initial); await pending.promise; });
    expect(result.current.value?.final_round_id).toBe(finalRoundIds.round);
  });

  it("401は指定内容を表示せず認証失効として通知する", async () => {
    api.get.mockRejectedValue(new ApiResponseError(401, { code: "operator_session_invalid", message: "expired" }));
    const controls = context();
    const { result } = renderHook(useOperatorFinalRound, { initialProps: controls });
    await waitFor(() => expect(controls.onAuthenticationLost).toHaveBeenCalledOnce());
    expect(result.current.value).toBeNull();
    expect(controls.onError).not.toHaveBeenCalled();
  });

  it("5秒の取得期限と明示retryを持ち、失敗時に自動連打しない", async () => {
    const deadline = new AbortController();
    const timeout = vi.spyOn(AbortSignal, "timeout").mockReturnValue(deadline.signal);
    api.get.mockImplementationOnce((signal: AbortSignal) => new Promise<OperatorFinalRound>((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(signal.reason), { once: true });
    }));
    const { result } = renderHook(useOperatorFinalRound, { initialProps: context() });
    expect(timeout).toHaveBeenCalledWith(5000);
    await act(async () => deadline.abort(new DOMException("deadline", "TimeoutError")));
    expect(result.current.status).toBe("error");
    expect(api.get).toHaveBeenCalledOnce();
    timeout.mockRestore();
    act(() => result.current.retry());
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(api.get).toHaveBeenCalledTimes(2);
  });

  it("既存進行操作とlockを共有し、通信失敗後だけ同じ冪等キーで再試行する", async () => {
    const controls = context();
    const { result } = renderHook(useOperatorFinalRound, { initialProps: controls });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    controls.mutationInFlightRef.current = true;
    await act(async () => result.current.onChange(true));
    expect(api.set).not.toHaveBeenCalled();
    controls.mutationInFlightRef.current = false;
    api.set.mockRejectedValueOnce(new ApiResponseError(503, { code: "temporarily_unavailable", message: "unavailable" }));
    await act(async () => result.current.onChange(true));
    expect(controls.mutationInFlightRef.current).toBe(false);
    expect(controls.onBusy).toHaveBeenLastCalledWith(false);
    const firstKey = api.set.mock.calls[0]![2].idempotency_key;
    await act(async () => result.current.onChange(true));
    expect(api.set.mock.calls[1]![2]).toEqual({ expected_version: 5, idempotency_key: firstKey });
    expect(controls.onMessage).toHaveBeenLastCalledWith("この問題を最終問題に設定しました。");
    expect(controls.mutationKeysRef.current.size).toBe(0);
  });

  it("別問題の指定を解除せず、終了後の変更も拒否する", async () => {
    const controls = context();
    const { result, rerender } = renderHook(useOperatorFinalRound, { initialProps: controls });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    await act(async () => result.current.onChange(false));
    rerender({ ...controls, projection: { ...finalRoundProjection(), event_state: "Completed" } });
    await act(async () => result.current.onChange(true));
    expect(api.set).not.toHaveBeenCalled();
  });
});
