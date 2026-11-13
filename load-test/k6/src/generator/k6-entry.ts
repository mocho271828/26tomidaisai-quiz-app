import { SESSION_CONNECTION_PREFIX } from "./ramp-readiness.js";
import { CANARY_PREFIX } from "./canary.js";
import { sleep } from "k6";
import crypto from "k6/crypto";
import encoding from "k6/encoding";
import exec from "k6/execution";
import http, { type CookieJar } from "k6/http";
import type { Options } from "k6/options";
import ws, { type Response as WebSocketResponse, type Socket } from "k6/ws";

import {
  GeneratorPayloadRefusalError,
  decodeApiJson,
  realtimeServerMessageSchema,
} from "./api-contracts.js";
import {
  K6GeneratorMetrics,
  type GeneratorWebSocketAttempt,
} from "./k6-metrics.js";
import {
  GENERATOR_METRIC_THRESHOLDS,
  GENERATOR_SUMMARY_TREND_STATS,
} from "./metric-taxonomy.js";
import { renderGeneratorMetricsSummary } from "./metrics-summary.js";
import {
  K6ParticipantTransport,
  K6TransportConfigurationError,
} from "./k6-transport.js";
import {
  SCENARIO_START_TOLERANCE_MILLISECONDS,
  advanceNondecreasingMilliseconds,
  isScenarioStartWithinTolerance,
} from "./k6-runtime-policy.js";
import { planGeneratorVirtualUser } from "./planner-core.js";
import {
  loadK6RuntimeManifest,
} from "./runtime-manifest.js";
import { GeneratorVirtualSession } from "./session.js";
import { GeneratorProtocolRefusal } from "./session-contracts.js";
import { uuid5Ascii } from "./uuid5-core.js";

const GENERATOR_EVENT_PREFIX = "@@LOAD_TEST_EVENT@@";
const PARTICIPANT_REALTIME_NAME =
  "GET /api/v1/participant/realtime (WebSocket)";
const PARTICIPANT_REALTIME_PATH = "/api/v1/participant/realtime";
const TICK_INTERVAL_SECONDS = 0.1;
const TICK_INTERVAL_MILLISECONDS = TICK_INTERVAL_SECONDS * 1_000;
const binding = loadK6RuntimeManifest(
  __ENV["LOAD_TEST_RUNTIME_MANIFEST_JSON"],
  __ENV,
  (value) => crypto.sha256(value, "hex"),
);
const metrics = new K6GeneratorMetrics();

export const options: Options = {
  handleSummaryTimeout: "5s",
  insecureSkipTLSVerify: false,
  maxRedirects: 0,
  scenarios: {
    participant_sessions: {
      executor: "ramping-vus",
      exec: "runParticipantSession",
      gracefulRampDown: "0s",
      gracefulStop: "0s",
      startVUs: 0,
      stages: [
        { duration: "150s", target: 100 },
        { duration: "150s", target: 300 },
        { duration: "150s", target: 500 },
        { duration: "150s", target: 600 },
        { duration: "3900s", target: 600 },
        { duration: "300s", target: 0 },
      ],
    },
  },
  // URLや生errorを時系列labelにせず、固定name/outcome tagだけを残す。
  summaryTrendStats: [...GENERATOR_SUMMARY_TREND_STATS],
  systemTags: ["expected_response", "method", "name", "scenario", "status"],
  // 合否基準ではなく、0件を含む固定tag submetricをsummaryへ出すために使う。
  thresholds: Object.fromEntries(
    Object.entries(GENERATOR_METRIC_THRESHOLDS).map(([selector, thresholds]) => [
      selector,
      [...thresholds],
    ]),
  ),
  throw: false,
};

let hasRunParticipantSession = false;

/** ramping-vusで割り当てられた1 VUを、終了まで同じCookie/WS sessionとして維持する。 */
export function runParticipantSession(): void {
  if (hasRunParticipantSession) {
    abortRun("generator_iteration_repeated", "runtime");
    return;
  }
  hasRunParticipantSession = true;

  try {
    requireScheduledScenarioStart();
    const vuId = exec.vu.idInTest;
    const plan = planGeneratorVirtualUser(
      binding.manifest,
      vuId,
      (runId, questionId, plannedVuId) =>
        uuid5Ascii(
          runId,
          `${questionId}:${plannedVuId}`,
          (value) => crypto.sha1(value, "binary"),
        ),
    );
    const jar = http.cookieJar();
    const clock = createNondecreasingClock();
    const transport = new K6ParticipantTransport({
      publicOrigin: binding.manifest.public_origin,
      roundQuestionIds: binding.manifest.questions.map((question) => question.id),
      jar,
      metrics,
      clock,
    });
    const session = new GeneratorVirtualSession({
      binding,
      plan,
      clientJoinToken: createClientJoinToken(),
      transport,
      eventSink: {
        emitSessionConnection(event): void {
          console.log(`${SESSION_CONNECTION_PREFIX}${JSON.stringify(event)}`);
        },
        emitCanary(event): void {
          console.log(`${CANARY_PREFIX}${JSON.stringify(event)}`);
        },
        emit(event): void {
          console.log(`${GENERATOR_EVENT_PREFIX}${JSON.stringify(event)}`);
        },
      },
    });
    let webSocketAttempt: GeneratorWebSocketAttempt = "initial";

    session.start(clock());
    for (;;) {
      const localNow = clock();
      if (session.shouldStop(localNow)) {
        // ramping-vusの次iterationを開始させず、executorのVU interruptを待つ。
        sleep(TICK_INTERVAL_SECONDS);
        continue;
      }
      session.tick(localNow);
      if (session.shouldConnectSocket(localNow)) {
        connectParticipantWebSocket(session, jar, clock, webSocketAttempt);
        webSocketAttempt = "reconnect";
        continue;
      }
      sleep(TICK_INTERVAL_SECONDS);
    }
  } catch (error: unknown) {
    const refusal = classifyRuntimeFailure(error);
    abortRun(refusal.code, refusal.boundary);
  }
}

export default runParticipantSession;

/** k6の全metric objectを公開せず、固定cardinalityの集計だけをstdoutへ返す。 */
export function handleSummary(data: unknown): { readonly stdout: string } {
  return {
    stdout: renderGeneratorMetricsSummary(
      data,
      {
        runId: binding.manifest.run_id,
        eventId: binding.manifest.event.id,
        sourceCommit: binding.manifest.artifacts.source_commit,
        schemaRevision: binding.manifest.schema_revision,
        manifestSha256: binding.manifestSha256,
        scenarioSha256: binding.scenarioSha256,
        runtimeManifestSha256: binding.runtimeManifestSha256,
      },
      new Date(),
    ),
  };
}

function connectParticipantWebSocket(
  session: GeneratorVirtualSession,
  jar: CookieJar,
  clock: () => number,
  attempt: GeneratorWebSocketAttempt,
): void {
  session.markSocketConnecting();
  let didEnterSocketCallback = false;
  let didOpen = false;
  let didClose = false;
  let response: WebSocketResponse;
  try {
    response = ws.connect(
      websocketUrl(binding.manifest.public_origin),
      {
        headers: { Origin: binding.manifest.public_origin },
        jar,
        tags: { name: PARTICIPANT_REALTIME_NAME },
      },
      (socket) => {
        didEnterSocketCallback = true;
        socket.on("open", () => {
          didOpen = true;
          metrics.recordWebSocketConnect(attempt, "opened");
          const localNow = clock();
          session.onSocketOpen(localNow);
          if (session.shouldStop(localNow)) {
            socket.close(1_000);
          }
        });
        socket.on("message", (rawMessage) => {
          processRealtimeMessage(session, socket, rawMessage, clock);
        });
        socket.on("error", () => {
          metrics.recordWebSocketError();
          const localNow = clock();
          if (!session.shouldStop(localNow)) {
            session.onSocketProtocolError(localNow);
            socket.close(1_008);
          }
        });
        socket.on("close", (code) => {
          didClose = true;
          metrics.recordWebSocketClose(code);
          const localNow = clock();
          if (!session.shouldStop(localNow)) {
            session.onSocketClosed(localNow);
          }
        });
        installSocketTick(session, socket, clock);
      },
    );
  } catch (error: unknown) {
    // callback内のprotocol/runtime失敗を接続失敗へ誤分類せず、run全体を停止させる。
    if (didEnterSocketCallback) {
      throw error;
    }
    metrics.recordWebSocketConnect(attempt, "failed");
    markSocketClosedBeforePlannedStop(session, clock);
    return;
  }

  if (!didOpen || response.status !== 101) {
    metrics.recordWebSocketConnect(attempt, "failed");
  }
  if (!didClose) {
    markSocketClosedBeforePlannedStop(session, clock);
  }
}

function markSocketClosedBeforePlannedStop(
  session: GeneratorVirtualSession,
  clock: () => number,
): void {
  const localNow = clock();
  if (!session.shouldStop(localNow)) {
    session.onSocketClosed(localNow);
  }
}

function installSocketTick(
  session: GeneratorVirtualSession,
  socket: Socket,
  clock: () => number,
): void {
  socket.setInterval(() => {
    const localNow = clock();
    if (session.shouldStop(localNow)) {
      socket.close(1_000);
      return;
    }
    session.tick(localNow);
  }, TICK_INTERVAL_MILLISECONDS);
}

function processRealtimeMessage(
  session: GeneratorVirtualSession,
  socket: Socket,
  rawMessage: string,
  clock: () => number,
): void {
  const localNow = clock();
  if (session.shouldStop(localNow)) {
    socket.close(1_000);
    return;
  }
  let message: ReturnType<typeof realtimeServerMessageSchema.parse>;
  try {
    message = decodeApiJson(realtimeServerMessageSchema, rawMessage);
  } catch (error: unknown) {
    metrics.recordWebSocketMessage("invalid");
    metrics.recordProtocolRefusal(
      error instanceof GeneratorPayloadRefusalError ? "payload" : "realtime",
    );
    session.onSocketProtocolError(localNow);
    socket.close(1_008);
    return;
  }
  metrics.recordWebSocketMessage(
    message.type === "state.invalidated"
      ? "state_invalidated"
      : "polling_required",
  );
  if (session.onRealtimeMessage(message, localNow)) {
    socket.close(1_008);
  }
}

function createClientJoinToken(): string {
  const random = crypto.randomBytes(32);
  const token = encoding.b64encode(random, "rawurl");
  new Uint8Array(random).fill(0);
  if (token.length !== binding.manifest.scenario.protocol.join_token_characters) {
    throw new K6EntryRefusal("generator_join_token_invalid");
  }
  return token;
}

function requireScheduledScenarioStart(): void {
  const scheduledStart = Date.parse(binding.manifest.scheduled_start_at);
  const scenarioStart = exec.scenario.startTime;
  if (!isScenarioStartWithinTolerance(
    scheduledStart,
    scenarioStart,
    SCENARIO_START_TOLERANCE_MILLISECONDS,
  )) {
    throw new K6EntryRefusal("generator_scenario_start_mismatch");
  }
}

function createNondecreasingClock(): () => number {
  let previousMilliseconds = 0;
  return () => {
    const nextMilliseconds = advanceNondecreasingMilliseconds(
      previousMilliseconds,
      Date.now(),
    );
    if (nextMilliseconds === null) {
      throw new K6EntryRefusal("generator_clock_invalid");
    }
    previousMilliseconds = nextMilliseconds;
    return nextMilliseconds;
  };
}

function websocketUrl(publicOrigin: string): string {
  return `wss://${publicOrigin.slice("https://".length)}${PARTICIPANT_REALTIME_PATH}`;
}

type ProtocolBoundary = Parameters<
  K6GeneratorMetrics["recordProtocolRefusal"]
>[0];

function abortRun(code: string, boundary: ProtocolBoundary): void {
  metrics.recordProtocolRefusal(boundary);
  exec.test.abort(code);
}

function classifyRuntimeFailure(error: unknown): {
  readonly code: string;
  readonly boundary: ProtocolBoundary;
} {
  if (error instanceof GeneratorProtocolRefusal) {
    return { code: error.code, boundary: "session" };
  }
  if (error instanceof K6TransportConfigurationError) {
    return { code: "generator_transport_configuration_invalid", boundary: "runtime" };
  }
  if (error instanceof K6EntryRefusal) {
    return { code: error.code, boundary: "runtime" };
  }
  return { code: "generator_runtime_failed", boundary: "runtime" };
}

type K6EntryRefusalCode =
  | "generator_clock_invalid"
  | "generator_join_token_invalid"
  | "generator_scenario_start_mismatch";

class K6EntryRefusal extends Error {
  readonly code: K6EntryRefusalCode;

  constructor(code: K6EntryRefusalCode) {
    super(code);
    this.name = "K6EntryRefusal";
    this.code = code;
  }
}
