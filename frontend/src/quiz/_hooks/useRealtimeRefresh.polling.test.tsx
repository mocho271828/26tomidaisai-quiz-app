import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  changeVisibility, control, deferred, eventId, FakeWebSocket, snapshot, type Refresh,
} from "./realtimeRefreshTestUtils";
import { useRealtimeRefresh } from "./useRealtimeRefresh";

function renderRealtime(refresh: Refresh) {
  return renderHook(() => useRealtimeRefresh({
    audience: "participant", enabled: true, eventId, path: "/api/v1/participant/realtime",
    refresh, refreshOnConnect: false, onState: () => true,
  }));
}

describe("useRealtimeRefresh fallback scheduling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.spyOn(Math, "random").mockReturnValue(0.5);
    FakeWebSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket);
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("5秒ごとの通知が継続する間は定期GETを送らない", async () => {
    const refresh = vi.fn<Refresh>().mockResolvedValue(true);
    const { result } = renderRealtime(refresh);
    const socket = FakeWebSocket.instances[0];
    await act(async () => { socket?.open(); socket?.message(snapshot()); });
    for (let sequence = 2; sequence <= 13; sequence += 1) {
      await act(async () => vi.advanceTimersByTimeAsync(5_000));
      await act(async () => socket?.message(control("heartbeat", sequence)));
    }
    expect(result.current).toBe("live");
    expect(refresh).not.toHaveBeenCalled();
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it("旧GET未完了での復帰は新GETを直ちに開始し、旧成功やfinallyで回復扱いにしない", async () => {
    const old = deferred<boolean>();
    const resumed = deferred<boolean>();
    const refresh = vi.fn<Refresh>().mockReturnValueOnce(old.promise).mockReturnValueOnce(resumed.promise).mockResolvedValue(false);
    const { result } = renderRealtime(refresh);
    const socket = FakeWebSocket.instances[0];
    await act(async () => { socket?.open(); socket?.message(snapshot()); });
    await act(async () => {
      socket?.message(control("session.changed", 2));
      changeVisibility("hidden");
      changeVisibility("visible");
    });
    expect(refresh).toHaveBeenCalledTimes(2);
    // 旧成功が完了した後も、新flightへ合流して余分なGETを増やさない。
    await act(async () => old.resolve(true));
    await act(async () => socket?.message(control("session.changed", 3)));
    expect(refresh).toHaveBeenCalledTimes(2);
    await act(async () => resumed.resolve(false));
    expect(refresh).toHaveBeenCalledTimes(3);
    expect(result.current).toBe("polling");
  });

  it("復帰前のHTTP成功は、復帰後のHTTPが未完了のpollingを解除しない", async () => {
    const old = deferred<boolean>();
    const resumed = deferred<boolean>();
    const refresh = vi.fn<Refresh>().mockReturnValueOnce(old.promise).mockReturnValueOnce(resumed.promise);
    const { result } = renderRealtime(refresh);
    const socket = FakeWebSocket.instances[0];
    await act(async () => socket?.serverClose(1013));
    await act(async () => vi.advanceTimersByTimeAsync(750));
    const reconnected = FakeWebSocket.instances[1];
    await act(async () => { reconnected?.open(); changeVisibility("hidden"); reconnected?.message(snapshot()); });
    await act(async () => changeVisibility("visible"));
    expect(refresh).toHaveBeenCalledTimes(2);
    expect(result.current).toBe("polling");
    await act(async () => old.resolve(true));
    expect(result.current).toBe("polling");
    await act(async () => resumed.resolve(true));
    expect(result.current).toBe("live");
  });

  it("古い復帰GETの失敗は、新しい復帰GET成功後の回答可能状態を取り消さない", async () => {
    const old = deferred<boolean>();
    const resumed = deferred<boolean>();
    const refresh = vi.fn<Refresh>().mockReturnValueOnce(old.promise).mockReturnValueOnce(resumed.promise);
    const { result } = renderRealtime(refresh);
    const socket = FakeWebSocket.instances[0];
    await act(async () => { socket?.open(); socket?.message(snapshot()); });
    await act(async () => { changeVisibility("hidden"); changeVisibility("visible"); });
    await act(async () => { changeVisibility("hidden"); changeVisibility("visible"); });
    expect(refresh).toHaveBeenCalledTimes(2);
    await act(async () => resumed.resolve(true));
    expect(result.current).toBe("live");
    await act(async () => old.resolve(false));
    expect(result.current).toBe("live");
    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it("復帰GET中にWSが再接続しても、本人GETと新snapshotの両方が揃ってからliveへ戻す", async () => {
    const pending = deferred<boolean>();
    const current = deferred<boolean>();
    const refresh = vi.fn<Refresh>().mockReturnValueOnce(pending.promise).mockReturnValueOnce(current.promise);
    const { result } = renderRealtime(refresh);
    const first = FakeWebSocket.instances[0];
    await act(async () => { first?.open(); first?.message(snapshot()); });
    await act(async () => { changeVisibility("hidden"); changeVisibility("visible"); });
    expect(refresh).toHaveBeenCalledOnce();
    await act(async () => first?.serverClose(1013));
    await act(async () => vi.advanceTimersByTimeAsync(750));
    const second = FakeWebSocket.instances[1];
    await act(async () => { second?.open(); second?.message(snapshot()); });
    expect(result.current).toBe("polling");
    await act(async () => {
      second?.message(control("session.changed", 2));
      second?.message(control("session.changed", 3));
    });
    await act(async () => pending.resolve(true));
    expect(result.current).toBe("polling");
    expect(refresh).toHaveBeenCalledTimes(2);
    await act(async () => current.resolve(true));
    expect(result.current).toBe("live");
    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it.each([[0, 5_000], [0.999, 5_999]])("取得完了後にjitter %sの待機%smsを挟み、低速GET中は連続再取得しない", async (random, delay) => {
    vi.spyOn(Math, "random").mockReturnValue(random);
    const pending = deferred<boolean>();
    const refresh = vi.fn<Refresh>().mockReturnValueOnce(pending.promise).mockResolvedValue(true);
    renderRealtime(refresh);
    const socket = FakeWebSocket.instances[0];
    await act(async () => {
      socket?.open();
      socket?.message({ type: "polling.required", reason: "realtime_unavailable", poll_after_ms: 5_000 });
    });
    expect(refresh).toHaveBeenCalledOnce();
    await act(async () => vi.advanceTimersByTimeAsync(20_000));
    expect(refresh).toHaveBeenCalledOnce();
    await act(async () => pending.resolve(true));
    expect(refresh).toHaveBeenCalledOnce();
    await act(async () => vi.advanceTimersByTimeAsync(delay - 1));
    expect(refresh).toHaveBeenCalledOnce();
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it("縮退中のsession.changedは後続1件へまとめ、取得完了後から待機を再開する", async () => {
    const pending = deferred<boolean>();
    const refresh = vi.fn<Refresh>().mockReturnValueOnce(pending.promise).mockResolvedValue(false);
    renderRealtime(refresh);
    const socket = FakeWebSocket.instances[0];
    await act(async () => { socket?.open(); socket?.message(snapshot()); });
    await act(async () => socket?.message(control("session.changed", 2)));
    await act(async () => { socket?.message(control("session.changed", 3)); pending.resolve(false); });
    expect(refresh).toHaveBeenCalledTimes(2);
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    await act(async () => socket?.message(control("session.changed", 4)));
    expect(refresh).toHaveBeenCalledTimes(3);
    await act(async () => vi.advanceTimersByTimeAsync(5_499));
    expect(refresh).toHaveBeenCalledTimes(3);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(refresh).toHaveBeenCalledTimes(4);
  });

  it("回復snapshotで予定済みfallbackを停止し、heartbeatだけでは回復しない", async () => {
    const refresh = vi.fn<Refresh>().mockResolvedValue(true);
    const { result } = renderRealtime(refresh);
    await act(async () => FakeWebSocket.instances[0]?.serverClose(1013));
    expect(refresh).toHaveBeenCalledOnce();
    await act(async () => vi.advanceTimersByTimeAsync(750));
    const socket = FakeWebSocket.instances[1];
    await act(async () => { socket?.open(); socket?.message(control("heartbeat", 1)); });
    expect(result.current).toBe("polling");
    await act(async () => socket?.message(snapshot(2)));
    expect(result.current).toBe("live");
    for (let sequence = 3; sequence <= 5; sequence += 1) {
      await act(async () => vi.advanceTimersByTimeAsync(5_000));
      await act(async () => socket?.message(control("heartbeat", sequence)));
    }
    expect(refresh).toHaveBeenCalledOnce();
  });

  it.each(["hidden", "unmount", "authentication"])("取得中の%s後は遅いGETが完了してもfallbackを再開しない", async (cancellation) => {
    const pending = deferred<boolean>();
    const refresh = vi.fn<Refresh>().mockReturnValueOnce(pending.promise).mockResolvedValue(true);
    const view = renderRealtime(refresh);
    await act(async () => FakeWebSocket.instances[0]?.serverClose(1013));
    await act(async () => vi.advanceTimersByTimeAsync(750));
    await act(async () => {
      if (cancellation === "hidden") changeVisibility("hidden");
      if (cancellation === "unmount") view.unmount();
      if (cancellation === "authentication") FakeWebSocket.instances[1]?.serverClose(4401);
      pending.resolve(true);
    });
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(refresh).toHaveBeenCalledOnce();
  });
});
