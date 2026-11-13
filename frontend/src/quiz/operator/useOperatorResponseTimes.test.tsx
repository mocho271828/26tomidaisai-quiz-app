import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { OperatorResponseTimes } from "../../api/operatorResponseTimeContracts";
import { useOperatorResponseTimes } from "./useOperatorResponseTimes";

const api = vi.hoisted(() => ({ getOperatorResponseTimes: vi.fn() }));
vi.mock("../../api/operatorResponseTimeClient", () => api);

const roundId = "60000000-0000-4000-8000-000000000001";
const otherRoundId = "60000000-0000-4000-8000-000000000002";
const emptyGroup = {
  answer_count: 0, average_response_ms: null, median_response_ms: null,
  fastest_response_ms: null, slowest_response_ms: null, rows: [],
};
const data: OperatorResponseTimes = {
  timing_basis: "client_monotonic_v1",
  rounds: [{ round_id: roundId, round_number: 1, is_practice: true, prompt: "練習" }],
  selected_round_id: roundId,
  all_answers: emptyGroup, correct_answers: emptyGroup,
};

function deferred() {
  let resolve: (value: OperatorResponseTimes) => void = () => undefined;
  const promise = new Promise<OperatorResponseTimes>((done) => { resolve = done; });
  return { promise, resolve };
}

describe("useOperatorResponseTimes", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    api.getOperatorResponseTimes.mockResolvedValue(data);
  });
  afterEach(() => vi.restoreAllMocks());

  it("確定前は取得せず、同じsnapshotの再描画では再取得しない", async () => {
    const { result, rerender } = renderHook(({ version }: { version: number | null }) =>
      useOperatorResponseTimes(version), { initialProps: { version: null as number | null } });
    expect(result.current.status).toBe("idle");
    expect(api.getOperatorResponseTimes).not.toHaveBeenCalled();
    rerender({ version: 1 });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    rerender({ version: 1 });
    expect(api.getOperatorResponseTimes).toHaveBeenCalledOnce();
    rerender({ version: 2 });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(api.getOperatorResponseTimes).toHaveBeenCalledTimes(2);
  });

  it("非表示中は取得を保留し、表示復帰で1度だけ取得する", async () => {
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    const { result } = renderHook(() => useOperatorResponseTimes(1));
    expect(api.getOperatorResponseTimes).not.toHaveBeenCalled();
    visibility.mockReturnValue("visible");
    await act(async () => document.dispatchEvent(new Event("visibilitychange")));
    await waitFor(() => expect(result.current.status).toBe("ready"));
    await act(async () => document.dispatchEvent(new Event("visibilitychange")));
    expect(api.getOperatorResponseTimes).toHaveBeenCalledOnce();
  });

  it("問題の変更で前の通信を中止し、遅れて届く旧応答を破棄する", async () => {
    const old = deferred();
    api.getOperatorResponseTimes.mockReturnValueOnce(old.promise);
    const { result } = renderHook(() => useOperatorResponseTimes(1));
    const firstSignal = api.getOperatorResponseTimes.mock.calls[0]![1] as AbortSignal;
    api.getOperatorResponseTimes.mockResolvedValue({ ...data, selected_round_id: otherRoundId, });
    act(() => {
      result.current.selectRound(otherRoundId);
    });
    expect(firstSignal.aborted).toBe(true);
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(api.getOperatorResponseTimes).toHaveBeenLastCalledWith(otherRoundId, expect.any(AbortSignal));
    await act(async () => old.resolve(data));
    expect(result.current.data?.selected_round_id).toBe(otherRoundId);
  });

  it("失敗時と選択条件不一致はエラーを表示し、明示再試行で回復する", async () => {
    api.getOperatorResponseTimes.mockRejectedValueOnce(new Error("通信失敗"));
    const { result } = renderHook(() => useOperatorResponseTimes(1));
    await waitFor(() => expect(result.current.status).toBe("error"));
    api.getOperatorResponseTimes.mockResolvedValueOnce({ ...data, selected_round_id: otherRoundId });
    act(() => result.current.selectRound(roundId));
    await waitFor(() => expect(result.current.status).toBe("error"));
    act(() => result.current.retry());
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(api.getOperatorResponseTimes).toHaveBeenCalledTimes(3);
  });

  it("snapshotの解除とunmountで通信を中止し、結果を残さない", async () => {
    const pending = deferred();
    api.getOperatorResponseTimes.mockReturnValue(pending.promise);
    const { result, rerender, unmount } = renderHook(({ version }: { version: number | null }) =>
      useOperatorResponseTimes(version), { initialProps: { version: 1 as number | null } });
    const firstSignal = api.getOperatorResponseTimes.mock.calls[0]![1] as AbortSignal;
    rerender({ version: null });
    await act(async () => pending.resolve(data));
    expect(firstSignal.aborted).toBe(true);
    expect(result.current.status).toBe("idle");
    expect(result.current.data).toBeNull();
    rerender({ version: 2 });
    const lastSignal = api.getOperatorResponseTimes.mock.calls.at(-1)![1] as AbortSignal;
    unmount();
    expect(lastSignal.aborted).toBe(true);
  });
});
