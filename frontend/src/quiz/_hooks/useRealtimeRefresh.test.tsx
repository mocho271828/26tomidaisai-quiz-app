import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PersonalResultDelivery, PersonalResultResponse } from "../../api/contracts";
import type { RealtimeStateMessage } from "../../api/realtimeContracts";
import { useRealtimeRefresh } from "./useRealtimeRefresh";

import {
  changeVisibility, control, deferred, eventId, FakeWebSocket, snapshot, streamId, type Refresh,
} from "./realtimeRefreshTestUtils";

function Harness({
  enabled = true,
  path = "/api/v1/participant/realtime",
  refresh,
  onState = () => true,
  onAuthenticationLost,
  refreshOnConnect = true,
  onPersonalResult,
  onPersonalResultDelivery,
}: {
  enabled?: boolean;
  path?: string;
  refresh: Refresh;
  onState?: (message: RealtimeStateMessage) => boolean;
  onAuthenticationLost?: () => void;
  refreshOnConnect?: boolean;
  onPersonalResult?: (result: PersonalResultResponse) => void;
  onPersonalResultDelivery?: (delivery: PersonalResultDelivery) => void;
}) {
  const status = useRealtimeRefresh({
    audience: "participant", enabled, eventId: eventId, path, refresh, onState,
    onAuthenticationLost, refreshOnConnect, onPersonalResult, onPersonalResultDelivery,
  });
  return <output>{status}</output>;
}

describe("useRealtimeRefresh", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeWebSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket);
    vi.spyOn(Math, "random").mockReturnValue(0.5);
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("本人結果と共通状態を独立に適用し、heartbeatのreadyを本人受信扱いしない", async () => {
    const onResult = vi.fn();
    const onDelivery = vi.fn();
    const refresh = vi.fn<Refresh>().mockResolvedValue(true);
    const result: PersonalResultResponse = {
      delivery: { status: "ready", revision: {
        event_id: eventId, event_version: 1, result_round_id: null, result_round_version: null,
        ranking_snapshot_version: null, result_visibility: { distribution: false, personal_ranking: false, overall_ranking: false },
      } },
      result: { selected_option_id: null, outcome: null, ranking: null },
    };
    render(<Harness refresh={refresh} refreshOnConnect={false} onPersonalResult={onResult} onPersonalResultDelivery={onDelivery} />);
    const socket = FakeWebSocket.instances[0];
    await act(async () => { socket?.open(); socket?.message(snapshot()); });
    await act(async () => socket?.message({ ...control("heartbeat", 2), personal_result_delivery: result.delivery }));
    expect(onDelivery).toHaveBeenCalledWith(result.delivery);
    expect(onResult).not.toHaveBeenCalled();
    expect(refresh).not.toHaveBeenCalled();
    await act(async () => socket?.message({ ...control("heartbeat", 3), type: "participant.result", result }));
    expect(onResult).toHaveBeenCalledWith(result);
    expect(screen.getByText("live")).toBeVisible();
    expect(refresh).not.toHaveBeenCalled();
  });

  it("本人結果だけが届いても初回共通snapshotの6秒期限を延長しない", async () => {
    const refresh = vi.fn<Refresh>().mockResolvedValue(true);
    const onResult = vi.fn();
    render(<Harness refresh={refresh} onPersonalResult={onResult} refreshOnConnect={false} />);
    const socket = FakeWebSocket.instances[0];
    await act(async () => socket?.open());
    await act(async () => vi.advanceTimersByTimeAsync(5_000));
    await act(async () => socket?.message({ ...control("heartbeat", 1), type: "participant.result",
      result: { delivery: { status: "unavailable", revision: null }, result: null } }));
    expect(onResult).not.toHaveBeenCalled();
    expect(screen.getByText("connecting")).toBeVisible();
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(socket?.readyState).toBe(FakeWebSocket.CLOSED);
    expect(refresh).toHaveBeenCalledOnce();
  });

  it("open/heartbeatだけではliveにせず、初期snapshotを直接適用し本人GETを1回だけ行う", async () => {
    const refresh = vi.fn<Refresh>().mockResolvedValue(true);
    const onState = vi.fn().mockReturnValue(true);
    render(<Harness refresh={refresh} onState={onState} />);
    const socket = FakeWebSocket.instances[0];
    await act(async () => socket?.open());
    expect(refresh).not.toHaveBeenCalled();
    await act(async () => socket?.message(control("heartbeat", 1)));
    expect(screen.getByText("connecting")).toBeVisible();
    await act(async () => socket?.message(snapshot(2)));
    expect(onState).toHaveBeenCalledOnce();
    expect(refresh).toHaveBeenCalledOnce();
    expect(screen.getByText("live")).toBeVisible();
    await act(async () => socket?.message(snapshot(3)));
    expect(onState).toHaveBeenCalledTimes(2);
    expect(refresh).toHaveBeenCalledOnce();
  });

  it("初期snapshotはheartbeatで通信を確認しながら待ち、6秒でpollingへ移る", async () => {
    const refresh = vi.fn<Refresh>().mockResolvedValue(true);
    render(<Harness refresh={refresh} />);
    const socket = FakeWebSocket.instances[0];
    await act(async () => socket?.open());
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    await act(async () => socket?.message(control("heartbeat", 1)));
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    await act(async () => socket?.message(control("heartbeat", 2)));
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    await act(async () => socket?.message(control("heartbeat", 3)));
    await act(async () => vi.advanceTimersByTimeAsync(999));
    expect(socket?.readyState).toBe(FakeWebSocket.OPEN);
    expect(screen.getByText("connecting")).toBeVisible();
    expect(refresh).not.toHaveBeenCalled();
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(socket?.readyState).toBe(FakeWebSocket.CLOSED);
    expect(screen.getByText("polling")).toBeVisible();
    expect(refresh).toHaveBeenCalledOnce();
  });

  it("初期状態の受信後に通信が7.5秒止まればpollingへ移る", async () => {
    const refresh = vi.fn<Refresh>().mockResolvedValue(true);
    render(<Harness refresh={refresh} refreshOnConnect={false} />);
    const socket = FakeWebSocket.instances[0];
    await act(async () => socket?.open());
    await act(async () => socket?.message(snapshot()));
    await act(async () => vi.advanceTimersByTimeAsync(7_499));
    expect(screen.getByText("live")).toBeVisible();
    expect(refresh).not.toHaveBeenCalled();
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(socket?.readyState).toBe(FakeWebSocket.CLOSED);
    expect(screen.getByText("polling")).toBeVisible();
    expect(refresh).toHaveBeenCalledOnce();
  });

  it("heartbeatを受けながら5秒後に来た初回状態を適用し、6秒期限を回収する", async () => {
    const refresh = vi.fn<Refresh>().mockResolvedValue(true);
    const onState = vi.fn().mockReturnValue(true);
    render(<Harness refresh={refresh} onState={onState} />);
    const socket = FakeWebSocket.instances[0];
    await act(async () => socket?.open());
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    await act(async () => socket?.message(control("heartbeat", 1)));
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    await act(async () => socket?.message(control("heartbeat", 2)));
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(screen.getByText("connecting")).toBeVisible();
    await act(async () => socket?.message(snapshot(3)));
    expect(onState).toHaveBeenCalledOnce();
    expect(refresh).toHaveBeenCalledOnce();
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(socket?.readyState).toBe(FakeWebSocket.OPEN);
    expect(screen.getByText("live")).toBeVisible();
    expect(refresh).toHaveBeenCalledOnce();
  });

  it("運営相当のsnapshot適用にはGETを発生させない", async () => {
    const refresh = vi.fn<Refresh>().mockResolvedValue(true);
    render(<Harness refresh={refresh} refreshOnConnect={false} />);
    await act(async () => FakeWebSocket.instances[0]?.open());
    await act(async () => FakeWebSocket.instances[0]?.message(snapshot()));
    expect(refresh).not.toHaveBeenCalled();
    expect(screen.getByText("live")).toBeVisible();
  });

  it("本人GETの一時失敗後は同じsnapshotとheartbeatだけでpollingを停止する", async () => {
    const refresh = vi.fn<Refresh>().mockResolvedValueOnce(false).mockResolvedValue(true);
    render(<Harness refresh={refresh} />);
    const socket = FakeWebSocket.instances[0];
    await act(async () => socket?.open());
    await act(async () => socket?.message(snapshot()));
    expect(screen.getByText("polling")).toBeVisible();
    expect(refresh).toHaveBeenCalledOnce();

    await act(async () => vi.advanceTimersByTimeAsync(5_000));
    await act(async () => socket?.message(control("heartbeat", 2)));
    expect(screen.getByText("polling")).toBeVisible();
    expect(refresh).toHaveBeenCalledOnce();
    await act(async () => vi.advanceTimersByTimeAsync(500));
    expect(screen.getByText("live")).toBeVisible();
    expect(refresh).toHaveBeenCalledTimes(2);
    for (let sequence = 3; sequence <= 6; sequence += 1) {
      await act(async () => vi.advanceTimersByTimeAsync(2_000));
      await act(async () => socket?.message(control("heartbeat", sequence)));
    }
    expect(refresh).toHaveBeenCalledTimes(2);
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(socket?.readyState).toBe(FakeWebSocket.OPEN);
  });

  it("再接続先のsnapshotでは失敗した本人GETの補完を打ち切らない", async () => {
    const refresh = vi.fn<Refresh>().mockResolvedValueOnce(false).mockResolvedValue(true);
    render(<Harness refresh={refresh} refreshOnConnect={false} />);
    const first = FakeWebSocket.instances[0];
    await act(async () => { first?.open(); first?.message(snapshot()); });
    await act(async () => first?.serverClose(1013));
    expect(refresh).toHaveBeenCalledOnce();
    await act(async () => vi.advanceTimersByTimeAsync(750));
    const next = FakeWebSocket.instances[1];
    await act(async () => { next?.open(); next?.message(snapshot(1, eventId)); });
    expect(screen.getByText("polling")).toBeVisible();
    await act(async () => {
      next?.message(control("heartbeat", 2, eventId));
      await vi.advanceTimersByTimeAsync(4_750);
    });
    expect(refresh).toHaveBeenCalledTimes(2);
    expect(screen.getByText("live")).toBeVisible();
    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  it("本人GET再試行中の明示縮退後は古いHTTP成功や新socketのheartbeatでliveに戻さない", async () => {
    vi.spyOn(Math, "random").mockReturnValue(0.5);
    const retry = deferred<boolean>();
    const refresh = vi.fn<Refresh>().mockResolvedValueOnce(false)
      .mockReturnValueOnce(retry.promise).mockResolvedValue(true);
    render(<Harness refresh={refresh} />);
    const first = FakeWebSocket.instances[0];
    await act(async () => first?.open());
    await act(async () => first?.message(snapshot()));
    await act(async () => vi.advanceTimersByTimeAsync(5_500));
    expect(refresh).toHaveBeenCalledTimes(2);
    await act(async () => first?.message({ type: "polling.required", reason: "realtime_unavailable", poll_after_ms: 5_000 }));
    await act(async () => vi.advanceTimersByTimeAsync(750));
    const next = FakeWebSocket.instances[1];
    await act(async () => next?.open());
    await act(async () => next?.message(control("heartbeat", 1, eventId)));
    await act(async () => retry.resolve(true));
    expect(screen.getByText("polling")).toBeVisible();
    await act(async () => next?.message(snapshot(2, eventId)));
    expect(screen.getByText("live")).toBeVisible();
  });

  it("復帰GETの失敗後も検証済みsnapshotを保持し、後続GET成功でpollingを停止する", async () => {
    const old = deferred<boolean>();
    const resumed = deferred<boolean>();
    const refresh = vi.fn<Refresh>().mockResolvedValueOnce(true)
      .mockReturnValueOnce(old.promise).mockReturnValueOnce(resumed.promise).mockResolvedValue(true);
    render(<Harness refresh={refresh} />);
    const socket = FakeWebSocket.instances[0];
    await act(async () => socket?.open());
    await act(async () => socket?.message(snapshot()));
    await act(async () => socket?.message(control("session.changed", 2)));
    await act(async () => {
      changeVisibility("hidden");
      changeVisibility("visible");
    });
    await act(async () => old.resolve(true));
    expect(refresh).toHaveBeenCalledTimes(3);
    await act(async () => resumed.resolve(false));
    expect(screen.getByText("polling")).toBeVisible();
    await act(async () => socket?.message(control("heartbeat", 3)));
    expect(screen.getByText("polling")).toBeVisible();
    await act(async () => vi.advanceTimersByTimeAsync(5_500));
    expect(screen.getByText("live")).toBeVisible();
    await act(async () => socket?.message(control("heartbeat", 4)));
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    expect(refresh).toHaveBeenCalledTimes(4);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it("sequenceが後退したsnapshotを無視し、heartbeatもGETを発生させない", async () => {
    const refresh = vi.fn<Refresh>().mockResolvedValue(true);
    const onState = vi.fn().mockReturnValue(true);
    render(<Harness refresh={refresh} onState={onState} />);
    const socket = FakeWebSocket.instances[0];
    await act(async () => socket?.open());
    await act(async () => socket?.message(snapshot(3)));
    await act(async () => socket?.message(snapshot(2)));
    await act(async () => socket?.message(snapshot(3)));
    await act(async () => socket?.message(control("heartbeat", 4)));
    expect(onState).toHaveBeenCalledOnce();
    expect(refresh).toHaveBeenCalledOnce();
    await act(async () => vi.advanceTimersByTimeAsync(7_499));
    expect(screen.getByText("live")).toBeVisible();
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(screen.getByText("polling")).toBeVisible();
  });

  it.each(["invalid", "event", "audience", "stream"])("%s不一致はpollingへ縮退する", async (invalid) => {
    const refresh = vi.fn<Refresh>().mockResolvedValue(true);
    render(<Harness refresh={refresh} />);
    const socket = FakeWebSocket.instances[0];
    await act(async () => socket?.open());
    await act(async () => socket?.message(snapshot()));
    await act(async () => {
      if (invalid === "invalid") socket?.invalidMessage();
      if (invalid === "event") socket?.message({ ...snapshot(2), event_id: streamId });
      if (invalid === "audience") socket?.message({ ...control("heartbeat", 2), audience: "operator" });
      if (invalid === "stream") socket?.message(snapshot(2, eventId));
    });
    expect(screen.getByText("polling")).toBeVisible();
    expect(socket?.readyState).toBe(FakeWebSocket.CLOSED);
  });

  it("polling.required後は再接続snapshotの検証までpollingを維持する", async () => {
    vi.spyOn(Math, "random").mockReturnValue(0.5);
    const refresh = vi.fn<Refresh>().mockResolvedValue(true);
    render(<Harness refresh={refresh} />);
    const first = FakeWebSocket.instances[0];
    await act(async () => first?.open());
    await act(async () => first?.message({ type: "polling.required", reason: "realtime_unavailable", poll_after_ms: 5_000 }));
    expect(screen.getByText("polling")).toBeVisible();
    await act(async () => vi.advanceTimersByTimeAsync(750));
    const next = FakeWebSocket.instances[1];
    await act(async () => next?.open());
    expect(screen.getByText("polling")).toBeVisible();
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    await act(async () => next?.message(control("heartbeat", 1, eventId)));
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    await act(async () => next?.message(control("heartbeat", 2, eventId)));
    expect(screen.getByText("polling")).toBeVisible();
    await act(async () => next?.message(snapshot(3, eventId)));
    expect(screen.getByText("live")).toBeVisible();
  });

  it("古いsocketとそのGET成功が新しい接続の検証を代行しない", async () => {
    vi.spyOn(Math, "random").mockReturnValue(0.5);
    const old = deferred<boolean>();
    const refresh = vi.fn<Refresh>().mockReturnValueOnce(old.promise).mockResolvedValue(true);
    const onState = vi.fn().mockReturnValue(true);
    render(<Harness refresh={refresh} onState={onState} />);
    const first = FakeWebSocket.instances[0];
    await act(async () => first?.open());
    await act(async () => first?.message(snapshot()));
    await act(async () => first?.serverClose(1013));
    await act(async () => vi.advanceTimersByTimeAsync(750));
    await act(async () => FakeWebSocket.instances[1]?.open());
    await act(async () => first?.message(snapshot(2)));
    await act(async () => old.resolve(true));
    expect(onState).toHaveBeenCalledOnce();
    expect(screen.getByText("polling")).toBeVisible();
    await act(async () => FakeWebSocket.instances[1]?.message(snapshot(1, eventId)));
    expect(screen.getByText("live")).toBeVisible();
  });

  it("session.changedを取得中の後続1件へまとめる", async () => {
    const first = deferred<boolean>();
    const refresh = vi.fn<Refresh>().mockReturnValueOnce(first.promise).mockResolvedValue(true);
    render(<Harness refresh={refresh} />);
    const socket = FakeWebSocket.instances[0];
    await act(async () => socket?.open());
    await act(async () => socket?.message(snapshot()));
    await act(async () => {
      socket?.message(control("session.changed", 2));
      socket?.message(control("session.changed", 3));
      first.resolve(true);
    });
    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it("hidden中のsnapshot/controlは復帰時に最新1件と本人GETへまとめる", async () => {
    const refresh = vi.fn<Refresh>().mockResolvedValue(true);
    const onState = vi.fn().mockReturnValue(true);
    render(<Harness refresh={refresh} onState={onState} />);
    const socket = FakeWebSocket.instances[0];
    await act(async () => socket?.open());
    await act(async () => socket?.message(snapshot()));
    refresh.mockClear();
    onState.mockClear();
    await act(async () => {
      changeVisibility("hidden");
      socket?.message(snapshot(2));
      socket?.message(control("session.changed", 3));
      socket?.message(snapshot(4));
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(refresh).not.toHaveBeenCalled();
    expect(onState).not.toHaveBeenCalled();
    expect(socket?.readyState).toBe(FakeWebSocket.OPEN);
    await act(async () => changeVisibility("visible"));
    expect(refresh).toHaveBeenCalledOnce();
    expect(onState).toHaveBeenCalledWith(snapshot(4), true);
    expect(onState).toHaveBeenCalledOnce();
  });

  it("4401失効では本人UIを失効させ、polling/retryを止める", async () => {
    const refresh = vi.fn<Refresh>().mockResolvedValue(true);
    const lost = vi.fn();
    render(<Harness refresh={refresh} onAuthenticationLost={lost} />);
    await act(async () => FakeWebSocket.instances[0]?.serverClose(4401));
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(lost).toHaveBeenCalledOnce();
    expect(refresh).not.toHaveBeenCalled();
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it("初回状態待ちをhidden中は止め、復帰後もheartbeatだけでは6秒を延長しない", async () => {
    const refresh = vi.fn<Refresh>().mockResolvedValue(true);
    render(<Harness refresh={refresh} />);
    const socket = FakeWebSocket.instances[0];
    await act(async () => socket?.open());
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    await act(async () => {
      changeVisibility("hidden");
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(socket?.readyState).toBe(FakeWebSocket.OPEN);
    expect(refresh).not.toHaveBeenCalled();
    await act(async () => changeVisibility("visible"));
    expect(refresh).toHaveBeenCalledOnce();
    expect(screen.getByText("connecting")).toBeVisible();
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    await act(async () => socket?.message(control("heartbeat", 1)));
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    await act(async () => socket?.message(control("heartbeat", 2)));
    await act(async () => vi.advanceTimersByTimeAsync(1_999));
    expect(socket?.readyState).toBe(FakeWebSocket.OPEN);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(socket?.readyState).toBe(FakeWebSocket.CLOSED);
    expect(screen.getByText("polling")).toBeVisible();
  });

  it("再接続は指数backoffを維持し、10秒snapshot/heartbeat安定後だけ戻す", async () => {
    vi.spyOn(Math, "random").mockReturnValue(0.5);
    const refresh = vi.fn<Refresh>().mockResolvedValue(true);
    const view = render(<Harness refresh={refresh} />);
    await act(async () => FakeWebSocket.instances[0]?.serverClose(1013));
    await act(async () => vi.advanceTimersByTimeAsync(749));
    expect(FakeWebSocket.instances).toHaveLength(1);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    await act(async () => FakeWebSocket.instances[1]?.serverClose(1013));
    await act(async () => vi.advanceTimersByTimeAsync(1_499));
    expect(FakeWebSocket.instances).toHaveLength(2);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    const third = FakeWebSocket.instances[2];
    await act(async () => third?.open());
    await act(async () => third?.message(snapshot()));
    for (let sequence = 2; sequence <= 6; sequence += 1) {
      await act(async () => vi.advanceTimersByTimeAsync(2_000));
      await act(async () => third?.message(control("heartbeat", sequence)));
    }
    await act(async () => third?.serverClose(1013));
    await act(async () => vi.advanceTimersByTimeAsync(750));
    expect(FakeWebSocket.instances).toHaveLength(4);
    view.unmount();
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(FakeWebSocket.instances).toHaveLength(4);
  });

  it.each(["path", "enabled", "unmount"])("%s変更で旧接続と全timerを回収する", async (change) => {
    const refresh = vi.fn<Refresh>().mockResolvedValue(true);
    const view = render(<Harness refresh={refresh} />);
    await act(async () => FakeWebSocket.instances[0]?.open());
    if (change === "path") view.rerender(<Harness path="/realtime/new" refresh={refresh} />);
    else if (change === "enabled") view.rerender(<Harness enabled={false} refresh={refresh} />);
    else view.unmount();
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(refresh).not.toHaveBeenCalled();
    expect(FakeWebSocket.instances).toHaveLength(change === "path" ? 2 : 1);
  });
});
