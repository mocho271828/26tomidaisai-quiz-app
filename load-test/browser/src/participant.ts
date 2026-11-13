import type { Browser, BrowserContext, Page, Request, WebSocket } from "playwright";
import type { BrowserRunConfig, RunConfig } from "./config.js";
import { Evidence, telemetrySchema, type Telemetry } from "./evidence.js";
import { callbackEpochDelta, installBrowserDiagnostics, parseServerTiming } from "./browserDiagnostics.js";
import { cancellableDelay, fixedFailureReason, type RunLifecycle } from "./lifecycle.js";
import { recordParticipantIdentity } from "./participantIdentity.js";

export function routeClass(raw: string): string {
  const path = new URL(raw).pathname;
  if (/\/rounds\/[^/]+\/answers$/.test(path)) return "answer";
  if (/\/answers\/[^/]+\/status$/.test(path)) return "answer_status";
  if (path.endsWith("/participant/session")) return "state";
  if (path.endsWith("/participant/result")) return "personal_result";
  if (path.endsWith("/time")) return "time";
  if (path.endsWith("/participant/join")) return "join";
  return "other";
}

export type Actor = { context: BrowserContext; page: Page; serial: number;
  connected: boolean; joined: boolean; complete: boolean; failed: boolean;
  stopped: boolean;
  wsOpens: number; wsCloses: number;
  rounds: number[]; results: number[]; failures: {round: number; stage: string}[]; pending: Promise<void> };

export type ActorOptions = {
  detailedDiagnostics?: boolean;
  onTelemetry?: (actor: Actor, telemetry: Telemetry) => void;
};

export async function createActor(browser: Browser, serial: number, config: BrowserRunConfig,
  evidence: Evidence, lifecycle?: RunLifecycle, options: ActorOptions = {}): Promise<Actor> {
  const context = await browser.newContext({ viewport: {width: 390, height: 844}, locale: "ja-JP" });
  const page = await context.newPage();
  page.setDefaultTimeout(30000);
  const actor: Actor = {context, page, serial, connected: false, joined: false,
    complete: false, failed: false, stopped: false, wsOpens: 0, wsCloses: 0,
    rounds: [], results: [], failures: [], pending: Promise.resolve()};
  const requests = new WeakMap<Request, number>();
  const detailed = options.detailedDiagnostics !== false;
  if (detailed) page.on("request", request => { requests.set(request, performance.now()); });
  if (detailed) page.on("response", response => {
    const request = response.request();
    const timing = request.timing();
    const route = routeClass(request.url());
    if (!["answer", "answer_status", "state", "personal_result"].includes(route)) return;
    evidence.event({event: "http_response_headers", serial, route, status: response.status(),
      callbackEpochDeltaUncalibratedMs: callbackEpochDelta(evidence.wallNow(), timing.startTime, timing.responseStart),
      callbackEpochDeltaReason: "node_browser_epoch_not_calibrated",
      serverTiming: parseServerTiming(response.headers()["server-timing"])});
  });
  if (!detailed) page.on("response", response => {
    const code = response.status();
    if (code >= 500 && config.scenario === "normal" && !lifecycle?.signal.aborted) actor.failed = true;
    if (code >= 400 && !(code === 401 && !actor.joined && routeClass(response.url()) === "state"))
      evidence.event({event: "http_error", serial, route: routeClass(response.url()), status: code});
  });
  if (detailed) page.on("requestfinished", request => {
    const started = requests.get(request);
    const observedAt = performance.now();
    const callbackWall = evidence.wallNow();
    const status = request.response();
    void status.then(response => {
      const code = response?.status() ?? 0;
      if (code >= 500 && config.scenario === "normal" && !lifecycle?.signal.aborted) actor.failed = true;
      const timing = request.timing();
      evidence.event({ event: "http", serial,
        route: routeClass(request.url()), status: code, requestSequence: requestSequence(request),
        phase: lifecycle?.phase,
        durationMs: started === undefined ? undefined : observedAt - started,
        networkDurationMs: timing.responseEnd >= 0 ? timing.responseEnd : undefined,
        callbackEpochDeltaUncalibratedMs: callbackEpochDelta(callbackWall, timing.startTime, timing.responseEnd),
        callbackEpochDeltaReason: "playwright_finish_relative_origin_mismatch", timing: {
          dnsMs: timing.domainLookupEnd < 0 ? -1 : timing.domainLookupEnd - timing.domainLookupStart,
          connectMs: timing.connectEnd < 0 ? -1 : timing.connectEnd - timing.connectStart,
          tlsMs: timing.secureConnectionStart < 0 ? -1 : timing.connectEnd - timing.secureConnectionStart,
          firstByteMs: timing.responseStart < 0 ? -1 : timing.responseStart - timing.requestStart,
        },
      });
    }).catch(() => evidence.event({event: "http_observation_failed", serial}));
  });
  page.on("requestfailed", request => evidence.event({event: "request_failed", serial,
    route: routeClass(request.url()), requestSequence: requestSequence(request), phase: lifecycle?.phase}));
  page.on("pageerror", () => {
    if (!lifecycle?.signal.aborted) actor.failed = true;
    evidence.event({event: "page_error", serial, phase: lifecycle?.phase});
  });
  page.on("crash", () => {
    if (!lifecycle?.signal.aborted) actor.failed = true;
    evidence.event({event: "page_crash", serial, phase: lifecycle?.phase});
  });
  let currentSocket: WebSocket | undefined;
  page.on("websocket", socket => {
    currentSocket = socket;
    evidence.event({event: "ws_created", serial});
    socket.on("close", () => {
      // 旧接続の遅いclose通知で、すでに開いた新接続の状態を消さない。
      if (currentSocket !== socket) {
        evidence.event({event: "ws_obsolete_closed", serial, phase: lifecycle?.phase});
        return;
      }
      actor.connected = false;
      actor.wsCloses += 1;
      if (config.scenario === "normal" && actor.joined && !actor.complete && !lifecycle?.signal.aborted) actor.failed = true;
      evidence.event({event: "ws_disconnected", serial, phase: lifecycle?.phase});
    });
  });
  await page.exposeBinding("recordQuizObservation", (_source, input: unknown) => {
    const result = telemetrySchema.safeParse(input);
    if (result.success) {
      if (result.data.kind === "ws" && result.data.audience === "participant") {
        if (result.data.phase === "open") actor.wsOpens += 1;
        if (result.data.phase === "open" || result.data.phase === "stable") actor.connected = true;
        if (result.data.phase === "closed" || result.data.phase === "retry") actor.connected = false;
      }
      options.onTelemetry?.(actor, result.data);
      evidence.event({event: "ui_observation", serial, telemetry: result.data,
        deliveryLagMs: evidence.wallNow() - result.data.wall_time_ms});
    } else evidence.event({event: "invalid_ui_observation", serial});
  });
  await page.addInitScript(({detailed}) => {
    window.addEventListener("quiz:observation", event => {
      const target = window as unknown as { recordQuizObservation: (value: unknown) => Promise<void>;
        quizRunnerProgress?: {round: number; state: string} };
      const detail: unknown = (event as CustomEvent<unknown>).detail;
      if (typeof detail === "object" && detail !== null && "kind" in detail &&
        detail.kind === "projection_received" && "round_number" in detail && typeof detail.round_number === "number" &&
        "state" in detail && typeof detail.state === "string")
        target.quizRunnerProgress = {round: detail.round_number, state: detail.state};
      if (detailed || (typeof detail === "object" && detail !== null && "kind" in detail &&
        ["ws", "projection_received", "participant_display", "answer", "answer_dom", "answer_timeout"].includes(String(detail.kind))))
        void target.recordQuizObservation(detail);
    });
  }, {detailed});
  if (detailed) await installBrowserDiagnostics(page, serial, evidence);
  evidence.event({event: "actor_created", serial, phase: "setup", lifecycleVersion: 2});
  return actor;
}

export function requestSequence(request: Pick<Request, "headers">): number | undefined {
  const value = request.headers()["x-quiz-observation-sequence"];
  if (value === undefined || !/^[1-9]\d{0,14}$/.test(value)) return undefined;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) ? parsed : undefined;
}

type ActorStage = "navigation" | "join_button" | "join_response" | "session_cookie" |
  "question_dom" | "answerable_dom" | "answer_click" | "accepted_dom" | "result_dom" | "unanswered_result";
type RoundCheck = "question" | "answerable" | "accepted" | "result";

async function waitForRound(page: Page, round: number, kind: RoundCheck, timeout: number): Promise<void> {
  const handle = await page.waitForFunction(({round, kind}) => {
    const progress = (window as unknown as {quizRunnerProgress?: {round: number; state: string}}).quizRunnerProgress;
    const rootClass = kind === "result" ? ".participant-result-stage" : ".participant-question";
    const root = Array.from(document.querySelectorAll(rootClass)).find(element =>
      element.querySelector(kind === "result" ? ".participant-result-round" : ".participant-round-badge")?.getAttribute("data-round-number") === String(round));
    const element = kind === "question" ? root : root?.querySelector(kind === "result"
      ? ".participant-score-result" : kind === "accepted" ? ".answer-state-accepted" : ".answer-button");
    if (element instanceof HTMLElement && !element.classList.contains("participant-result-pending") && element.getClientRects().length > 0 &&
        !["hidden", "collapse"].includes(getComputedStyle(element).visibility) &&
        (kind !== "answerable" || (element instanceof HTMLButtonElement && !element.disabled))) return "ready";
    const superseded = progress !== undefined && (kind === "result"
      ? progress.round > round + 1 || (progress.round === round + 1 && progress.state !== "PreparingAssets")
      : progress.round > round);
    return superseded ? "superseded" : false;
  }, {round, kind}, {timeout, polling: 100});
  try {
    if (await handle.jsonValue() === "superseded") throw new Error("round_superseded");
  } finally { await handle.dispose(); }
}

export async function followRoundProgress(signal: AbortSignal, answer: (round: number) => Promise<void>,
  observe: (round: number) => Promise<void>): Promise<void> {
  const observations: Promise<void>[] = [];
  try {
    for (let round = 1; round <= 6; round++) {
      signal.throwIfAborted();
      await answer(round);
      signal.throwIfAborted();
      // 受付・結果の待機は次問の回答経路から分離し、失敗は各観測側へ残す。
      observations.push(observe(round));
    }
  } finally { await Promise.all(observations); }
}

export async function runActor(actor: Actor, config: RunConfig, evidence: Evidence,
  signal = new AbortController().signal): Promise<void> {
  const {page, serial} = actor;
  let stage: ActorStage = "navigation";
  const fail = (round: number, failedStage: ActorStage, error: unknown): void => {
    if (signal.aborted) return;
    actor.failed = true;
    actor.failures.push({round, stage: failedStage});
    evidence.event({event: "actor_failed", serial, round, stage: failedStage,
      reason: fixedFailureReason(error, signal), phase: "measurement"});
  };
  const enter = (next: ActorStage): void => {
    signal.throwIfAborted();
    stage = next;
    evidence.event({event: "actor_stage", serial, stage, phase: "measurement"});
  };
  try {
    signal.throwIfAborted();
    evidence.event({event: "actor_started", serial, phase: "measurement", lifecycleVersion: 2});
    if (config.scenario === "clock-delay") {
      let delayed = 0;
      await page.route("**/api/v1/time", async route => {
        try {
          if (delayed++ < 3) await cancellableDelay(600, signal);
          if (!signal.aborted) await route.continue();
        } catch { if (!signal.aborted) fail(0, "navigation", new Error("clock_delay_route_failed")); }
      });
    }
    enter("navigation");
    await page.goto(config.baseUrl, {waitUntil: "domcontentloaded"});
    enter("join_button");
    await page.getByRole("button", {name: "参加する", exact: true}).click();
    enter("join_response");
    await page.locator(".participant-live-shell").waitFor();
    signal.throwIfAborted();
    actor.joined = true;
    evidence.event({event: "joined", serial});
    enter("session_cookie");
    // Cookie値は独立性の比較にだけ使い、証跡へ書き出さない。
    const cookies = await actor.context.cookies();
    if (!cookies.some(cookie => cookie.httpOnly && cookie.secure)) throw new Error("secure_session_missing");
    await recordParticipantIdentity(page, serial, config, evidence);
    const clicked = new Set<number>();
    await followRoundProgress(signal, async round => {
      evidence.event({event: "round_expected", serial, round});
      let answerStage: ActorStage = "question_dom";
      try {
        await waitForRound(page, round, "question", config.durationSeconds * 1000);
        signal.throwIfAborted();
        answerStage = "answerable_dom";
        await waitForRound(page, round, "answerable", 15000);
        signal.throwIfAborted();
        evidence.event({event: "answerable_dom", serial, round});
        await cancellableDelay(config.answerDelayMs, signal);
        evidence.event({event: "click_planned", serial, round});
        answerStage = "answer_click";
        const question = page.locator(".participant-question").filter({
          has: page.locator(`.participant-round-badge[data-round-number="${round}"]`),
        });
        await question.locator(".answer-button").nth((serial - 1) % 4).click({timeout: 8000});
        signal.throwIfAborted();
        clicked.add(round);
      } catch (error: unknown) { fail(round, answerStage, error); }
    }, async round => {
      const receipt = async (): Promise<void> => {
        if (!clicked.has(round)) return;
        try {
          await waitForRound(page, round, "accepted", 12000);
          signal.throwIfAborted();
          actor.rounds.push(round);
          evidence.event({event: "accepted_dom", serial, round});
          evidence.event({event: "round_ws_connected", serial, round, count: actor.connected ? 1 : 0});
        } catch (error: unknown) { fail(round, "accepted_dom", error); }
      };
      const result = async (): Promise<void> => {
        let resultStage: ActorStage = "result_dom";
        try {
          await waitForRound(page, round, "result", 45000);
          signal.throwIfAborted();
          resultStage = "unanswered_result";
          const root = page.locator(".participant-result-stage").filter({
            has: page.locator(`.participant-result-round[data-round-number="${round}"]`),
          });
          if (await root.locator(".outcome-unanswered").count() !== 0) throw new Error("unanswered_result");
          signal.throwIfAborted();
          actor.results.push(round);
          evidence.event({event: "result_dom", serial, round});
        } catch (error: unknown) { fail(round, resultStage, error); }
      };
      await Promise.all([receipt(), result()]);
    });
    actor.complete = !actor.failed && actor.rounds.length === 6 && actor.results.length === 6;
    if (actor.complete) evidence.event({event: "six_rounds_complete", serial});
  } catch (error: unknown) {
    fail(0, stage, error);
  } finally {
    actor.stopped = true;
    if (signal.aborted) evidence.event({event: "actor_cancelled", serial, reason: "runner_cancelled", phase: "shutdown"});
  }
}

export async function verifyIsolation(actors: Actor[], evidence: Evidence): Promise<boolean> {
  const values: string[] = [];
  for (const actor of actors) {
    const cookie = (await actor.context.cookies()).find(value => value.httpOnly && value.secure);
    if (cookie) values.push(cookie.value);
  }
  const isolated = values.length === actors.length && new Set(values).size === actors.length;
  evidence.event({event: isolated ? "cookie_isolation_verified" : "cookie_isolation_failed", count: values.length});
  return isolated;
}
