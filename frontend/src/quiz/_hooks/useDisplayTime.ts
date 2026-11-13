import { useEffect, useState } from "react";

export type DisplayClock = { now: number; readNow?: () => number };

/** 0.1秒の描画更新を時刻表示の葉に閉じ、回答済み・待機画面へ伝播させない。 */
export function useDisplayTime(clock: DisplayClock, until: number, enabled = true): number {
  const { now, readNow } = clock;
  const [sample, setSample] = useState({ readNow, now });
  const current = readNow === sample.readNow ? Math.max(now, sample.now) : now;
  useEffect(() => {
    if (!enabled || readNow === undefined || readNow() >= until) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState !== "visible") return;
      const next = readNow();
      setSample({ readNow, now: next });
      if (next >= until) window.clearInterval(timer);
    }, 100);
    return () => window.clearInterval(timer);
  }, [enabled, readNow, until]);
  return enabled ? current : now;
}

/** 問題の公開・締切・再送期限だけで親を更新し、判定自体は単調時計から再計算する。 */
export function useTimeBoundary(clock: DisplayClock, boundaries: readonly number[]): void {
  const [, update] = useState(0);
  const { readNow } = clock;
  const now = readNow?.() ?? clock.now;
  const next = Math.min(...boundaries.filter(value => Number.isFinite(value) && value > now));
  useEffect(() => {
    if (readNow === undefined || !Number.isFinite(next)) return;
    let timer: number;
    const wake = (): void => {
      const remaining = next - readNow();
      if (remaining <= 0) { update(value => value + 1); return; }
      // 小数msの切捨てによる早起きやsetTimeoutの32bit上限で境界通知を失わない。
      timer = window.setTimeout(wake, Math.min(2_147_483_647, Math.max(1, Math.ceil(remaining))));
    };
    wake();
    return () => window.clearTimeout(timer);
  }, [next, readNow]);
}
