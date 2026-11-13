import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiResponseError } from "../../api/client";
import type { OperatorState } from "../../api/operatorContracts";
import type { RealtimeStateMessage } from "../../api/realtimeContracts";
import type { RealtimeRefreshOptions } from "../_hooks/useRealtimeRefresh";
import { useModeratorState } from "./useModeratorState";

const api = vi.hoisted(() => ({ state: vi.fn() }));
const realtime = vi.hoisted(() => ({ options: null as RealtimeRefreshOptions | null }));
vi.mock("../../api/operatorClient", () => ({ getOperatorState: api.state }));
vi.mock("../_hooks/useRealtimeRefresh", () => ({
  useRealtimeRefresh: (options: RealtimeRefreshOptions) => { realtime.options = options; return "live"; },
}));

const id = (suffix: string): string => `77000000-0000-4000-8000-0000000000${suffix}`;
function state(): OperatorState {
  const control = { mode: "manual" as const, initially_visible: false, changes: [] };
  return {
    award_ceremony_phase: null,
    event_id: id("01"), event_version: 10, event_title: "富大祭", event_state: "InProgress",
    projected_at: "2026-10-22T03:00:00Z", start_delay_seconds: 3, participant_count: 10,
    available_questions: [], used_question_ids: [], latest_ranking_snapshot_version: 2,
    result_visibility: { distribution: false, personal_ranking: false, overall_ranking: false },
    visibility_controls: { distribution: control, personal_ranking: control, overall_ranking: control },
    current_round: {
      round_id: id("02"), version: 3, state_version: 5, state: "ReadyToReveal",
      open_at: "2026-10-22T02:59:45Z", standard_close_at: "2026-10-22T02:59:55Z",
      result_publication: "Withheld", can_publish_results: true, result_failure_code: null,
      question: { question_id: id("03"), version: 1, prompt: "現在の問題", options: [] },
      statistics: { answer_count: 0, average_response_ms: null, options: [] },
    },
  };
}
function message(value: OperatorState): RealtimeStateMessage {
  return { type: "operator.state", audience: "operator", event_id: value.event_id, stream_id: id("04"), sequence: 1, state: value };
}
function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  let reject: (reason: unknown) => void = () => undefined;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

describe("useModeratorState", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    realtime.options = null;
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    api.state.mockResolvedValue(state());
  });
  afterEach(() => vi.restoreAllMocks());

  it("初回GET後だけ運営WSを有効化し、接続のたびの重複GETを要求しない", async () => {
    const onLost = vi.fn();
    const { result } = renderHook(() => useModeratorState(onLost));
    expect(realtime.options?.enabled).toBe(false);
    await waitFor(() => expect(result.current.isFresh).toBe(true));
    expect(api.state).toHaveBeenCalledOnce();
    expect(realtime.options).toMatchObject({ enabled: true, audience: "operator", eventId: id("01"), refreshOnConnect: false });
    expect(realtime.options?.path).toMatch(/\/operator\/realtime$/);
    expect(realtime.options?.onAuthenticationLost).toBe(onLost);
  });

  it("同じ版のWS統計更新を反映し、古いcacheで巻き戻さずGETも増やさない", async () => {
    const onLost = vi.fn();
    const { result } = renderHook(() => useModeratorState(onLost));
    await waitFor(() => expect(result.current.isFresh).toBe(true));
    const updated = { ...state(), projected_at: "2026-10-22T03:00:01Z", participant_count: 20 };
    act(() => expect(realtime.options?.onState(message(updated))).toBe(true));
    act(() => expect(realtime.options?.onState(message(state()))).toBe(true));
    expect(result.current.projection?.participant_count).toBe(20);
    expect(api.state).toHaveBeenCalledOnce();
  });

  it.each(["event", "round", "version"] as const)("切断後の不一致WS %sで画面を復帰しない", async (boundary) => {
    const onLost = vi.fn();
    const { result } = renderHook(() => useModeratorState(onLost));
    await waitFor(() => expect(result.current.isFresh).toBe(true));
    act(() => realtime.options?.onUnavailable?.());
    const candidate = state();
    if (boundary === "event") candidate.event_id = id("90");
    if (boundary === "round") candidate.current_round = { ...candidate.current_round!, round_id: id("91") };
    if (boundary === "version") candidate.event_version -= 1;
    act(() => expect(realtime.options?.onState(message(candidate))).toBe(false));
    expect(result.current.isFresh).toBe(false);
    expect(result.current.projection).toEqual(state());
    expect(api.state).toHaveBeenCalledOnce();
  });

  it("再確認GET待ちのWSは新しい状態を反映しても確認済みにしない", async () => {
    const onLost = vi.fn();
    const { result } = renderHook(() => useModeratorState(onLost));
    await waitFor(() => expect(result.current.isFresh).toBe(true));
    act(() => realtime.options?.onUnavailable?.());
    const next = { ...state(), event_version: 11 };
    act(() => expect(realtime.options?.onState(message(next), true)).toBe(true));
    expect(result.current.projection?.event_version).toBe(11);
    expect(result.current.isFresh).toBe(false);
  });

  it.each(["older", "foreign"] as const)("遅いHTTP %sで新しいWSを巻き戻さず、鮮度確認も成立させない", async (boundary) => {
    const onLost = vi.fn();
    const { result } = renderHook(() => useModeratorState(onLost));
    await waitFor(() => expect(result.current.isFresh).toBe(true));
    const pending = deferred<OperatorState>();
    api.state.mockReturnValueOnce(pending.promise);
    let refresh: Promise<boolean> | undefined;
    await act(async () => { refresh = result.current.refresh(); });
    act(() => realtime.options?.onState(message({ ...state(), event_version: 11 })));
    act(() => realtime.options?.onUnavailable?.());
    const stale = boundary === "older" ? state() : { ...state(), event_id: id("90") };
    await act(async () => { pending.resolve(stale); await refresh; });
    expect(result.current.projection?.event_version).toBe(11);
    expect(result.current.isFresh).toBe(false);
  });

  it("非表示では初回GETを保留し、表示復帰で開始する", async () => {
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    const onLost = vi.fn();
    const { result } = renderHook(() => useModeratorState(onLost));
    await act(async () => undefined);
    expect(api.state).not.toHaveBeenCalled();
    visibility.mockReturnValue("visible");
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await waitFor(() => expect(result.current.isFresh).toBe(true));
    expect(api.state).toHaveBeenCalledOnce();
  });

  it("非表示前の応答を復帰確認に使わず、新しい取得を先に進める", async () => {
    const first = deferred<OperatorState>();
    api.state.mockReturnValueOnce(first.promise);
    const visibility = vi.spyOn(document, "visibilityState", "get");
    const onLost = vi.fn();
    const { result } = renderHook(() => useModeratorState(onLost));
    await waitFor(() => expect(api.state).toHaveBeenCalledOnce());
    const firstSignal: AbortSignal = api.state.mock.calls[0]![0];
    act(() => { visibility.mockReturnValue("hidden"); document.dispatchEvent(new Event("visibilitychange")); });
    expect(result.current.isFresh).toBe(false);
    const next = { ...state(), event_version: 11 };
    api.state.mockResolvedValueOnce(next);
    act(() => { visibility.mockReturnValue("visible"); document.dispatchEvent(new Event("visibilitychange")); });
    await waitFor(() => expect(result.current.isFresh).toBe(true));
    expect(firstSignal.aborted).toBe(true);
    await act(async () => { first.resolve(state()); await first.promise; });
    expect(result.current.projection?.event_version).toBe(11);
    expect(api.state).toHaveBeenCalledTimes(2);
  });

  it("同じ世代のretry連打を1要求へまとめ、失敗後の手動retryで回復する", async () => {
    api.state.mockRejectedValueOnce(new Error("temporary"));
    const onLost = vi.fn();
    const { result } = renderHook(() => useModeratorState(onLost));
    await waitFor(() => expect(result.current.error).not.toBeNull());
    const pending = deferred<OperatorState>();
    api.state.mockReturnValueOnce(pending.promise);
    let first: Promise<boolean> | undefined;
    let second: Promise<boolean> | undefined;
    await act(async () => { first = result.current.refresh(); second = result.current.refresh(); });
    expect(first).toBe(second);
    expect(api.state).toHaveBeenCalledTimes(2);
    await act(async () => { pending.resolve(state()); await first; });
    expect(result.current.isFresh).toBe(true);
    expect(result.current.error).toBeNull();
  });

  it("5秒の期限切れは鮮度を無効にし、GETを自動連打しない", async () => {
    const deadline = new AbortController();
    const timeout = vi.spyOn(AbortSignal, "timeout").mockReturnValue(deadline.signal);
    api.state.mockImplementationOnce((signal: AbortSignal) => new Promise<OperatorState>((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(signal.reason), { once: true });
    }));
    const onLost = vi.fn();
    const { result } = renderHook(() => useModeratorState(onLost));
    await waitFor(() => expect(api.state).toHaveBeenCalledOnce());
    expect(timeout).toHaveBeenCalledWith(5000);
    await act(async () => deadline.abort(new DOMException("deadline", "TimeoutError")));
    expect(result.current.isFresh).toBe(false);
    expect(result.current.error).not.toBeNull();
    expect(api.state).toHaveBeenCalledOnce();
    expect(onLost).not.toHaveBeenCalled();
  });

  it("現在の401だけを認証失効として扱い、unmount後の遅い401を無視する", async () => {
    const unauthorized = new ApiResponseError(401, { code: "operator_session_invalid", message: "expired" });
    api.state.mockRejectedValueOnce(unauthorized);
    const onLost = vi.fn();
    const { result, unmount } = renderHook(() => useModeratorState(onLost));
    await waitFor(() => expect(onLost).toHaveBeenCalledOnce());
    expect(result.current.isFresh).toBe(false);
    const pending = deferred<OperatorState>();
    api.state.mockReturnValueOnce(pending.promise);
    await act(async () => { void result.current.refresh(); });
    const signal: AbortSignal = api.state.mock.calls[1]![0];
    unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => { pending.reject(unauthorized); await pending.promise.catch(() => undefined); });
    expect(onLost).toHaveBeenCalledOnce();
  });
});
