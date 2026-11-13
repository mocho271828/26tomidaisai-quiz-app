import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { observeBrowser } from "../_utils/browserObservation";

import { getServerTime } from "../../api/audienceClient";
import { ApiCancellation, forwardedCancellation } from "../../api/cancellation";

export const CLOCK_RECHECK_EVENT = "quiz:clock-recheck";

export type ServerClock = {
  isSynchronized: boolean;
  now: number;
  monotonicOffsetMs?: number;
  epoch?: number;
  isCurrent?: () => boolean;
  readNow?: () => number;
};

type ClockSample = {
  estimatedServerTimeAtEndMs: number;
  endedAtPerformanceMs: number;
  roundTripTimeMs: number;
};

type ClockAnchor = {
  serverTimeMs: number;
  performanceTimeMs: number;
};

const CLOCK_SAMPLE_COUNT = 3;
const CLOCK_SAMPLE_TIMEOUT_MS = 1_000;
const MAX_CLOCK_ROUND_TRIP_TIME_MS = 500;
const CLOCK_SYNC_INTERVAL_MS = 120_000;
const CLOCK_SYNC_JITTER_MS = 12_000;
const CLOCK_TICK_INTERVAL_MS = 100;
const CLOCK_RETRY_BASE_MS = 1_000;
const CLOCK_RETRY_MAX_MS = 5_000;

function selectShortestRoundTripSample(samples: ClockSample[]): ClockSample | null {
  return samples.reduce<ClockSample | null>((shortest, sample) => {
    if (shortest === null || sample.roundTripTimeMs < shortest.roundTripTimeMs) {
      return sample;
    }
    return shortest;
  }, null);
}

async function takeClockSample(parentSignal: AbortSignal): Promise<ClockSample | null> {
  const sampleController = new AbortController();
  let rejectCancellation: (reason: DOMException) => void = () => undefined;
  const cancellation = new Promise<never>((_resolve, reject) => {
    rejectCancellation = reject;
  });
  const cancelSample = (reason: DOMException): void => {
    sampleController.abort(reason);
    rejectCancellation(reason);
  };
  const onParentAbort = (): void => {
    cancelSample(forwardedCancellation(parentSignal));
  };
  if (parentSignal.aborted) {
    onParentAbort();
  } else {
    parentSignal.addEventListener("abort", onParentAbort, { once: true });
  }
  const timeout = window.setTimeout(() => {
    cancelSample(new ApiCancellation("deadline"));
  }, CLOCK_SAMPLE_TIMEOUT_MS);
  const startedAtPerformanceMs = performance.now();
  let response: Awaited<ReturnType<typeof getServerTime>>;
  try {
    response = await Promise.race([
      getServerTime(sampleController.signal),
      cancellation,
    ]);
  } finally {
    window.clearTimeout(timeout);
    parentSignal.removeEventListener("abort", onParentAbort);
  }
  const endedAtPerformanceMs = performance.now();
  const serverTimeMs = Date.parse(response.server_time);
  // 共有DB照会への遅い合流を短RTTと誤認しないよう、flight開始まで遡る。
  const effectiveStartedAtPerformanceMs = startedAtPerformanceMs - response.shared_sample_age_ms;
  const roundTripTimeMs = endedAtPerformanceMs - effectiveStartedAtPerformanceMs;

  if (
    !Number.isFinite(serverTimeMs) ||
    !Number.isFinite(roundTripTimeMs) ||
    !Number.isFinite(response.shared_sample_age_ms) || response.shared_sample_age_ms < 0 ||
    roundTripTimeMs < 0
  ) {
    return null;
  }

  observeBrowser({ kind: "clock_sample", rtt_ms: roundTripTimeMs, offset_ms: serverTimeMs + roundTripTimeMs / 2 - Date.now() });
  return {
    // server_timeが往復の中点を表すと仮定し、受信時点までの片道分を進める。
    estimatedServerTimeAtEndMs: serverTimeMs + roundTripTimeMs / 2,
    endedAtPerformanceMs,
    roundTripTimeMs,
  };
}

function serverTimeAt(anchor: ClockAnchor, performanceTimeMs: number): number {
  return anchor.serverTimeMs + Math.max(0, performanceTimeMs - anchor.performanceTimeMs);
}

export function useServerClock(): ServerClock {
  const [epoch, setEpoch] = useState(0);
  const validateClockRef = useRef<() => boolean>(() => false);
  const [clockAnchor, setClockAnchor] = useState<ClockAnchor | null>(null);

  useEffect(() => {
    let controller: AbortController | null = null;
    let sequence = 0;
    let syncTimer: number | undefined;
    let failureCount = 0;
    let previousVisibility = document.visibilityState;
    const scheduleSync = (delayMs: number): void => {
      window.clearTimeout(syncTimer);
      syncTimer = window.setTimeout(() => void synchronize(), delayMs);
    };

    const synchronize = async (): Promise<void> => {
      if (document.visibilityState !== "visible" || controller !== null) {
        return;
      }

      sequence += 1;
      const requestSequence = sequence;
      const currentController = new AbortController();
      controller = currentController;
      const samples: ClockSample[] = [];
      try {
        for (let sampleIndex = 0; sampleIndex < CLOCK_SAMPLE_COUNT; sampleIndex += 1) {
          try {
            const sample = await takeClockSample(currentController.signal);
            if (requestSequence !== sequence || currentController.signal.aborted) {
              return;
            }
            if (sample !== null) {
              samples.push(sample);
            }
          } catch {
            if (requestSequence !== sequence || currentController.signal.aborted) {
              return;
            }
            // 単一標本の失敗では、残りの有効標本による同期を継続する。
          }
        }

        if (requestSequence !== sequence || currentController.signal.aborted) {
          return;
        }

        const shortestSample = selectShortestRoundTripSample(samples);
        if (
          shortestSample === null ||
          shortestSample.roundTripTimeMs > MAX_CLOCK_ROUND_TRIP_TIME_MS
        ) {
          hasValidClock = false;
          setClockAnchor(null);
          // 失敗時は通常周期を待たず再標本化する。上限付きjitterで同時復帰を分散する。
          const ceiling = Math.min(CLOCK_RETRY_MAX_MS, CLOCK_RETRY_BASE_MS * 2 ** Math.min(failureCount, 3));
          failureCount += 1;
          const retryMs = Math.round(ceiling * (0.5 + Math.random() / 2));
          observeBrowser({ kind: "clock_sync", outcome: "retry", retry_ms: retryMs });
          scheduleSync(retryMs);
          return;
        }

        const appliedAtPerformanceMs = performance.now();
        const sampledServerTimeAtApplyMs =
          shortestSample.estimatedServerTimeAtEndMs +
          Math.max(0, appliedAtPerformanceMs - shortestSample.endedAtPerformanceMs);
        setClockAnchor((currentAnchor) => {
          const currentServerTimeAtApplyMs =
            currentAnchor === null
              ? Number.NEGATIVE_INFINITY
              : serverTimeAt(currentAnchor, appliedAtPerformanceMs);
          return {
            // 再同期の補正値が小さくても、利用者へ見せる時計は後退させない。
            serverTimeMs: Math.max(
              sampledServerTimeAtApplyMs,
              currentServerTimeAtApplyMs,
            ),
            performanceTimeMs: appliedAtPerformanceMs,
          };
        });
        hasValidClock = true;
        previousPerformanceMs = appliedAtPerformanceMs;
        previousWallMs = Date.now();
        failureCount = 0;
        const nextSyncMs = CLOCK_SYNC_INTERVAL_MS + Math.round(Math.random() * CLOCK_SYNC_JITTER_MS);
        observeBrowser({ kind: "clock_sync", outcome: "synchronized", retry_ms: nextSyncMs });
        scheduleSync(nextSyncMs);
      } finally {
        if (controller === currentController) controller = null;
      }
    };

    let previousPerformanceMs = performance.now();
    let previousWallMs = Date.now();
    let hasValidClock = false;
    const recheck = (shouldRefreshState = true): void => {
      sequence += 1;
      controller?.abort(new ApiCancellation("superseded"));
      controller = null;
      window.clearTimeout(syncTimer);
      hasValidClock = false;
      setClockAnchor(null);
      setEpoch((value) => value + 1);
      previousPerformanceMs = performance.now();
      previousWallMs = Date.now();
      if (shouldRefreshState) window.dispatchEvent(new Event(CLOCK_RECHECK_EVENT));
      void synchronize();
    };
    const isClockCurrent = (): boolean => {
      const performanceMs = performance.now();
      const wallMs = Date.now();
      const monotonicElapsed = performanceMs - previousPerformanceMs;
      const wallElapsed = wallMs - previousWallMs;
      previousPerformanceMs = performanceMs;
      previousWallMs = wallMs;
      // 日時変更は計測に使わず、sleepやcallback停止の可能性として再確認する。
      if (monotonicElapsed > 1_500 || Math.abs(wallElapsed - monotonicElapsed) > 1_000) {
        recheck();
        return false;
      }
      return hasValidClock && document.visibilityState === "visible";
    };
    validateClockRef.current = isClockCurrent;
    void synchronize();
    const tickTimer = window.setInterval(() => {
      if (document.visibilityState === "visible") {
        isClockCurrent();
        // 時計の有効性は常時確認するが、表示のtickでページ全体を再描画しない。
      }
    }, CLOCK_TICK_INTERVAL_MS);
    const onVisibilityChange = (): void => {
      if (previousVisibility === document.visibilityState) return;
      previousVisibility = document.visibilityState;
      // 画面状態の復帰取得は既存visibility経路へ集約する。
      recheck(false);
    };
    const onOnline = (): void => { if (document.visibilityState === "visible" && controller === null) recheck(); };
    const onPageShow = (event: PageTransitionEvent): void => { if (event.persisted) recheck(); };
    document.addEventListener("visibilitychange", onVisibilityChange);
    window.addEventListener("online", onOnline);
    window.addEventListener("pageshow", onPageShow);
    return () => {
      sequence += 1;
      controller?.abort(new ApiCancellation("unmount"));
      window.clearTimeout(syncTimer);
      window.clearInterval(tickTimer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      window.removeEventListener("online", onOnline);
      window.removeEventListener("pageshow", onPageShow);
      validateClockRef.current = () => false;
    };
  }, []);

  const isCurrent = useCallback(() => validateClockRef.current(), []);
  return useMemo(() => ({
    isSynchronized: clockAnchor !== null,
    epoch,
    isCurrent,
    readNow: () => clockAnchor === null ? Date.now() : serverTimeAt(clockAnchor, performance.now()),
    ...(clockAnchor === null ? {} : {monotonicOffsetMs: clockAnchor.performanceTimeMs - clockAnchor.serverTimeMs}),
    now:
      clockAnchor === null
        ? Date.now()
        : serverTimeAt(clockAnchor, performance.now()),
  }), [clockAnchor, epoch, isCurrent]);
}
