import { StrictMode } from "react";
import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useSingleFlightRefresh } from "./useSingleFlightRefresh";

function deferred(): { promise: Promise<boolean>; resolve: (value: boolean) => void } {
  let resolvePromise: (value: boolean) => void = () => undefined;
  const promise = new Promise<boolean>((resolve) => { resolvePromise = resolve; });
  return { promise, resolve: resolvePromise };
}

describe("全状態取得経路の集約", () => {
  it("取得中の通知と操作後更新を1件の後続へ畳み、最後の結果まで待つ", async () => {
    const first = deferred();
    const second = deferred();
    const fetchState = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const view = renderHook(() => useSingleFlightRefresh(fetchState));
    const initial = view.result.current();
    await act(async () => { await Promise.resolve(); });
    const notified = view.result.current();
    const afterMutation = view.result.current();
    expect(notified).toBe(initial);
    expect(afterMutation).toBe(initial);
    expect(fetchState).toHaveBeenCalledOnce();
    await act(async () => { first.resolve(true); await Promise.resolve(); });
    expect(fetchState).toHaveBeenCalledTimes(2);
    second.resolve(false);
    await expect(initial).resolves.toBe(false);
    expect(fetchState).toHaveBeenCalledTimes(2);
  });

  it("unmountで進行中取得をabortしpending取得を送らない", async () => {
    const first = deferred();
    const fetchState = vi.fn().mockReturnValue(first.promise);
    const view = renderHook(() => useSingleFlightRefresh(fetchState));
    const flight = view.result.current();
    await act(async () => { await Promise.resolve(); });
    void view.result.current();
    const signal = fetchState.mock.calls[0]?.[0] as AbortSignal;
    view.unmount();
    expect(signal.aborted).toBe(true);
    first.resolve(false);
    await flight;
    expect(fetchState).toHaveBeenCalledOnce();
  });

  it("StrictModeの再mountでもrefreshを使用できる", async () => {
    const fetchState = vi.fn().mockResolvedValue(true);
    const view = renderHook(() => useSingleFlightRefresh(fetchState), { wrapper: StrictMode });
    await expect(view.result.current()).resolves.toBe(true);
    expect(fetchState).toHaveBeenCalledOnce();
  });

  it("同世代の参加者取得は後続GETを増やさず同じflightを共有する", async () => {
    const first = deferred();
    const fetchState = vi.fn().mockReturnValue(first.promise);
    const view = renderHook(() => useSingleFlightRefresh(fetchState, () => "session:1"));
    const initial = view.result.current();
    await act(async () => { await Promise.resolve(); });
    expect(view.result.current()).toBe(initial);
    first.resolve(true);
    await expect(initial).resolves.toBe(true);
    expect(fetchState).toHaveBeenCalledOnce();
  });

  it("世代変更で旧取得をabortして即座に取得し、旧finallyは新flightを消さない", async () => {
    const previous = deferred();
    const resumed = deferred();
    let generation = "session:1";
    const fetchState = vi.fn().mockReturnValueOnce(previous.promise).mockReturnValueOnce(resumed.promise);
    const view = renderHook(() => useSingleFlightRefresh(fetchState, () => generation));
    const oldFlight = view.result.current();
    await act(async () => { await Promise.resolve(); });
    const oldSignal = fetchState.mock.calls[0]?.[0] as AbortSignal;
    generation = "session:2";
    const newFlight = view.result.current();
    await act(async () => { await Promise.resolve(); });
    expect(oldSignal.aborted).toBe(true);
    expect(fetchState).toHaveBeenCalledTimes(2);
    previous.resolve(true);
    await expect(oldFlight).resolves.toBe(false);
    expect(view.result.current()).toBe(newFlight);
    expect(fetchState).toHaveBeenCalledTimes(2);
    const newSignal = fetchState.mock.calls[1]?.[0] as AbortSignal;
    expect(newSignal.aborted).toBe(false);
    resumed.resolve(true);
    await expect(newFlight).resolves.toBe(true);
  });
});
