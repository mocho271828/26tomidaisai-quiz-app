import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ParticipantCurrentRound } from "../../api/contracts";
import type { ServerClock } from "../_hooks/useServerClock";
import { elapsedSeconds, remainingSeconds } from "../_utils/answerTime";
import { useRoundClock } from "./useRoundClock";

const start = Date.parse("2026-10-15T00:00:00Z");
const round: ParticipantCurrentRound = {
  round_id: "60000000-0000-4000-8000-000000000001", round_number: 3,
  version: 2, state_version: 2, state: "Scheduled", open_at: new Date(start).toISOString(),
  standard_close_at: new Date(start + 10_000).toISOString(),
};
function clock(elapsedMs: number, correction = 0, epoch = 0): ServerClock {
  return { isSynchronized: true, monotonicOffsetMs: -start - correction,
    now: start + elapsedMs + correction, epoch };
}

describe("Roundの端末計測", () => {
  afterEach(() => vi.restoreAllMocks());

  it.each([-0.1, 0, 99.9, 100, 9999.9, 10000])("click時の未丸め値 %s ms で開始・締切を判定する", (value) => {
    let performanceMs = -100;
    vi.spyOn(performance, "now").mockImplementation(() => performanceMs);
    const { result } = renderHook(() => useRoundClock(round, clock(-100)));
    performanceMs = value;
    expect(result.current.captureElapsedMs()).toBe(value >= 0 && value < 10000 ? Math.floor(value) : null);
  });

  it("tick間の選択を採時し、通常の時計再同期・Scheduled→Openで開始基点を動かさない", () => {
    let performanceMs = 2000;
    vi.spyOn(performance, "now").mockImplementation(() => performanceMs);
    const { result, rerender } = renderHook(({ synced, state }) => useRoundClock(state, synced), {
      initialProps: { synced: clock(2000), state: round },
    });
    performanceMs = 2089.9;
    expect(result.current.captureElapsedMs()).toBe(2089);
    rerender({ synced: clock(performanceMs, 400), state: { ...round, state: "Open" } });
    expect(result.current.now - start).toBeCloseTo(2089.9);
    expect(result.current.captureElapsedMs()).toBe(2089);
    performanceMs = 10000;
    expect(result.current.captureElapsedMs()).toBeNull();
  });

  it("復帰の新anchorは共通開始へ対応させ、同一documentで経過を後退させない", () => {
    let performanceMs = 2000;
    vi.spyOn(performance, "now").mockImplementation(() => performanceMs);
    const { result, rerender } = renderHook(({ synced }) => useRoundClock(round, synced), {
      initialProps: { synced: clock(2000) },
    });
    rerender({ synced: { ...clock(2000), isSynchronized: false, epoch: 1 } });
    expect(result.current.captureElapsedMs()).toBeNull();
    rerender({ synced: clock(2000, -500, 1) });
    expect(result.current.captureElapsedMs()).toBe(2000);
    performanceMs = 2100;
    rerender({ synced: clock(2100, 5000, 2) });
    expect(result.current.captureElapsedMs()).toBe(7100);
  });

  it("同期anchor欠落・クリック直前の時計異常では端末日時へfallbackしない", () => {
    const { result, rerender } = renderHook(({ synced }) => useRoundClock(round, synced), {
      initialProps: { synced: { isSynchronized: true, now: start + 1000 } as ServerClock },
    });
    expect(result.current.captureElapsedMs()).toBeNull();
    rerender({ synced: { ...clock(1000), isCurrent: () => false } });
    expect(result.current.captureElapsedMs()).toBeNull();
  });

  it("経過は切捨て、開始前残秒は切上げで同じ整数msを整形する", () => {
    expect(elapsedSeconds(9999)).toBe("9.9");
    expect(elapsedSeconds(2399)).toBe("2.3");
    expect(remainingSeconds(1)).toBe("0.1");
    expect(remainingSeconds(1001)).toBe("1.1");
  });
});
