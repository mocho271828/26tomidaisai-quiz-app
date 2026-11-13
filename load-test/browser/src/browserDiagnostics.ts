import type { Page } from "playwright";
import { z } from "zod";
import type { Evidence } from "./evidence.js";
import { MAX_MANUAL_ROUNDS, roundNumberSchema } from "./controlRounds.js";

export const serverTimingNames = ["api_total", "pre_handler", "participant_metadata", "recovery_wait", "common_state_wait", "thread_wait", "dispatch_wait", "handler", "db_acquire",
  "db_lock_query", "answer_transaction", "state_projection", "status_projection", "redis_publish", "response_build"] as const;
export const serverTimingSchema = z.partialRecord(z.enum(serverTimingNames), z.number().finite().nonnegative());
const clock = {monotonic_ms: z.number().finite().nonnegative(), wall_time_ms: z.number().finite()};
export const browserProbeSchema = z.discriminatedUnion("kind", [
  z.object({kind: z.literal("resource"), route: z.enum(["answer", "answer_status", "state", "personal_result"]),
    complete: z.boolean(), duration_ms: z.number().finite().nonnegative(), first_byte_ms: z.number().finite().nonnegative().optional(),
    response_body_ms: z.number().finite().nonnegative().optional(), observer_lag_ms: z.number().finite().nonnegative().optional(),
    server_timing: serverTimingSchema, ...clock}).strict(),
  z.object({kind: z.literal("dom"), phase: z.enum(["accepted", "result"]),
    round_number: roundNumberSchema, visible: z.boolean(), unanswered: z.boolean(), ...clock}).strict(),
  z.object({kind: z.literal("longtask"), duration_ms: z.number().finite().nonnegative(), ...clock}).strict(),
  z.object({kind: z.literal("loop"), max_delay_ms: z.number().finite().nonnegative(), ...clock}).strict(),
  z.object({kind: z.literal("overflow"), count: z.number().int().positive(), ...clock}).strict(),
  z.object({kind: z.literal("delivery_failure"), count: z.number().int().positive(), ...clock}).strict(),
]);
export type BrowserProbe = z.infer<typeof browserProbeSchema>;
const probeBatchSchema = z.object({sent_wall_time_ms: z.number().finite(),
  records: z.array(browserProbeSchema).max(128)}).strict();

export function parseServerTiming(header: string | undefined): Record<string, number> {
  const result: Record<string, number> = {};
  for (const part of (header ?? "").split(",")) {
    const match = /^\s*([a-z_]+)\s*;\s*dur\s*=\s*(\d+(?:\.\d+)?)\s*$/.exec(part);
    if (!match || !serverTimingNames.some(name => name === match[1])) continue;
    const duration = Number(match[2]);
    if (Number.isFinite(duration)) result[match[1]!] = duration;
  }
  return result;
}

export function callbackEpochDelta(wallNow: number, startTime: number, relativeTime: number): number | undefined {
  // Node/Chromiumのepochは未校正。特にPlaywright responseEndはstartTimeと相対基準が異なる。
  // この符号付き差は診断材料に限定し、実際のIPC遅延の標本として使わない。
  return startTime > 0 && relativeTime >= 0 ? wallNow - startTime - relativeTime : undefined;
}

export async function installBrowserDiagnostics(page: Page, serial: number, evidence: Evidence): Promise<void> {
  await page.exposeBinding("recordRunnerProbe", (_source, input: unknown) => {
    const parsed = probeBatchSchema.safeParse(input);
    if (!parsed.success) { evidence.event({event: "invalid_browser_probe", serial}); return; }
    const deliveryLagMs = evidence.wallNow() - parsed.data.sent_wall_time_ms;
    for (const browserProbe of parsed.data.records) {
      evidence.event({event: "browser_probe", serial, deliveryLagMs, browserProbe});
      if (browserProbe.kind === "overflow" || browserProbe.kind === "delivery_failure")
        evidence.event({event: `browser_probe_${browserProbe.kind}`, serial, count: browserProbe.count});
    }
  });
  await page.addInitScript(({names, maxRounds}) => {
    const records: BrowserProbe[] = [];
    let overflow = 0;
    let deliveryFailures = 0;
    let pending: Promise<void> | null = null;
    const now = (): {monotonic_ms: number; wall_time_ms: number} => ({
      monotonic_ms: performance.now(), wall_time_ms: performance.timeOrigin + performance.now(),
    });
    const push = (record: BrowserProbe): void => {
      if (records.length < 126) records.push(record); else overflow += 1;
    };
    const route = (raw: string): "answer" | "answer_status" | "state" | "personal_result" | null => {
      const path = new URL(raw).pathname;
      if (/\/rounds\/[^/]+\/answers$/.test(path)) return "answer";
      if (/\/answers\/[^/]+\/status$/.test(path)) return "answer_status";
      if (path.endsWith("/participant/result")) return "personal_result";
      return path.endsWith("/participant/session") ? "state" : null;
    };
    // URL、本文、Cookieは保持せず、browser側のtransportとobserver遅延だけを採る。
    new PerformanceObserver(list => {
      for (const entry of list.getEntries() as PerformanceResourceTiming[]) {
        const classified = route(entry.name);
        if (classified === null) continue;
        const serverTiming: Record<string, number> = {};
        for (const timing of entry.serverTiming)
          if (names.some(name => name === timing.name) && Number.isFinite(timing.duration) && timing.duration >= 0)
            serverTiming[timing.name] = timing.duration;
        const complete = entry.responseStart > 0 && entry.responseEnd >= entry.responseStart;
        push({kind: "resource", route: classified, complete, duration_ms: entry.duration,
          ...(complete ? {
            ...(entry.requestStart > 0 && entry.responseStart >= entry.requestStart ? {first_byte_ms: entry.responseStart - entry.requestStart} : {}),
            response_body_ms: entry.responseEnd - entry.responseStart,
            observer_lag_ms: Math.max(0, performance.now() - entry.responseEnd),
          } : {}), server_timing: serverTiming, ...now()});
      }
    }).observe({type: "resource", buffered: true});
    if (PerformanceObserver.supportedEntryTypes.includes("longtask"))
      new PerformanceObserver(list => {
        for (const entry of list.getEntries()) push({kind: "longtask", duration_ms: entry.duration, ...now()});
      }).observe({type: "longtask", buffered: true});
    const seen = new Set<string>();
    const scan = (): void => {
      for (const [selector, phase] of [[".participant-question .answer-state-accepted", "accepted"],
        [".participant-result-stage .participant-score-result", "result"]] as const) {
        const element = document.querySelector<HTMLElement>(selector);
        if (!element || element.classList.contains("participant-result-pending")) continue;
        const stage = element.closest(".participant-question, .participant-result-stage");
        const round = Number(stage?.querySelector(phase === "result" ? ".participant-result-round" : ".participant-round-badge")?.getAttribute("data-round-number"));
        if (round < 1 || round > maxRounds || !Number.isSafeInteger(round)) continue;
        const style = getComputedStyle(element);
        const visible = element.getClientRects().length > 0 && style.visibility !== "hidden" && style.display !== "none";
        const key = `${phase}:${round}:${visible}`;
        if (seen.has(key)) continue;
        seen.add(key);
        push({kind: "dom", phase, round_number: round, visible,
          unanswered: stage !== null && stage.querySelector(".outcome-unanswered") !== null, ...now()});
      }
    };
    new MutationObserver(scan).observe(document, {subtree: true, childList: true, attributes: true,
      attributeFilter: ["class", "hidden", "style", "data-round-number"]});
    document.addEventListener("DOMContentLoaded", scan, {once: true});
    let prior = performance.now();
    let maxDelay = 0;
    let ticks = 0;
    const flush = async (): Promise<void> => {
      // IPC待ちでbatchが増殖しないよう、各pageは常に1送信だけに制限する。
      if (pending !== null) await pending;
      if (overflow > 0) records.push({kind: "overflow", count: overflow, ...now()});
      if (deliveryFailures > 0) records.push({kind: "delivery_failure", count: deliveryFailures, ...now()});
      if (records.length === 0) return;
      const target = window as unknown as {recordRunnerProbe: (value: unknown) => Promise<void>};
      const batch = records.splice(0);
      pending = target.recordRunnerProbe({sent_wall_time_ms: performance.timeOrigin + performance.now(), records: batch});
      overflow = 0; deliveryFailures = 0;
      try { await pending; }
      catch { deliveryFailures += batch.length; throw new Error("runner_probe_delivery_failed"); }
      finally { pending = null; }
    };
    const timer = setInterval(() => {
      const current = performance.now();
      maxDelay = Math.max(maxDelay, current - prior - 1000);
      prior = current;
      if (++ticks < 5) return;
      push({kind: "loop", max_delay_ms: maxDelay, ...now()});
      // 欠測件数は次回batchへ記録し、終了時のflush失敗もNode側で不合格にする。
      if (pending === null) void flush().catch(() => {});
      maxDelay = 0; ticks = 0;
    }, 1000);
    (window as unknown as {flushRunnerProbe: () => Promise<void>}).flushRunnerProbe = async () => {
      clearInterval(timer);
      if (ticks > 0 || performance.now() - prior > 1000)
        push({kind: "loop", max_delay_ms: Math.max(maxDelay, performance.now() - prior - 1000), ...now()});
      await flush();
    };
  }, {names: [...serverTimingNames], maxRounds: MAX_MANUAL_ROUNDS});
}

export async function flushBrowserDiagnostics(page: Page): Promise<void> {
  await page.evaluate(async () => {
    const target = window as unknown as {flushRunnerProbe?: () => Promise<void>};
    await target.flushRunnerProbe?.();
  });
}
