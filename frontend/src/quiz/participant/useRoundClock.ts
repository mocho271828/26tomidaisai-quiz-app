import { useCallback, useRef } from "react";
import type { ParticipantCurrentRound } from "../../api/contracts";
import type { ServerClock } from "../_hooks/useServerClock";

type RoundAnchor = {
  identity: string;
  epoch: number;
  openAtMs: number;
  durationMs: number;
  openAtPerformanceMs: number;
  lastElapsedMs: number;
};

export type RoundClock = {
  now: number;
  isSynchronized: boolean;
  deadlinePerformanceMs?: number;
  readNow?: () => number;
  captureElapsedMs: () => number | null;
};

// Roundの予約時刻を固定し、通常の同期補正や描画周期で選択時刻を動かさない。
export function useRoundClock(round: ParticipantCurrentRound | null, clock: ServerClock): RoundClock {
  const anchorRef = useRef<RoundAnchor | null>(null);
  const openAtMs = round?.open_at == null ? Number.NaN : Date.parse(round.open_at);
  const closeAtMs = round?.standard_close_at == null ? Number.NaN : Date.parse(round.standard_close_at);
  const identity = `${round?.round_id}:${round?.open_at}:${round?.standard_close_at}`;
  const canAnchor = clock.isSynchronized && clock.monotonicOffsetMs !== undefined &&
    Number.isFinite(openAtMs) && Number.isFinite(closeAtMs);
  let anchor = anchorRef.current;
  if (anchor?.identity !== identity) anchor = null;
  if (canAnchor && (anchor === null || anchor.epoch !== (clock.epoch ?? 0))) {
    const performanceMs = performance.now();
    const initialElapsedMs = performanceMs - (openAtMs + clock.monotonicOffsetMs!);
    const elapsedMs = Math.max(initialElapsedMs, anchor?.lastElapsedMs ?? Number.NEGATIVE_INFINITY);
    anchor = { identity, epoch: clock.epoch ?? 0, openAtMs, durationMs: closeAtMs - openAtMs,
      openAtPerformanceMs: performanceMs - elapsedMs, lastElapsedMs: elapsedMs };
  }
  anchorRef.current = anchor;
  const renderedPerformanceMs = clock.monotonicOffsetMs === undefined
    ? performance.now() : (clock.readNow?.() ?? clock.now) + clock.monotonicOffsetMs;
  const elapsedMs = anchor === null ? Number.NaN
    : Math.max(anchor.lastElapsedMs, renderedPerformanceMs - anchor.openAtPerformanceMs);
  if (anchor !== null) anchor.lastElapsedMs = elapsedMs;
  const readNow = useCallback(() => {
    if (anchor === null) return clock.readNow?.() ?? clock.now;
    anchor.lastElapsedMs = Math.max(anchor.lastElapsedMs, performance.now() - anchor.openAtPerformanceMs);
    return anchor.openAtMs + anchor.lastElapsedMs;
  }, [anchor, clock]);
  return {
    now: anchor === null ? clock.now : anchor.openAtMs + elapsedMs,
    isSynchronized: canAnchor && anchor !== null,
    ...(clock.readNow === undefined ? {} : { readNow }),
    ...(anchor === null ? {} : { deadlinePerformanceMs: anchor.openAtPerformanceMs + anchor.durationMs }),
    captureElapsedMs: () => {
      // クリック冒頭の1回だけの採時。表示tickの値や端末の日時設定を代用しない。
      const selectedPerformanceMs = performance.now();
      if (!canAnchor || anchor === null || clock.isCurrent?.() === false) return null;
      const captured = Math.max(anchor.lastElapsedMs, selectedPerformanceMs - anchor.openAtPerformanceMs);
      if (captured < 0 || captured >= anchor.durationMs) return null;
      anchor.lastElapsedMs = captured;
      return Math.floor(captured);
    },
  };
}
