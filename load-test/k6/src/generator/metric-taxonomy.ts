export const GENERATOR_HTTP_ROUTES = [
  "answer_status",
  "app_index",
  "app_javascript",
  "app_stylesheet",
  "heartbeat",
  "join",
  "participant_session",
  "readiness",
  "server_time",
  "submit_answer",
] as const;
export type GeneratorHttpRoute = (typeof GENERATOR_HTTP_ROUTES)[number];

export const GENERATOR_HTTP_OUTCOMES = [
  "client_failure",
  "contract_failure",
  "server_failure",
  "success",
  "transport_failure",
] as const;
export type GeneratorHttpOutcome = (typeof GENERATOR_HTTP_OUTCOMES)[number];

export const GENERATOR_STATIC_CACHE_MODES = ["cold", "warm"] as const;
export type GeneratorStaticCacheMode =
  (typeof GENERATOR_STATIC_CACHE_MODES)[number];

export const GENERATOR_STATIC_ASSET_FAILURES = [
  "bundle_contract_invalid",
  "index_request_failed",
  "javascript_request_failed",
  "stylesheet_request_failed",
] as const;
export type GeneratorStaticAssetFailure =
  (typeof GENERATOR_STATIC_ASSET_FAILURES)[number];


export const GENERATOR_WEBSOCKET_ATTEMPTS = ["initial", "reconnect"] as const;
export type GeneratorWebSocketAttempt =
  (typeof GENERATOR_WEBSOCKET_ATTEMPTS)[number];

export const GENERATOR_WEBSOCKET_OUTCOMES = ["failed", "opened"] as const;
export type GeneratorWebSocketOutcome =
  (typeof GENERATOR_WEBSOCKET_OUTCOMES)[number];

export const GENERATOR_WEBSOCKET_MESSAGES = [
  "invalid",
  "polling_required",
  "state_invalidated",
] as const;
export type GeneratorWebSocketMessage =
  (typeof GENERATOR_WEBSOCKET_MESSAGES)[number];

export const GENERATOR_WEBSOCKET_CLOSE_CODES = [
  "1000",
  "1003",
  "1008",
  "1013",
  "4401",
  "other",
] as const;
export type GeneratorWebSocketCloseCode =
  (typeof GENERATOR_WEBSOCKET_CLOSE_CODES)[number];

export const GENERATOR_PROTOCOL_BOUNDARIES = [
  "payload",
  "realtime",
  "runtime",
  "session",
] as const;
export type GeneratorProtocolBoundary =
  (typeof GENERATOR_PROTOCOL_BOUNDARIES)[number];

export const GENERATOR_SUMMARY_TREND_STATS = [
  "count",
  "min",
  "avg",
  "med",
  "max",
  "p(50)",
  "p(95)",
  "p(99)",
] as const;

export const GENERATOR_METRIC_NAMES = {
  httpRequests: "generator_http_requests",
  httpDuration: "generator_http_duration_ms",
  staticAssetFailures: "generator_static_asset_failures",
  webSocketConnections: "generator_ws_connections",
  webSocketMessages: "generator_ws_messages",
  webSocketCloses: "generator_ws_closes",
  webSocketErrors: "generator_ws_errors",
  protocolRefusals: "generator_protocol_refusals",
} as const;

export type GeneratorMetricKind = "counter" | "trend";

export interface GeneratorMetricSeries {
  readonly kind: GeneratorMetricKind;
  readonly metricName: string;
  readonly selector: string;
}

const series = buildMetricSeries();

/** k6 summaryへ出す親metricと固定tag submetricの全体集合。 */
export const GENERATOR_METRIC_SERIES: readonly GeneratorMetricSeries[] = series;

/** thresholdは合否判定ではなく、0件を含む固定submetric生成にだけ使う。 */
export const GENERATOR_METRIC_THRESHOLDS: Readonly<
  Record<string, readonly string[]>
> = Object.freeze(
  Object.fromEntries(
    series.map((entry) => [
      entry.selector,
      Object.freeze([entry.kind === "counter" ? "count>=0" : "max>=0"]),
    ]),
  ),
);

export function metricSelector(
  metricName: string,
  tags: readonly (readonly [string, string])[],
): string {
  if (tags.length === 0) {
    return metricName;
  }
  return `${metricName}{${tags.map(([name, value]) => `${name}:${value}`).join(",")}}`;
}

function buildMetricSeries(): GeneratorMetricSeries[] {
  const entries: GeneratorMetricSeries[] = [];
  const addRoot = (metricName: string, kind: GeneratorMetricKind): void => {
    entries.push({ kind, metricName, selector: metricName });
  };
  const addTagged = (
    metricName: string,
    kind: GeneratorMetricKind,
    tags: readonly (readonly [string, string])[],
  ): void => {
    entries.push({ kind, metricName, selector: metricSelector(metricName, tags) });
  };

  addRoot(GENERATOR_METRIC_NAMES.httpRequests, "counter");
  addRoot(GENERATOR_METRIC_NAMES.httpDuration, "trend");
  for (const route of GENERATOR_HTTP_ROUTES) {
    for (const outcome of GENERATOR_HTTP_OUTCOMES) {
      const tags = [
        ["route", route],
        ["outcome", outcome],
      ] as const;
      addTagged(GENERATOR_METRIC_NAMES.httpRequests, "counter", tags);
      addTagged(GENERATOR_METRIC_NAMES.httpDuration, "trend", tags);
    }
  }

  addRoot(GENERATOR_METRIC_NAMES.staticAssetFailures, "counter");
  for (const cacheMode of GENERATOR_STATIC_CACHE_MODES) {
    for (const reason of GENERATOR_STATIC_ASSET_FAILURES) {
      addTagged(GENERATOR_METRIC_NAMES.staticAssetFailures, "counter", [
        ["cache_mode", cacheMode],
        ["reason", reason],
      ]);
    }
  }


  addRoot(GENERATOR_METRIC_NAMES.webSocketConnections, "counter");
  for (const attempt of GENERATOR_WEBSOCKET_ATTEMPTS) {
    for (const outcome of GENERATOR_WEBSOCKET_OUTCOMES) {
      addTagged(GENERATOR_METRIC_NAMES.webSocketConnections, "counter", [
        ["attempt", attempt],
        ["outcome", outcome],
      ]);
    }
  }

  addRoot(GENERATOR_METRIC_NAMES.webSocketMessages, "counter");
  for (const message of GENERATOR_WEBSOCKET_MESSAGES) {
    addTagged(GENERATOR_METRIC_NAMES.webSocketMessages, "counter", [
      ["message", message],
    ]);
  }

  addRoot(GENERATOR_METRIC_NAMES.webSocketCloses, "counter");
  for (const code of GENERATOR_WEBSOCKET_CLOSE_CODES) {
    addTagged(GENERATOR_METRIC_NAMES.webSocketCloses, "counter", [["code", code]]);
  }

  addRoot(GENERATOR_METRIC_NAMES.webSocketErrors, "counter");

  addRoot(GENERATOR_METRIC_NAMES.protocolRefusals, "counter");
  for (const boundary of GENERATOR_PROTOCOL_BOUNDARIES) {
    addTagged(GENERATOR_METRIC_NAMES.protocolRefusals, "counter", [
      ["boundary", boundary],
    ]);
  }
  return entries;
}
