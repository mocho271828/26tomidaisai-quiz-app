import { readFile } from "node:fs/promises";
import { z } from "zod";
import { telemetrySchema } from "./evidence.js";
import { browserProbeSchema, serverTimingNames, serverTimingSchema } from "./browserDiagnostics.js";
import { analyzeContinuity } from "./continuityAnalysis.js";
import { analysisWindow, inCohort, windowSummary } from "./analysisWindow.js";
import { analyzeRequestFailures } from "./requestFailureAnalysis.js";

const eventSchema = z.object({event: z.string(), monotonicMs: z.number().finite(),
  lifecycleVersion: z.literal(2).optional(),
  phase: z.enum(["setup", "measurement", "shutdown"]).optional(),
  requestSequence: z.number().int().positive().optional(),
  serial: z.number().int().optional(), round: z.number().int().optional(),
  durationMs: z.number().finite().optional(), route: z.string().optional(), status: z.number().int().optional(),
  count: z.number().finite().optional(),
  cohort: z.object({count: z.number().int().min(1).max(100),
    ordinalStart: z.number().int().min(1).max(17576)}).strict().optional(),
  outcome: z.enum(["success", "http_error", "transport_error"]).optional(),
  telemetry: telemetrySchema.optional(),
  callbackLagMs: z.number().finite().optional(), deliveryLagMs: z.number().finite().optional(),
  callbackEpochDeltaUncalibratedMs: z.number().finite().optional(),
  callbackEpochDeltaReason: z.enum(["node_browser_epoch_not_calibrated", "playwright_finish_relative_origin_mismatch"]).optional(),
  networkDurationMs: z.number().finite().optional(), stage: z.string().optional(),
  browserProbe: browserProbeSchema.optional(),
  serverTiming: serverTimingSchema.optional(),
});
export type AnalysisEvent = z.infer<typeof eventSchema>;

export function distribution(values: number[]): {count: number; p50: number | null; p95: number | null; max: number | null} {
  const sorted = [...values].sort((a, b) => a - b);
  return {count: sorted.length, p50: sorted[Math.max(0, Math.ceil(sorted.length * 0.5) - 1)] ?? null,
    p95: sorted[Math.max(0, Math.ceil(sorted.length * 0.95) - 1)] ?? null, max: sorted.at(-1) ?? null};
}

async function readAnalysisEvents(path: string): Promise<AnalysisEvent[]> {
  return (await readFile(path, "utf8")).trim().split("\n")
    .filter(Boolean).map(line => eventSchema.parse(JSON.parse(line) as unknown));
}

export async function analyzeRequestFailureFile(path: string): Promise<ReturnType<typeof analyzeRequestFailures>> {
  const recorded = await readAnalysisEvents(path);
  const window = analysisWindow(recorded);
  return analyzeRequestFailures(window.measurement.filter(event => inCohort(event, window)), recorded,
    window.available && window.finishedObserved);
}

export async function analyze(path: string): Promise<object> {
  const recorded = await readAnalysisEvents(path);
  const window = analysisWindow(recorded);
  const events = window.measurement.filter(event => inCohort(event, window));
  const routes = ["time", "state", "join", "answer", "answer_status", "personal_result", "other"];
  const selected = new Map<string, number>();
  const receiptMs: number[] = [];
  const received = new Map<string, number>();
  const displayed = new Map<string, number>();
  const receivedToDisplayMs: number[] = [];
  const clockRttMs: number[] = [];
  const sending = new Map<string, number>();
  const browserAccepted = new Map<string, number>();
  const receiptInBrowser: number[] = [];
  for (const event of events) {
    const key = `${event.serial}:${event.round}`;
    if (event.event === "click_planned") selected.set(key, event.monotonicMs);
    const clicked = selected.get(key);
    if (event.event === "accepted_dom" && clicked !== undefined)
      receiptMs.push(event.monotonicMs - clicked);
    const observation = event.telemetry;
    if (observation?.kind === "answer" && observation.phase === "sending")
      sending.set(`${event.serial}:${observation.round_number}`, Math.min(
        sending.get(`${event.serial}:${observation.round_number}`) ?? Infinity, observation.monotonic_ms));
    const probe = event.browserProbe;
    if (probe?.kind === "dom" && probe.phase === "accepted" && probe.visible) {
      const roundKey = `${event.serial}:${probe.round_number}`;
      browserAccepted.set(roundKey, Math.min(browserAccepted.get(roundKey) ?? Infinity, probe.monotonic_ms));
    }
    if (observation?.kind === "clock_sample") clockRttMs.push(observation.rtt_ms);
    if (observation?.kind === "projection_received" && observation.has_question) {
      const roundKey = `${event.serial}:${observation.round_number}`;
      if (!received.has(roundKey)) received.set(roundKey, observation.monotonic_ms);
    }
    if (observation?.kind === "participant_display" && observation.has_question) {
      const roundKey = `${event.serial}:${observation.round_number}`;
      if (!displayed.has(roundKey)) displayed.set(roundKey, observation.monotonic_ms);
    }
  }
  // 独立bindingの到着順を因果順にせず、browser時計の初回同士を全件収集後に対応させる。
  for (const [roundKey, acceptedAt] of browserAccepted) {
    const sentAt = sending.get(roundKey);
    if (sentAt !== undefined && acceptedAt >= sentAt) receiptInBrowser.push(acceptedAt - sentAt);
  }
  // 同じ問の再取得・後続状態遷移を初回commitの所要時間へ混ぜない。
  for (const [roundKey, receivedAt] of received) {
    const displayedAt = displayed.get(roundKey);
    if (displayedAt !== undefined && displayedAt >= receivedAt)
      receivedToDisplayMs.push(displayedAt - receivedAt);
  }
  const cohort = window.cohort;
  const validCohort = cohort !== null;
  const recoveryEvents = validCohort ? events.filter(event => event.serial !== undefined) : [];
  return {
    measurementWindow: windowSummary(window),
    continuity: analyzeContinuity(recorded, window),
    requestFailureDiagnostics: analyzeRequestFailures(events, recorded, window.available && window.finishedObserved),
    participantRecoveryRequests: Object.fromEntries(["participant_state", "participant_result"].map(operation => {
      const settled = recoveryEvents.filter(event => event.telemetry?.kind === "api_request" &&
        event.telemetry.operation === operation && event.telemetry.phase === "settled");
      return [operation, {
        available: validCohort,
        denominator: validCohort ? cohort.count : null,
        requests: settled.length,
        participants: new Set(settled.flatMap(event => event.serial === undefined ? [] : [event.serial])).size,
        timeoutParticipants: new Set(settled.flatMap(event => event.telemetry?.kind === "api_request" &&
          event.telemetry.outcome === "timeout" && event.serial !== undefined ? [event.serial] : [])).size,
        outcomes: Object.fromEntries(["validated", "http_error", "contract_error", "transport_error", "aborted", "timeout"]
          .map(outcome => [outcome, settled.filter(event => event.telemetry?.kind === "api_request" && event.telemetry.outcome === outcome).length])),
        elapsedMs: distribution(settled.flatMap(event => event.telemetry?.kind === "api_request" ? [event.telemetry.elapsed_ms] : [])),
        timeoutElapsedMs: distribution(settled.flatMap(event => event.telemetry?.kind === "api_request" &&
          event.telemetry.outcome === "timeout" ? [event.telemetry.elapsed_ms] : [])),
      }];
    })),
    operatorMutationMs: Object.fromEntries(["prepare", "schedule", "publish", "advance", "complete", "visibility", "other"].map(route => [route,
      Object.fromEntries(["success", "http_error", "transport_error"].map(outcome => [outcome, distribution(events
        .filter(event => event.event === "operator_http" && event.route === route && event.outcome === outcome && event.durationMs !== undefined)
        .map(event => event.durationMs!))]))])),
    operatorMutationScope: "Playwright request→requestfinished/requestfailedのNode観測時間。HTTP本文完了までを含み、ブラウザDOM反映・API内部時間と区別する",
    http: Object.fromEntries(routes.map(route => [route, distribution(events
      .filter(event => event.event === "http" && event.route === route && event.durationMs !== undefined)
      .map(event => event.durationMs!))])),
    browserTransportMs: Object.fromEntries(routes.map(route => [route, distribution(events
      .filter(event => event.event === "http" && event.route === route && event.networkDurationMs !== undefined)
      .map(event => event.networkDurationMs!))])),
    playwrightCallbackLatencyMs: {available: false,
      reason: "Node/Chromium epoch差は未校正。Playwright responseEndはstartTimeと相対基準も異なるため、実遅延percentileへ採用しない"},
    playwrightCallbackEpochDeltaUncalibratedMs: Object.fromEntries(["http_response_headers", "http"].map(name => {
      const values = events.filter(event => event.event === name)
        .flatMap(event => {
          const value = event.callbackEpochDeltaUncalibratedMs ?? event.callbackLagMs;
          return value === undefined ? [] : [value];
        });
      return [name, {count: values.length, min: values.length ? values.reduce((value, next) => Math.min(value, next), Infinity) : null,
        max: values.length ? values.reduce((value, next) => Math.max(value, next), -Infinity) : null, negativeCount: values.filter(value => value < 0).length,
        reason: name === "http" ? "playwright_finish_relative_origin_mismatch" : "node_browser_epoch_not_calibrated"}];
    })),
    serverTimingMs: Object.fromEntries(serverTimingNames.map(name => [name, distribution(events
      .filter(event => event.event === "http_response_headers" && event.serverTiming?.[name] !== undefined)
      .map(event => event.serverTiming![name]!))])),
    serverTimingByRouteMs: Object.fromEntries(["answer", "answer_status", "state", "personal_result"].map(route => [route,
      Object.fromEntries(serverTimingNames.map(name => [name, distribution(events
        .filter(event => event.event === "http_response_headers" && event.route === route && event.serverTiming?.[name] !== undefined)
        .map(event => event.serverTiming![name]!))]))])),
    serverTimingScope: "API到着→headers生成と内訳。補助stageは重複し得るため合計しない。gateway待ち/転送/browser処理を含まない",
    generatorNodeLoopMs: {
      intervalP95: distribution(events.filter(event => event.event === "generator_node_loop_p95_ms").map(event => event.durationMs!)),
      intervalMax: distribution(events.filter(event => event.event === "generator_node_loop_max_ms").map(event => event.durationMs!)),
    },
    uiBindingDeliveryLagMs: distribution(events.filter(event => event.event === "ui_observation" && event.deliveryLagMs !== undefined).map(event => event.deliveryLagMs!)),
    probeBindingDeliveryLagMs: distribution(events.filter(event => event.event === "browser_probe" && event.deliveryLagMs !== undefined).map(event => event.deliveryLagMs!)),
    bindingDeliveryLagScope: "同一hostのbrowser→Node epoch差による推定。時計は未校正で、厳密なIPC遅延やAPI内部時間ではない。probeの5秒batch待ちは除く",
    browserResourceObserverLagMs: distribution(events.flatMap(event => event.browserProbe?.kind === "resource" && event.browserProbe.observer_lag_ms !== undefined ? [event.browserProbe.observer_lag_ms] : [])),
    browserResourceTimingMs: Object.fromEntries(["answer", "answer_status", "state", "personal_result"].map(route => [route,
      Object.fromEntries(["duration_ms", "first_byte_ms", "response_body_ms"].map(field => [field, distribution(events.flatMap(event => {
        const probe = event.browserProbe;
        if (probe?.kind !== "resource" || !probe.complete || probe.route !== route) return [];
        const value = probe[field as "duration_ms" | "first_byte_ms" | "response_body_ms"];
        return value === undefined ? [] : [value];
      }))]))])),
    browserLoopIntervalMaxMs: distribution(events.flatMap(event => event.browserProbe?.kind === "loop" ? [event.browserProbe.max_delay_ms] : [])),
    browserLongTaskMs: distribution(events.flatMap(event => event.browserProbe?.kind === "longtask" ? [event.browserProbe.duration_ms] : [])),
    browserProbeDropped: events.reduce((count, event) => count + (event.browserProbe?.kind === "overflow" || event.browserProbe?.kind === "delivery_failure" ? event.browserProbe.count : 0), 0),
    browserIncompleteResources: events.filter(event => event.browserProbe?.kind === "resource" && !event.browserProbe.complete).length,
    browserSendingToReceiptDomMs: distribution(receiptInBrowser),
    browserReceiptDomScope: "browserのMutationObserverで可視受付DOMを初回観測。描画/paint完了時間ではない。Node受信時刻や5秒batch待ちを含まない",
    actorFailureStages: Object.fromEntries([...new Set(events.filter(event => event.event === "actor_failed").map(event => event.stage ?? "unclassified"))]
      .map(stage => [stage, events.filter(event => event.event === "actor_failed" && (event.stage ?? "unclassified") === stage).length])),
    attemptCounts: {
      roundExpected: events.filter(event => event.event === "round_expected").length,
      clickPlanned: events.filter(event => event.event === "click_planned").length,
      sending: events.filter(event => event.telemetry?.kind === "answer" && event.telemetry.phase === "sending").length,
      acceptedDom: events.filter(event => event.event === "accepted_dom").length,
      resultDom: events.filter(event => event.event === "result_dom").length,
    },
    attemptCountsScope: "新版は失敗を保持して後続問も試行。旧版は最初の失敗で残り未試行だったため件数を直接同条件比較しない",
    answerClickToReceiptDomMs: distribution(receiptMs),
    receivedToDisplayCommitMs: distribution(receivedToDisplayMs),
    receivedToDisplayCommitScope: "初回projection受信→初回React状態表示commit（問題本文の可視化や予定待機の測定ではない）",
    clockRttMs: distribution(clockRttMs),
    serverErrors: events.filter(event => event.event === "http" && (event.status ?? 0) >= 500).length,
    requestFailures: events.filter(event => event.event === "request_failed").length,
    invalidObservations: events.filter(event => event.event === "invalid_ui_observation").length,
    cpuPercent: distribution(events.filter(event => event.event === "generator_cpu_percent" && event.monotonicMs >= 500).map(event => event.durationMs!)),
    cpuPercentScope: "ホストCPU差分。旧collectorの比較区間がない初期sample（開始後500ms未満）を除外",
    memoryUsedBytes: distribution(events.filter(event => event.event === "generator_memory_used_bytes").map(event => event.count!)),
    memoryUsedBytesScope: "ホスト総メモリ−free。macOSの再利用可能cacheを含み、専用browser使用量やメモリ圧力の指標ではない",
    swapUsedBytes: distribution(events.filter(event => event.event === "generator_swap_used_bytes").map(event => event.count!)),
    scope: "single generator host; per-metric clock boundaries and uncalibrated epoch estimates; no cross-host latency comparison",
  };
}

if (process.argv[1]?.endsWith("analyze.js")) {
  const path = process.argv[2];
  if (!path) throw new Error("events_path_required");
  analyze(path).then(result => process.stdout.write(JSON.stringify(result, null, 2) + "\n"))
    .catch(() => { process.stderr.write("invalid_evidence\n"); process.exitCode = 1; });
}
