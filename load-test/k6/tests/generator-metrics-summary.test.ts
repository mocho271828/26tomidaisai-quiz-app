import { describe, expect, it } from "vitest";

import { canonicalJson } from "../src/canonical-json-core.js";
import {
  GENERATOR_HTTP_OUTCOMES,
  GENERATOR_HTTP_ROUTES,
  GENERATOR_METRIC_NAMES,
  GENERATOR_METRIC_SERIES,
  GENERATOR_SUMMARY_TREND_STATS,
  metricSelector,
} from "../src/generator/metric-taxonomy.js";
import {
  GENERATOR_METRICS_PREFIX,
  MAX_GENERATOR_METRICS_ARTIFACT_BYTES,
  GeneratorMetricsSummaryRefusal,
  canonicalSummaryDatetime,
  parseGeneratorMetricsSummary,
  projectGeneratorMetricsSummary,
  renderGeneratorMetricsSummary,
  serializeGeneratorMetricsSummary,
  type GeneratorMetricsBinding,
} from "../src/generator/metrics-summary.js";

interface RawK6Summary {
  readonly metrics: Record<string, unknown>;
  readonly options: { summaryTrendStats: string[] };
  readonly state: { testRunDurationMs: number };
}

interface TrendValues {
  readonly count: number;
  readonly min: number;
  readonly avg: number;
  readonly med: number;
  readonly max: number;
  readonly "p(50)": number;
  readonly "p(95)": number;
  readonly "p(99)": number;
}

const BINDING: GeneratorMetricsBinding = {
  runId: "11111111-1111-4111-8111-111111111111",
  eventId: "22222222-2222-4222-8222-222222222222",
  sourceCommit: "a".repeat(40),
  schemaRevision: "load-test-v1",
  manifestSha256: "b".repeat(64),
  scenarioSha256: "c".repeat(64),
  runtimeManifestSha256: "d".repeat(64),
};
const GENERATED_AT = new Date("2026-09-04T01:02:03.123Z");

function counterMetric(count = 0, rate = 0): unknown {
  return {
    type: "counter",
    contains: "default",
    values: { count, rate },
  };
}

function trendMetric(overrides: Partial<TrendValues> = {}): unknown {
  return {
    type: "trend",
    contains: "time",
    values: {
      count: 0,
      min: 0,
      avg: 0,
      med: 0,
      max: 0,
      "p(50)": 0,
      "p(95)": 0,
      "p(99)": 0,
      ...overrides,
    },
  };
}

function rawSummary(): RawK6Summary {
  return {
    metrics: Object.fromEntries(
      GENERATOR_METRIC_SERIES.map((series) => [
        series.selector,
        series.kind === "counter" ? counterMetric() : trendMetric(),
      ]),
    ),
    options: { summaryTrendStats: [...GENERATOR_SUMMARY_TREND_STATS] },
    state: { testRunDurationMs: 4_800_000 },
  };
}

function httpSelector(metricName: string): string {
  return taggedHttpSelector(metricName, "answer_status", "success");
}

function taggedHttpSelector(
  metricName: string,
  route: (typeof GENERATOR_HTTP_ROUTES)[number],
  outcome: (typeof GENERATOR_HTTP_OUTCOMES)[number],
): string {
  return metricSelector(metricName, [
    ["route", route],
    ["outcome", outcome],
  ]);
}

function populateSuccessfulSamples(summary: RawK6Summary): void {
  summary.metrics[httpSelector(GENERATOR_METRIC_NAMES.httpRequests)] =
    counterMetric(2, 1.234);
  summary.metrics[httpSelector(GENERATOR_METRIC_NAMES.httpDuration)] =
    trendMetric({
      count: 2,
      min: 1.1,
      avg: 2.2,
      med: 1.75,
      max: 6,
      "p(50)": 2,
      "p(95)": 4.5,
      "p(99)": 5.5,
    });
  summary.metrics[
    taggedHttpSelector(GENERATOR_METRIC_NAMES.httpRequests, "app_stylesheet", "success")
  ] = counterMetric(2, 1.234);
  summary.metrics[
    taggedHttpSelector(GENERATOR_METRIC_NAMES.httpDuration, "app_stylesheet", "success")
  ] = trendMetric({ count: 2 });
  summary.metrics[GENERATOR_METRIC_NAMES.httpRequests] = counterMetric(4, 1.234);
  summary.metrics[GENERATOR_METRIC_NAMES.httpDuration] = trendMetric({ count: 4 });

  const staticFailure = metricSelector(
    GENERATOR_METRIC_NAMES.staticAssetFailures,
    [
      ["cache_mode", "cold"],
      ["reason", "stylesheet_request_failed"],
    ],
  );
  summary.metrics[staticFailure] = counterMetric(1);
  summary.metrics[GENERATOR_METRIC_NAMES.staticAssetFailures] = counterMetric(1);

  const connection = metricSelector(
    GENERATOR_METRIC_NAMES.webSocketConnections,
    [
      ["attempt", "initial"],
      ["outcome", "opened"],
    ],
  );
  summary.metrics[connection] = counterMetric(2);
  summary.metrics[GENERATOR_METRIC_NAMES.webSocketConnections] = counterMetric(2);

  const message = metricSelector(GENERATOR_METRIC_NAMES.webSocketMessages, [
    ["message", "state_invalidated"],
  ]);
  summary.metrics[message] = counterMetric(3);
  summary.metrics[GENERATOR_METRIC_NAMES.webSocketMessages] = counterMetric(3);

  const close = metricSelector(GENERATOR_METRIC_NAMES.webSocketCloses, [
    ["code", "1000"],
  ]);
  summary.metrics[close] = counterMetric(2);
  summary.metrics[GENERATOR_METRIC_NAMES.webSocketCloses] = counterMetric(2);
  summary.metrics[GENERATOR_METRIC_NAMES.webSocketErrors] = counterMetric(1);

  const refusal = metricSelector(GENERATOR_METRIC_NAMES.protocolRefusals, [
    ["boundary", "runtime"],
  ]);
  summary.metrics[refusal] = counterMetric(1);
  summary.metrics[GENERATOR_METRIC_NAMES.protocolRefusals] = counterMetric(1);
}

function expectRefusal(
  operation: () => unknown,
  expectedCode: GeneratorMetricsSummaryRefusal["code"],
): void {
  let thrown: unknown;
  try {
    operation();
  } catch (error: unknown) {
    thrown = error;
  }
  expect(thrown).toBeInstanceOf(GeneratorMetricsSummaryRefusal);
  if (!(thrown instanceof GeneratorMetricsSummaryRefusal)) {
    throw new Error("expected GeneratorMetricsSummaryRefusal");
  }
  expect(thrown.code).toBe(expectedCode);
}

describe("generator metrics projection", () => {
  it("固定taxonomyを低cardinality artifactへ正規化する", () => {
    const raw = rawSummary();
    populateSuccessfulSamples(raw);
    raw.metrics["http_reqs"] = counterMetric(2, 1.234);

    const projected = projectGeneratorMetricsSummary(raw, BINDING, GENERATED_AT);

    expect(GENERATOR_METRIC_SERIES).toHaveLength(133);
    expect(Object.keys(projected.http)).toEqual(GENERATOR_HTTP_ROUTES);
    expect(Object.keys(projected.http.answer_status)).toEqual(
      GENERATOR_HTTP_OUTCOMES,
    );
    expect(projected.http.answer_status.success).toEqual({
      request_count: 2,
      request_rate_milli_per_second: 1_234,
      duration_microseconds: {
        minimum_microseconds: 1_100,
        mean_microseconds: 2_200,
        p50_microseconds: 2_000,
        p95_microseconds: 4_500,
        p99_microseconds: 5_500,
        maximum_microseconds: 6_000,
      },
    });
    expect(projected.static_asset_failures.cold.stylesheet_request_failed).toBe(1);
    expect(projected.format).toBe("load-test-generator-metrics-summary-v3");
    expect(projected.websocket.connections.initial.opened).toBe(2);
    expect(projected.websocket.messages.state_invalidated).toBe(3);
    expect(projected.websocket.closes["1000"]).toBe(2);
    expect(projected.websocket.error_count).toBe(1);
    expect(projected.protocol_refusals.runtime).toBe(1);
    expect(projected.generated_at).toBe("2026-09-04T01:02:03.123000Z");
  });

  it("0件のHTTP cellはduration=nullかつrate=0で保持する", () => {
    const projected = projectGeneratorMetricsSummary(
      rawSummary(),
      BINDING,
      GENERATED_AT,
    );

    expect(projected.http.submit_answer.transport_failure).toEqual({
      request_count: 0,
      request_rate_milli_per_second: 0,
      duration_microseconds: null,
    });
  });

  it("0件trendに非0統計があれば拒否する", () => {
    const raw = rawSummary();
    raw.metrics[httpSelector(GENERATOR_METRIC_NAMES.httpDuration)] = trendMetric({
      "p(99)": 1,
    });

    expectRefusal(
      () => projectGeneratorMetricsSummary(raw, BINDING, GENERATED_AT),
      "metrics_summary_numeric_invalid",
    );
  });

  it("親counterとsubmetric合計の不一致を拒否する", () => {
    const raw = rawSummary();
    raw.metrics[GENERATOR_METRIC_NAMES.staticAssetFailures] = counterMetric(1);

    expectRefusal(
      () => projectGeneratorMetricsSummary(raw, BINDING, GENERATED_AT),
      "metrics_summary_cardinality_invalid",
    );
  });

  it("同一HTTP cellのcounter件数とtrend件数の不一致を拒否する", () => {
    const raw = rawSummary();
    raw.metrics[httpSelector(GENERATOR_METRIC_NAMES.httpRequests)] =
      counterMetric(1);

    expectRefusal(
      () => projectGeneratorMetricsSummary(raw, BINDING, GENERATED_AT),
      "metrics_summary_cardinality_invalid",
    );
  });

  it("HTTP親trendとsubmetric合計の不一致を拒否する", () => {
    const raw = rawSummary();
    raw.metrics[httpSelector(GENERATOR_METRIC_NAMES.httpRequests)] =
      counterMetric(1);
    raw.metrics[httpSelector(GENERATOR_METRIC_NAMES.httpDuration)] = trendMetric({
      count: 1,
    });
    raw.metrics[GENERATOR_METRIC_NAMES.httpRequests] = counterMetric(1);

    expectRefusal(
      () => projectGeneratorMetricsSummary(raw, BINDING, GENERATED_AT),
      "metrics_summary_cardinality_invalid",
    );
  });

  it("未知のgenerator metricと固定series欠落を拒否する", () => {
    const unknown = rawSummary();
    unknown.metrics["generator_unbounded_tag"] = counterMetric();
    expectRefusal(
      () => projectGeneratorMetricsSummary(unknown, BINDING, GENERATED_AT),
      "metrics_summary_cardinality_invalid",
    );

    const missing = rawSummary();
    delete missing.metrics[
      metricSelector(GENERATOR_METRIC_NAMES.webSocketMessages, [
        ["message", "invalid"],
      ])
    ];
    expectRefusal(
      () => projectGeneratorMetricsSummary(missing, BINDING, GENERATED_AT),
      "metrics_summary_cardinality_invalid",
    );
  });

  it.each([
    ["negative counter", (raw: RawK6Summary) => {
      raw.metrics[GENERATOR_METRIC_NAMES.webSocketErrors] = counterMetric(-1);
    }],
    ["infinite counter rate", (raw: RawK6Summary) => {
      raw.metrics[GENERATOR_METRIC_NAMES.webSocketErrors] = counterMetric(
        0,
        Number.POSITIVE_INFINITY,
      );
    }],
    ["NaN run duration", (raw: RawK6Summary) => {
      raw.state.testRunDurationMs = Number.NaN;
    }],
    ["infinite trend percentile", (raw: RawK6Summary) => {
      raw.metrics[httpSelector(GENERATOR_METRIC_NAMES.httpDuration)] = trendMetric({
        "p(95)": Number.POSITIVE_INFINITY,
      });
    }],
  ])("非有限値・負数をcontract違反として拒否する: %s", (_name, mutate) => {
    const raw = rawSummary();
    mutate(raw);
    expectRefusal(
      () => projectGeneratorMetricsSummary(raw, BINDING, GENERATED_AT),
      "metrics_summary_contract_invalid",
    );
  });

  it.each([
    ["metric count", (raw: RawK6Summary) => {
      raw.metrics[httpSelector(GENERATOR_METRIC_NAMES.httpRequests)] =
        counterMetric(100_000_001);
    }],
    ["HTTP duration", (raw: RawK6Summary) => {
      raw.metrics[httpSelector(GENERATOR_METRIC_NAMES.httpRequests)] =
        counterMetric(1);
      raw.metrics[httpSelector(GENERATOR_METRIC_NAMES.httpDuration)] = trendMetric({
        count: 1,
        min: 120_000.001,
        avg: 120_000.001,
        med: 120_000.001,
        max: 120_000.001,
        "p(50)": 120_000.001,
        "p(95)": 120_000.001,
        "p(99)": 120_000.001,
      });
    }],
    ["test run duration", (raw: RawK6Summary) => {
      raw.state.testRunDurationMs = 86_400_001;
    }],
  ])("数値上限超過を拒否する: %s", (_name, mutate) => {
    const raw = rawSummary();
    mutate(raw);
    expectRefusal(
      () => projectGeneratorMetricsSummary(raw, BINDING, GENERATED_AT),
      "metrics_summary_numeric_invalid",
    );
  });

  it("percentile順序が逆転したdurationを拒否する", () => {
    const raw = rawSummary();
    raw.metrics[httpSelector(GENERATOR_METRIC_NAMES.httpRequests)] =
      counterMetric(1);
    raw.metrics[httpSelector(GENERATOR_METRIC_NAMES.httpDuration)] = trendMetric({
      count: 1,
      min: 1,
      avg: 2,
      med: 2,
      max: 5,
      "p(50)": 4,
      "p(95)": 3,
      "p(99)": 5,
    });
    raw.metrics[GENERATOR_METRIC_NAMES.httpRequests] = counterMetric(1);
    raw.metrics[GENERATOR_METRIC_NAMES.httpDuration] = trendMetric({ count: 1 });

    expectRefusal(
      () => projectGeneratorMetricsSummary(raw, BINDING, GENERATED_AT),
      "metrics_summary_contract_invalid",
    );
  });

  it.each([
    ["missing", GENERATOR_SUMMARY_TREND_STATS.slice(0, -1)],
    ["reversed", [...GENERATOR_SUMMARY_TREND_STATS].reverse()],
    ["additional", [...GENERATOR_SUMMARY_TREND_STATS, "p(90)"]],
  ] satisfies readonly (readonly [string, readonly string[]])[])(
    "summaryTrendStatsの欠落・順序差・追加を拒否する: %s",
    (_name, trendStats) => {
      const raw = rawSummary();
      raw.options.summaryTrendStats = [...trendStats];
      expectRefusal(
        () => projectGeneratorMetricsSummary(raw, BINDING, GENERATED_AT),
        "metrics_summary_contract_invalid",
      );
    },
  );
});

describe("generator metrics artifact boundary", () => {
  function projectedSummary() {
    const raw = rawSummary();
    populateSuccessfulSamples(raw);
    return projectGeneratorMetricsSummary(raw, BINDING, GENERATED_AT);
  }

  it("prefix付き1行をrenderしcanonical payloadをround-tripする", () => {
    const raw = rawSummary();
    populateSuccessfulSamples(raw);
    const rendered = renderGeneratorMetricsSummary(
      raw,
      BINDING,
      GENERATED_AT,
    );
    expect(rendered.startsWith(GENERATOR_METRICS_PREFIX)).toBe(true);
    expect(rendered.endsWith("\n")).toBe(true);
    expect(rendered.slice(0, -1)).not.toContain("\n");

    const payload = rendered.slice(GENERATOR_METRICS_PREFIX.length, -1);
    const parsed = parseGeneratorMetricsSummary(payload, BINDING);
    expect(serializeGeneratorMetricsSummary(parsed)).toBe(payload);
    expect(new TextEncoder().encode(`${payload}\n`).byteLength).toBeLessThanOrEqual(
      MAX_GENERATOR_METRICS_ARTIFACT_BYTES,
    );
  });

  it.each([
    ["runId", "33333333-3333-4333-8333-333333333333"],
    ["eventId", "44444444-4444-4444-8444-444444444444"],
    ["sourceCommit", "e".repeat(40)],
    ["schemaRevision", "load-test-v2"],
    ["manifestSha256", "f".repeat(64)],
    ["scenarioSha256", "0".repeat(64)],
    ["runtimeManifestSha256", "1".repeat(64)],
  ] satisfies readonly (readonly [keyof GeneratorMetricsBinding, string])[])(
    "binding不一致を個別に拒否する: %s",
    (field, replacement) => {
      const payload = serializeGeneratorMetricsSummary(projectedSummary());
      expectRefusal(
        () =>
          parseGeneratorMetricsSummary(payload, {
            ...BINDING,
            [field]: replacement,
          }),
        "metrics_summary_binding_invalid",
      );
    },
  );

  it("非canonical JSON・余分なfield・不正JSONを区別して拒否する", () => {
    const projected = projectedSummary();
    const nonCanonical = JSON.stringify(projected);
    expect(nonCanonical).not.toBe(serializeGeneratorMetricsSummary(projected));
    expectRefusal(
      () => parseGeneratorMetricsSummary(nonCanonical, BINDING),
      "metrics_summary_contract_invalid",
    );

    const extraField = canonicalJson({ ...projected, raw_metrics: {} });
    expectRefusal(
      () => parseGeneratorMetricsSummary(extraField, BINDING),
      "metrics_summary_contract_invalid",
    );
    expectRefusal(
      () => parseGeneratorMetricsSummary("{", BINDING),
      "metrics_summary_json_invalid",
    );
  });

  it("UTF-8 byte上限を超えるpayloadをJSON decodeより先に拒否する", () => {
    const oversized = "あ".repeat(MAX_GENERATOR_METRICS_ARTIFACT_BYTES);
    expectRefusal(
      () => parseGeneratorMetricsSummary(oversized, BINDING),
      "metrics_summary_size_invalid",
    );
  });

  it("canonical datetimeを6桁以下のUTC精度へ正規化する", () => {
    expect(canonicalSummaryDatetime(new Date("2026-09-04T01:02:03.000Z"))).toBe(
      "2026-09-04T01:02:03Z",
    );
    expect(canonicalSummaryDatetime(GENERATED_AT)).toBe(
      "2026-09-04T01:02:03.123000Z",
    );
    expectRefusal(
      () => canonicalSummaryDatetime(new Date(Number.NaN)),
      "metrics_summary_contract_invalid",
    );
  });
});
