import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useServerClock } from "./useServerClock";
import { useDisplayTime } from "./useDisplayTime";

const audienceApi = vi.hoisted(() => ({ getServerTime: vi.fn() }));

vi.mock("../../api/audienceClient", () => audienceApi);

function ClockHarness() {
  const clock = useServerClock();
  const now = useDisplayTime(clock, Number.POSITIVE_INFINITY, clock.isSynchronized);
  return (
    <output data-testid="clock" data-monotonic-offset={clock.monotonicOffsetMs}>
      {clock.isSynchronized ? Math.round(now).toString() : "syncing"}
    </output>
  );
}

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
} {
  let resolvePromise: (value: T) => void = () => undefined;
  const promise = new Promise<T>((resolve) => {
    resolvePromise = resolve;
  });
  return { promise, resolve: resolvePromise };
}

async function flushPromises(): Promise<void> {
  await act(async () => {
    for (let index = 0; index < 8; index += 1) {
      await Promise.resolve();
    }
  });
}

function displayedClockValue(): number {
  return Number(screen.getByTestId("clock").textContent);
}

describe("useServerClock", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-08T01:00:00Z"));
    vi.clearAllMocks();
    vi.spyOn(Math, "random").mockReturnValue(0.5);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("同一visible世代のonline連続発火を進行中の3標本へまとめる", async () => {
    const first = deferred<{ shared_sample_age_ms: number; server_time: string }>();
    audienceApi.getServerTime.mockReturnValueOnce(first.promise).mockResolvedValue({ shared_sample_age_ms: 0, server_time: "2026-08-08T01:00:10Z" });
    render(<ClockHarness />);
    const firstSignal = audienceApi.getServerTime.mock.calls[0]?.[0] as AbortSignal;
    await act(async () => {
      window.dispatchEvent(new Event("online"));
      window.dispatchEvent(new Event("online"));
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(audienceApi.getServerTime).toHaveBeenCalledOnce();
    expect(firstSignal.aborted).toBe(false);
    await act(async () => first.resolve({ shared_sample_age_ms: 0, server_time: "2026-08-08T01:00:10Z" }));
    await flushPromises();
    expect(audienceApi.getServerTime).toHaveBeenCalledTimes(3);
    expect(screen.getByTestId("clock")).not.toHaveTextContent("syncing");
  });

  it("共有DB照会への2秒遅い合流は実RTT50msでも同期済みにしない", async () => {
    audienceApi.getServerTime.mockImplementation(() => new Promise((resolve) => {
      window.setTimeout(() => resolve({ server_time: "2026-08-08T01:00:10Z", shared_sample_age_ms: 2_000 }), 50);
    }));
    render(<ClockHarness />);
    await act(async () => vi.advanceTimersByTimeAsync(150));
    expect(audienceApi.getServerTime).toHaveBeenCalledTimes(3);
    expect(screen.getByTestId("clock")).toHaveTextContent("syncing");
    audienceApi.getServerTime.mockResolvedValue({ server_time: "2026-08-08T01:00:11Z", shared_sample_age_ms: 0 });
    await act(async () => vi.advanceTimersByTimeAsync(750));
    expect(screen.getByTestId("clock")).not.toHaveTextContent("syncing");
  });

  it("3標本のうちround-trip timeが最短の標本を採用する", async () => {
    const slow = deferred<{ shared_sample_age_ms: number; server_time: string }>();
    const fastest = deferred<{ shared_sample_age_ms: number; server_time: string }>();
    const medium = deferred<{ shared_sample_age_ms: number; server_time: string }>();
    audienceApi.getServerTime
      .mockReturnValueOnce(slow.promise)
      .mockReturnValueOnce(fastest.promise)
      .mockReturnValueOnce(medium.promise);
    render(<ClockHarness />);

    await act(async () => {
      vi.advanceTimersByTime(120);
      slow.resolve({ shared_sample_age_ms: 0, server_time: "2026-08-08T01:00:10Z" });
      await Promise.resolve();
    });
    await act(async () => {
      vi.advanceTimersByTime(10);
      fastest.resolve({ shared_sample_age_ms: 0, server_time: "2026-08-08T01:00:20Z" });
      await Promise.resolve();
    });
    await act(async () => {
      vi.advanceTimersByTime(60);
      medium.resolve({ shared_sample_age_ms: 0, server_time: "2026-08-08T01:00:30Z" });
      await Promise.resolve();
    });
    await flushPromises();

    // 最短標本は130ms時点でserver 20,005ms、採用時190msまで60ms進む。
    expect(displayedClockValue()).toBe(Date.parse("2026-08-08T01:00:20Z") + 65);
  });

  it("同期成功後は120秒にjitterを加えて3標本を取り、表示時計は100msごとに進む", async () => {
    audienceApi.getServerTime.mockResolvedValue({
      shared_sample_age_ms: 0, server_time: "2026-08-08T01:00:10Z",
    });
    render(<ClockHarness />);
    await flushPromises();
    const synchronizedAt = displayedClockValue();

    await act(async () => vi.advanceTimersByTimeAsync(100));
    expect(displayedClockValue()).toBe(synchronizedAt + 100);
    await act(async () => vi.advanceTimersByTimeAsync(125_899));
    expect(audienceApi.getServerTime).toHaveBeenCalledTimes(3);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(audienceApi.getServerTime).toHaveBeenCalledTimes(6);
    expect(displayedClockValue()).toBe(synchronizedAt + 126_000);

    await act(async () => vi.advanceTimersByTimeAsync(125_999));
    expect(audienceApi.getServerTime).toHaveBeenCalledTimes(6);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(audienceApi.getServerTime).toHaveBeenCalledTimes(9);
  });

  it("通常周期の再同期が失敗しても120秒を待たず再試行する", async () => {
    vi.spyOn(Math, "random").mockReturnValue(0.5);
    audienceApi.getServerTime.mockResolvedValue({
      shared_sample_age_ms: 0, server_time: "2026-08-08T01:00:10Z",
    });
    render(<ClockHarness />);
    await flushPromises();
    audienceApi.getServerTime.mockImplementation(() =>
      new Promise<{ shared_sample_age_ms: number; server_time: string }>((resolve) => {
        window.setTimeout(() => resolve({ shared_sample_age_ms: 0, server_time: new Date().toISOString() }), 600);
      }),
    );

    await act(async () => vi.advanceTimersByTimeAsync(127_800));
    expect(audienceApi.getServerTime).toHaveBeenCalledTimes(6);
    expect(screen.getByTestId("clock")).toHaveTextContent("syncing");
    audienceApi.getServerTime.mockResolvedValue({
      shared_sample_age_ms: 0, server_time: "2026-08-08T01:02:10Z",
    });
    await act(async () => vi.advanceTimersByTimeAsync(750));
    expect(audienceApi.getServerTime).toHaveBeenCalledTimes(9);
    expect(screen.getByTestId("clock")).not.toHaveTextContent("syncing");
  });

  it("同期済みでもonlineで即時再同期し、成功時点から120秒にjitterを加えて通常同期する", async () => {
    audienceApi.getServerTime.mockResolvedValue({
      shared_sample_age_ms: 0, server_time: "2026-08-08T01:00:10Z",
    });
    render(<ClockHarness />);
    await flushPromises();
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    await act(async () => window.dispatchEvent(new Event("online")));
    await flushPromises();
    expect(audienceApi.getServerTime).toHaveBeenCalledTimes(6);

    await act(async () => vi.advanceTimersByTimeAsync(125_999));
    expect(audienceApi.getServerTime).toHaveBeenCalledTimes(6);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(audienceApi.getServerTime).toHaveBeenCalledTimes(9);
  });

  it("最短標本でもround-trip timeが500msを超える場合は同期済みにしない", async () => {
    const responseDelays = [800, 600, 700];
    audienceApi.getServerTime.mockImplementation(
      () =>
        new Promise<{ shared_sample_age_ms: number; server_time: string }>((resolve) => {
          const responseDelay = responseDelays.shift();
          window.setTimeout(
            () => resolve({ shared_sample_age_ms: 0, server_time: "2026-08-08T01:00:10Z" }),
            responseDelay,
          );
        }),
    );
    render(<ClockHarness />);

    await act(async () => vi.advanceTimersByTimeAsync(2_100));

    expect(audienceApi.getServerTime).toHaveBeenCalledTimes(3);
    expect(screen.getByTestId("clock")).toHaveTextContent("syncing");
  });

  it("各sample fetchをtimeoutでabortし、hangしても同期処理を完了する", async () => {
    const sampleSignals: AbortSignal[] = [];
    audienceApi.getServerTime.mockImplementation((signal: AbortSignal) => {
      sampleSignals.push(signal);
      return new Promise<{ shared_sample_age_ms: number; server_time: string }>(() => undefined);
    });
    render(<ClockHarness />);

    await act(async () => vi.advanceTimersByTimeAsync(3_000));

    expect(audienceApi.getServerTime).toHaveBeenCalledTimes(3);
    expect(sampleSignals).toHaveLength(3);
    expect(sampleSignals.every((signal) => signal.aborted)).toBe(true);
    expect(screen.getByTestId("clock")).toHaveTextContent("syncing");
  });

  it("端末wall clock変更は正式計測へ使わず再同期を要求する", async () => {
    audienceApi.getServerTime.mockResolvedValue({ shared_sample_age_ms: 0, server_time: "2026-08-08T01:00:10Z" });
    const recheck = vi.fn();
    window.addEventListener("quiz:clock-recheck", recheck);
    render(<ClockHarness />);
    await flushPromises();
    const synchronizedAt = displayedClockValue();
    vi.setSystemTime(new Date("2036-01-01T00:00:00Z"));
    await act(async () => vi.advanceTimersByTimeAsync(100));
    await flushPromises();
    expect(recheck).toHaveBeenCalledOnce();
    expect(audienceApi.getServerTime).toHaveBeenCalledTimes(6);
    expect(displayedClockValue()).toBe(synchronizedAt);
    window.removeEventListener("quiz:clock-recheck", recheck);
  });

  it("background中は同期せず、visible復帰時に新しい3標本で再同期する", async () => {
    let visibilityState: DocumentVisibilityState = "visible";
    vi.spyOn(document, "visibilityState", "get").mockImplementation(
      () => visibilityState,
    );
    audienceApi.getServerTime
      .mockResolvedValueOnce({ shared_sample_age_ms: 0, server_time: "2026-08-08T01:00:10Z" })
      .mockResolvedValueOnce({ shared_sample_age_ms: 0, server_time: "2026-08-08T01:00:10Z" })
      .mockResolvedValueOnce({ shared_sample_age_ms: 0, server_time: "2026-08-08T01:00:10Z" })
      .mockResolvedValue({ shared_sample_age_ms: 0, server_time: "2026-08-08T01:03:00Z" });
    render(<ClockHarness />);
    await flushPromises();
    expect(displayedClockValue()).toBe(Date.parse("2026-08-08T01:00:10Z"));

    visibilityState = "hidden";
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await act(async () => vi.advanceTimersByTime(120_000));
    expect(audienceApi.getServerTime).toHaveBeenCalledTimes(3);

    visibilityState = "visible";
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await flushPromises();
    expect(audienceApi.getServerTime).toHaveBeenCalledTimes(6);
    expect(displayedClockValue()).toBe(Date.parse("2026-08-08T01:03:00Z"));
  });

  it("復帰後の同期より遅く届いた古いresponseで時計を巻き戻さない", async () => {
    let visibilityState: DocumentVisibilityState = "visible";
    vi.spyOn(document, "visibilityState", "get").mockImplementation(
      () => visibilityState,
    );
    const oldResponse = deferred<{ shared_sample_age_ms: number; server_time: string }>();
    audienceApi.getServerTime
      .mockReturnValueOnce(oldResponse.promise)
      .mockResolvedValue({ shared_sample_age_ms: 0, server_time: "2026-08-08T01:00:40Z" });
    render(<ClockHarness />);

    visibilityState = "hidden";
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    visibilityState = "visible";
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await flushPromises();
    expect(displayedClockValue()).toBe(Date.parse("2026-08-08T01:00:40Z"));

    await act(async () => {
      oldResponse.resolve({ shared_sample_age_ms: 0, server_time: "2026-08-08T00:00:00Z" });
      await Promise.resolve();
    });
    expect(displayedClockValue()).toBe(Date.parse("2026-08-08T01:00:40Z"));
  });
  it("初回全標本600msでも通信回復後1秒以内に再同期を開始する", async () => {
    vi.spyOn(Math, "random").mockReturnValue(0.5);
    audienceApi.getServerTime.mockImplementation(() => new Promise<{ shared_sample_age_ms: number; server_time: string }>((resolve) => {
      window.setTimeout(() => resolve({ shared_sample_age_ms: 0, server_time: new Date().toISOString() }), 600);
    }));
    render(<ClockHarness />);
    await act(async () => vi.advanceTimersByTimeAsync(1_800));
    expect(screen.getByTestId("clock")).toHaveTextContent("syncing");
    audienceApi.getServerTime.mockResolvedValue({ shared_sample_age_ms: 0, server_time: "2026-08-08T01:00:10Z" });
    await act(async () => vi.advanceTimersByTimeAsync(750));
    expect(audienceApi.getServerTime).toHaveBeenCalledTimes(6);
    expect(displayedClockValue()).toBe(Date.parse("2026-08-08T01:00:10Z"));
  });

  it("onlineで早期再同期しunmountでretryを取り消す", async () => {
    vi.spyOn(Math, "random").mockReturnValue(0.5);
    audienceApi.getServerTime.mockRejectedValue(new TypeError("network unavailable"));
    const view = render(<ClockHarness />);
    await flushPromises();
    expect(audienceApi.getServerTime).toHaveBeenCalledTimes(3);
    audienceApi.getServerTime.mockResolvedValue({ shared_sample_age_ms: 0, server_time: "2026-08-08T01:00:10Z" });
    await act(async () => window.dispatchEvent(new Event("online")));
    await flushPromises();
    expect(audienceApi.getServerTime).toHaveBeenCalledTimes(6);
    view.unmount();
    await act(async () => vi.advanceTimersByTimeAsync(240_000));
    expect(audienceApi.getServerTime).toHaveBeenCalledTimes(6);
  });

});
