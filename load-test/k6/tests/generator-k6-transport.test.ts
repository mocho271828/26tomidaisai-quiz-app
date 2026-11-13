import type { CookieJar } from "k6/http";
import { beforeEach, describe, expect, it } from "vitest";

import { K6GeneratorMetrics } from "../src/generator/k6-metrics.js";
import { K6ParticipantTransport } from "../src/generator/k6-transport.js";
import {
  enqueueHttpResponse,
  enqueueHttpTransportFailure,
  expectedStatusesCallback,
  readHttpCalls,
  resetHttpStub,
  type StubHttpCall,
} from "./stubs/k6-http.js";
import { readMetricSamples, resetMetricStub } from "./stubs/k6-metrics.js";

const PUBLIC_ORIGIN = "https://quiz.aws.internal";
const API_ORIGIN = `${PUBLIC_ORIGIN}/api/v1`;
const ROUND_ID = "11111111-1111-4111-8111-111111111111";
const OPTION_ID = "22222222-2222-4222-8222-222222222222";
const CLIENT_ANSWER_ID = "33333333-3333-4333-8333-333333333333";
const ANSWER_ID = "44444444-4444-4444-8444-444444444444";
const CLIENT_JOIN_TOKEN = "j".repeat(43);
const PUBLIC_LABEL = "AAA";
const ROUND_QUESTION_IDS = [
  "60000000-0000-4000-8000-000000000001",
  "60000000-0000-4000-8000-000000000002",
  "60000000-0000-4000-8000-000000000003",
  "60000000-0000-4000-8000-000000000004",
  "60000000-0000-4000-8000-000000000005",
  "60000000-0000-4000-8000-000000000006",
  "60000000-0000-4000-8000-000000000007",
  "60000000-0000-4000-8000-000000000008",
  "60000000-0000-4000-8000-000000000009",
  "60000000-0000-4000-8000-000000000010",
] as const;
const participantState = {
  projected_at: "2026-09-04T00:00:00Z",
  event: {
    event_id: "55555555-5555-4555-8555-555555555555",
    event_title: "Load Test Event",
    event_state: "LobbyOpen",
    event_version: 1,
    start_delay_seconds: 3,
  },
  participant: {
    public_label: PUBLIC_LABEL,
    is_ranking_eligible: true,
  },
  current_round: null,
  latest_result: null,
} as const;

const readinessAcknowledgement = {
  round_id: ROUND_ID,
  round_version: 3,
  round_state: "ReadyToSchedule",
  state_version: 4,
} as const;

const answerReceipt = {
  answer_id: ANSWER_ID,
  round_id: ROUND_ID,
  option_id: OPTION_ID,
  client_answer_id: CLIENT_ANSWER_ID,
  received_at: "2026-09-04T00:01:00Z",
  is_replay: false,
} as const;

interface ExpectedCall {
  readonly route: string;
  readonly method: "GET" | "POST";
  readonly url: string;
  readonly body: string | null;
  readonly name: string;
  readonly timeout: number;
  readonly accept: string;
  readonly hasJsonBody: boolean;
  readonly responseType: "binary" | "text";
  readonly cacheMode?: "cold" | "warm";
}

beforeEach(() => {
  resetHttpStub();
  resetMetricStub();
});

describe("K6ParticipantTransport request contract", () => {
  it("participant APIとapp assetsを同一TLS origin・VU CookieJar・固定optionで送信する", () => {
    const { jar, transport } = createTransport();
    enqueueJson(participantState);
    enqueueJson(participantState);
    enqueueJson(participantState);
    enqueueJson(readinessAcknowledgement);
    enqueueJson(answerReceipt);
    enqueueJson({ status: "NotStored", client_answer_id: CLIENT_ANSWER_ID });
    enqueueJson({ server_time: "2026-09-04T00:00:01Z" });
    enqueueHttpResponse({
      status: 200,
      body: '<script type="module" src="/assets/app.js"></script><link rel="stylesheet" href="/assets/app.css">',
    });
    enqueueHttpResponse({ status: 200, body: 'const image = "/assets/banner.webp";' });
    enqueueHttpResponse({
      status: 200,
      body: 'body { background-image: url("/assets/banner.webp"); }',
    });

    expect(transport.checkSession().kind).toBe("success");
    expect(transport.join(CLIENT_JOIN_TOKEN, PUBLIC_LABEL).kind).toBe("success");
    expect(transport.heartbeat().kind).toBe("success");
    expect(
      transport.recordReadiness(ROUND_ID, {
        round_version: 3,
        is_ready: true,
        has_asset_failure: false,
      }).kind,
    ).toBe("success");
    expect(
      transport.submitAnswer(ROUND_ID, {
        round_version: 3,
        option_id: OPTION_ID,
        client_answer_id: CLIENT_ANSWER_ID,
      }).kind,
    ).toBe("success");
    expect(transport.getAnswerStatus(CLIENT_ANSWER_ID).kind).toBe("success");
    expect(transport.getServerTime().kind).toBe("success");
    expect(
      transport.loadRoundAssets({
        roundNumber: 1,
        questionId: ROUND_QUESTION_IDS[0],
      }),
    ).toBe(true);

    const expectedCalls: readonly ExpectedCall[] = [
      apiCall("participant_session", "GET", "/participant/session", null, "GET /api/v1/participant/session", 5_000),
      apiCall(
        "join",
        "POST",
        "/participant/join",
        JSON.stringify({
          client_join_token: CLIENT_JOIN_TOKEN,
          public_label: PUBLIC_LABEL,
        }),
        "POST /api/v1/participant/join",
        5_000,
      ),
      apiCall(
        "heartbeat",
        "POST",
        "/participant/session/heartbeat",
        null,
        "POST /api/v1/participant/session/heartbeat",
        5_000,
      ),
      apiCall(
        "readiness",
        "POST",
        `/participant/rounds/${ROUND_ID}/readiness`,
        JSON.stringify({ round_version: 3, is_ready: true, has_asset_failure: false }),
        "POST /api/v1/participant/rounds/{round_id}/readiness",
        2_500,
      ),
      apiCall(
        "submit_answer",
        "POST",
        `/participant/rounds/${ROUND_ID}/answers`,
        JSON.stringify({
          round_version: 3,
          option_id: OPTION_ID,
          client_answer_id: CLIENT_ANSWER_ID,
        }),
        "POST /api/v1/participant/rounds/{round_id}/answers",
        2_500,
      ),
      apiCall(
        "answer_status",
        "GET",
        `/participant/answers/${CLIENT_ANSWER_ID}/status`,
        null,
        "GET /api/v1/participant/answers/{client_answer_id}/status",
        5_000,
      ),
      apiCall("server_time", "GET", "/time", null, "GET /api/v1/time", 1_000),
      assetCall("app_index", "/", "GET /", "text/html", "text"),
      assetCall(
        "app_javascript",
        "/assets/app.js",
        "GET /assets/{javascript}",
        "*/*",
        "text",
      ),
      assetCall(
        "app_stylesheet",
        "/assets/app.css",
        "GET /assets/{stylesheet}",
        "*/*",
        "text",
      ),
    ];

    expect(readHttpCalls()).toHaveLength(expectedCalls.length);
    expectedCalls.forEach((expected, index) => {
      assertCallContract(readHttpCalls()[index], expected, jar);
    });
    expect(
      readMetricSamples("generator_http_requests").map((sample) => sample.tags),
    ).toEqual(
      expectedCalls.map(({ route }) => ({ outcome: "success", route })),
    );
  });
});

describe("K6ParticipantTransport failure classification", () => {
  it.each([
    { status: 400, kind: "known-failure", failure: "client", outcome: "client_failure" },
    { status: 500, kind: "known-failure", failure: "server", outcome: "server_failure" },
    { status: 201, kind: "unknown", failure: undefined, outcome: "transport_failure" },
  ] as const)("HTTP $status を $outcome に分類する", ({ status, kind, failure, outcome }) => {
    enqueueHttpResponse({ status, body: JSON.stringify(participantState) });
    const result = createTransport().transport.checkSession();

    expect(result.kind).toBe(kind);
    if (failure !== undefined) {
      expect(result).toMatchObject({ failure });
    }
    expectHttpOutcome(outcome);
  });

  it.each(["{", "{}"])("200の不正JSON契約をcontract_failureに分類する", (body) => {
    enqueueHttpResponse({ status: 200, body });
    expect(createTransport().transport.checkSession().kind).toBe("unknown");
    expectHttpOutcome("contract_failure");
  });

  it("HTTP実行例外を本文や識別子なしのtransport_failureに分類する", () => {
    enqueueHttpTransportFailure();
    expect(createTransport().transport.checkSession()).toEqual({
      kind: "unknown",
      startedAtLocalMs: 1_000,
      endedAtLocalMs: 1_010,
    });
    expectHttpOutcome("transport_failure");
  });
});

describe("K6ParticipantTransport initial application assets", () => {
  it("HTML・JS・CSSはsession中1度だけ取得し画像は取得しない", () => {
    const { transport } = createTransport();
    enqueueApplicationAssets();
    expect(transport.loadRoundAssets({ roundNumber: 1, questionId: ROUND_QUESTION_IDS[0] })).toBe(true);
    expect(transport.loadRoundAssets({ roundNumber: 2, questionId: ROUND_QUESTION_IDS[1] })).toBe(true);
    expect(readHttpCalls().map((call) => call.url)).toEqual([
      `${PUBLIC_ORIGIN}/`, `${PUBLIC_ORIGIN}/assets/app.js`, `${PUBLIC_ORIGIN}/assets/app.css`,
    ]);
  });
});

function createTransport(
): {
  readonly jar: CookieJar;
  readonly transport: K6ParticipantTransport;
} {
  const jar = { scope: "isolated-test-vu" } as unknown as CookieJar;
  let now = 1_000;
  const clock = (): number => {
    const current = now;
    now += 10;
    return current;
  };
  return {
    jar,
    transport: new K6ParticipantTransport({
      publicOrigin: PUBLIC_ORIGIN,
      roundQuestionIds: ROUND_QUESTION_IDS,
      jar,
      metrics: new K6GeneratorMetrics(),
      clock,
    }),
  };
}

function enqueueApplicationAssets(): void {
  enqueueHttpResponse({
    status: 200,
    body: '<script type="module" src="/assets/app.js"></script><link rel="stylesheet" href="/assets/app.css">',
  });
  enqueueHttpResponse({ status: 200, body: 'const image = "/assets/banner.webp";' });
  enqueueHttpResponse({
    status: 200,
    body: 'body { background-image: url("/assets/banner.webp"); }',
  });
}

function enqueueJson(value: unknown): void {
  enqueueHttpResponse({ status: 200, body: JSON.stringify(value) ?? "null" });
}

function apiCall(
  route: string,
  method: "GET" | "POST",
  path: string,
  body: string | null,
  name: string,
  timeout: number,
): ExpectedCall {
  return {
    route,
    method,
    url: `${API_ORIGIN}${path}`,
    body,
    name,
    timeout,
    accept: "application/json",
    hasJsonBody: body !== null,
    responseType: "text",
  };
}

function assetCall(
  route: string,
  path: string,
  name: string,
  accept: string,
  responseType: "binary" | "text",
): ExpectedCall {
  return {
    route,
    method: "GET",
    url: `${PUBLIC_ORIGIN}${path}`,
    body: null,
    name,
    timeout: 5_000,
    cacheMode: "warm",
    accept,
    hasJsonBody: false,
    responseType,
  };
}

function assertCallContract(
  call: StubHttpCall | undefined,
  expected: ExpectedCall,
  jar: CookieJar,
): void {
  expect(call).toBeDefined();
  expect(call).toMatchObject({
    method: expected.method,
    url: expected.url,
    body: expected.body,
  });
  expect(call?.params).toEqual({
    headers: {
      Accept: expected.accept,
      Origin: PUBLIC_ORIGIN,
      ...(expected.hasJsonBody ? { "Content-Type": "application/json" } : {}),
    },
    jar,
    redirects: 0,
    responseCallback: expectedStatusesCallback,
    responseType: expected.responseType,
    tags: {
      ...(expected.cacheMode === undefined ? {} : { cache_mode: expected.cacheMode }),
      name: expected.name,
    },
    timeout: expected.timeout,
  });
}

function expectHttpOutcome(outcome: string): void {
  expect(readMetricSamples("generator_http_requests")).toEqual([
    {
      metricKind: "counter",
      metricName: "generator_http_requests",
      value: 1,
      tags: { outcome, route: "participant_session" },
    },
  ]);
}
