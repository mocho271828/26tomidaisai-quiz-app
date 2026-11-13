import { z } from "zod";

import {
  GENERATOR_HTTP_OUTCOMES,
  GENERATOR_HTTP_ROUTES,
  GENERATOR_METRIC_NAMES,
  GENERATOR_METRIC_SERIES,
  GENERATOR_PROTOCOL_BOUNDARIES,
  GENERATOR_STATIC_ASSET_FAILURES,
  GENERATOR_STATIC_CACHE_MODES,
  GENERATOR_SUMMARY_TREND_STATS,
  GENERATOR_WEBSOCKET_ATTEMPTS,
  GENERATOR_WEBSOCKET_CLOSE_CODES,
  GENERATOR_WEBSOCKET_MESSAGES,
  GENERATOR_WEBSOCKET_OUTCOMES,
  metricSelector,
} from "./metric-taxonomy.js";
import {
  GENERATOR_METRICS_PREFIX,
  GeneratorMetricsSummaryRefusal,
  GeneratorMetricsSummarySchema,
  MAX_GENERATOR_HTTP_DURATION_MICROSECONDS,
  MAX_GENERATOR_METRIC_COUNT,
  MAX_GENERATOR_TEST_RUN_DURATION_MILLISECONDS,
  canonicalSummaryDatetime,
  serializeGeneratorMetricsSummary,
} from "./metrics-summary-contract.js";
import type {
  GeneratorMetricsBinding,
  GeneratorMetricsSummary,
} from "./metrics-summary-contract.js";

export {
  GENERATOR_METRIC_THRESHOLDS,
  GENERATOR_SUMMARY_TREND_STATS,
} from "./metric-taxonomy.js";
export {
  GENERATOR_METRICS_ARTIFACT_NAME,
  GENERATOR_METRICS_PREFIX,
  GeneratorMetricsSummaryRefusal,
  GeneratorMetricsSummarySchema,
  MAX_GENERATOR_METRICS_ARTIFACT_BYTES,
  canonicalSummaryDatetime,
  parseGeneratorMetricsSummary,
  serializeGeneratorMetricsSummary,
} from "./metrics-summary-contract.js";
export type {
  GeneratorMetricsBinding,
  GeneratorMetricsSummary,
  GeneratorMetricsSummaryRefusalCode,
} from "./metrics-summary-contract.js";

const finiteNonnegativeSchema = z.number().finite().nonnegative();
const k6CounterMetricSchema = z.object({
  type: z.literal("counter"),
  contains: z.literal("default"),
  values: z.strictObject({
    count: finiteNonnegativeSchema,
    rate: finiteNonnegativeSchema,
  }),
});
const k6TrendMetricSchema = z.object({
  type: z.literal("trend"),
  contains: z.literal("time"),
  values: z.strictObject({
    count: finiteNonnegativeSchema,
    min: finiteNonnegativeSchema,
    avg: finiteNonnegativeSchema,
    med: finiteNonnegativeSchema,
    max: finiteNonnegativeSchema,
    "p(50)": finiteNonnegativeSchema,
    "p(95)": finiteNonnegativeSchema,
    "p(99)": finiteNonnegativeSchema,
  }),
});
const k6SummarySchema = z.object({
  metrics: z.record(z.string(), z.unknown()),
  options: z.object({
    summaryTrendStats: z.array(z.string()),
  }),
  state: z.object({
    testRunDurationMs: finiteNonnegativeSchema,
  }),
});

/** k6集計から固定93 cellだけを抽出し、raw metric/tagをartifactへ持ち込まない。 */
export function projectGeneratorMetricsSummary(
  rawSummary: unknown,
  binding: GeneratorMetricsBinding,
  generatedAt: Date,
): GeneratorMetricsSummary {
  try {
    const summary = k6SummarySchema.parse(rawSummary);
    requireExpectedTrendStats(summary.options.summaryTrendStats);
    requireExpectedCustomMetricKeys(summary.metrics);

    const http = projectHttp(summary.metrics);
    const staticAssetFailures = projectCounterMatrix(
      summary.metrics,
      GENERATOR_METRIC_NAMES.staticAssetFailures,
      GENERATOR_STATIC_CACHE_MODES,
      (cacheMode) =>
        GENERATOR_STATIC_ASSET_FAILURES.map(
          (reason) => [["cache_mode", cacheMode], ["reason", reason]] as const,
        ),
      GENERATOR_STATIC_ASSET_FAILURES,
    );
    const connections = projectCounterMatrix(
      summary.metrics,
      GENERATOR_METRIC_NAMES.webSocketConnections,
      GENERATOR_WEBSOCKET_ATTEMPTS,
      (attempt) =>
        GENERATOR_WEBSOCKET_OUTCOMES.map(
          (outcome) => [["attempt", attempt], ["outcome", outcome]] as const,
        ),
      GENERATOR_WEBSOCKET_OUTCOMES,
    );
    const messages = projectCounterRecord(
      summary.metrics,
      GENERATOR_METRIC_NAMES.webSocketMessages,
      GENERATOR_WEBSOCKET_MESSAGES,
      (message) => [["message", message]],
    );
    const closes = projectCounterRecord(
      summary.metrics,
      GENERATOR_METRIC_NAMES.webSocketCloses,
      GENERATOR_WEBSOCKET_CLOSE_CODES,
      (code) => [["code", code]],
    );
    const protocolRefusals = projectCounterRecord(
      summary.metrics,
      GENERATOR_METRIC_NAMES.protocolRefusals,
      GENERATOR_PROTOCOL_BOUNDARIES,
      (boundary) => [["boundary", boundary]],
    );
    const webSocketErrors = readCounterMetric(
      summary.metrics,
      GENERATOR_METRIC_NAMES.webSocketErrors,
    );

    return GeneratorMetricsSummarySchema.parse({
      format: "load-test-generator-metrics-summary-v3",
      run_id: binding.runId,
      event_id: binding.eventId,
      source_commit: binding.sourceCommit,
      schema_revision: binding.schemaRevision,
      manifest_sha256: binding.manifestSha256,
      scenario_sha256: binding.scenarioSha256,
      runtime_manifest_sha256: binding.runtimeManifestSha256,
      generated_at: canonicalSummaryDatetime(generatedAt),
      test_run_duration_ms: scaleFiniteNumber(
        summary.state.testRunDurationMs,
        1,
        MAX_GENERATOR_TEST_RUN_DURATION_MILLISECONDS,
      ),
      http,
      static_asset_failures: staticAssetFailures,
      websocket: {
        connections,
        messages,
        closes,
        error_count: requireMetricCount(webSocketErrors.count),
      },
      protocol_refusals: protocolRefusals,
    });
  } catch (error: unknown) {
    if (error instanceof GeneratorMetricsSummaryRefusal) {
      throw error;
    }
    throw new GeneratorMetricsSummaryRefusal("metrics_summary_contract_invalid");
  }
}

/** canonical JSONだけをstdoutへ返し、k6自身にはfileを書かせない。 */
export function renderGeneratorMetricsSummary(
  rawSummary: unknown,
  binding: GeneratorMetricsBinding,
  generatedAt: Date,
): string {
  const evidence = projectGeneratorMetricsSummary(rawSummary, binding, generatedAt);
  return `${GENERATOR_METRICS_PREFIX}${serializeGeneratorMetricsSummary(evidence)}\n`;
}

type K6MetricMap = Readonly<Record<string, unknown>>;
type K6CounterValues = z.infer<typeof k6CounterMetricSchema>["values"];
type K6TrendValues = z.infer<typeof k6TrendMetricSchema>["values"];

function projectHttp(
  metrics: K6MetricMap,
): GeneratorMetricsSummary["http"] {
  let requestTotal = 0;
  let durationTotal = 0;
  const routes = recordFromKeys(GENERATOR_HTTP_ROUTES, (route) =>
    recordFromKeys(GENERATOR_HTTP_OUTCOMES, (outcome) => {
      const tags = [
        ["route", route],
        ["outcome", outcome],
      ] as const;
      const requests = readCounterMetric(
        metrics,
        metricSelector(GENERATOR_METRIC_NAMES.httpRequests, tags),
      );
      const duration = readTrendMetric(
        metrics,
        metricSelector(GENERATOR_METRIC_NAMES.httpDuration, tags),
      );
      const requestCount = requireMetricCount(requests.count);
      const durationCount = requireMetricCount(duration.count);
      if (requestCount !== durationCount) {
        throw new GeneratorMetricsSummaryRefusal(
          "metrics_summary_cardinality_invalid",
        );
      }
      requestTotal = addMetricCounts(requestTotal, requestCount);
      durationTotal = addMetricCounts(durationTotal, durationCount);
      return {
        request_count: requestCount,
        request_rate_milli_per_second: scaleFiniteNumber(
          requests.rate,
          1_000,
          Number.MAX_SAFE_INTEGER,
        ),
        duration_microseconds: projectDuration(duration),
      };
    }),
  );
  requireParentCount(metrics, GENERATOR_METRIC_NAMES.httpRequests, requestTotal);
  requireParentTrendCount(metrics, GENERATOR_METRIC_NAMES.httpDuration, durationTotal);
  return routes;
}

function projectDuration(
  duration: K6TrendValues,
): GeneratorMetricsSummary["http"][keyof GeneratorMetricsSummary["http"]][keyof GeneratorMetricsSummary["http"][keyof GeneratorMetricsSummary["http"]]]["duration_microseconds"] {
  const count = requireMetricCount(duration.count);
  if (count === 0) {
    if (
      duration.min !== 0 ||
      duration.avg !== 0 ||
      duration.med !== 0 ||
      duration.max !== 0 ||
      duration["p(50)"] !== 0 ||
      duration["p(95)"] !== 0 ||
      duration["p(99)"] !== 0
    ) {
      throw new GeneratorMetricsSummaryRefusal("metrics_summary_numeric_invalid");
    }
    return null;
  }
  return {
    minimum_microseconds: scaleHttpDuration(duration.min),
    mean_microseconds: scaleHttpDuration(duration.avg),
    p50_microseconds: scaleHttpDuration(duration["p(50)"]),
    p95_microseconds: scaleHttpDuration(duration["p(95)"]),
    p99_microseconds: scaleHttpDuration(duration["p(99)"]),
    maximum_microseconds: scaleHttpDuration(duration.max),
  };
}

function projectCounterMatrix<
  Outer extends readonly [string, ...string[]],
  Inner extends readonly [string, ...string[]],
>(
  metrics: K6MetricMap,
  metricName: string,
  outerKeys: Outer,
  tagSets: (outer: Outer[number]) => readonly (readonly (readonly [string, string])[])[],
  innerKeys: Inner,
): Record<Outer[number], Record<Inner[number], number>> {
  let total = 0;
  const matrix = recordFromKeys(outerKeys, (outer) => {
    const selectors = tagSets(outer);
    if (selectors.length !== innerKeys.length) {
      throw new GeneratorMetricsSummaryRefusal(
        "metrics_summary_cardinality_invalid",
      );
    }
    return recordFromKeys(innerKeys, (_inner, index) => {
      const tags = selectors[index];
      if (tags === undefined) {
        throw new GeneratorMetricsSummaryRefusal(
          "metrics_summary_cardinality_invalid",
        );
      }
      const count = requireMetricCount(
        readCounterMetric(metrics, metricSelector(metricName, tags)).count,
      );
      total = addMetricCounts(total, count);
      return count;
    });
  });
  requireParentCount(metrics, metricName, total);
  return matrix;
}

function projectCounterRecord<Keys extends readonly [string, ...string[]]>(
  metrics: K6MetricMap,
  metricName: string,
  keys: Keys,
  tags: (key: Keys[number]) => readonly (readonly [string, string])[],
): Record<Keys[number], number> {
  let total = 0;
  const record = recordFromKeys(keys, (key) => {
    const count = requireMetricCount(
      readCounterMetric(metrics, metricSelector(metricName, tags(key))).count,
    );
    total = addMetricCounts(total, count);
    return count;
  });
  requireParentCount(metrics, metricName, total);
  return record;
}

function readCounterMetric(metrics: K6MetricMap, selector: string): K6CounterValues {
  const result = k6CounterMetricSchema.safeParse(metrics[selector]);
  if (!result.success) {
    throw new GeneratorMetricsSummaryRefusal("metrics_summary_contract_invalid");
  }
  return result.data.values;
}

function readTrendMetric(metrics: K6MetricMap, selector: string): K6TrendValues {
  const result = k6TrendMetricSchema.safeParse(metrics[selector]);
  if (!result.success) {
    throw new GeneratorMetricsSummaryRefusal("metrics_summary_contract_invalid");
  }
  return result.data.values;
}

function requireParentCount(
  metrics: K6MetricMap,
  metricName: string,
  expected: number,
): void {
  if (requireMetricCount(readCounterMetric(metrics, metricName).count) !== expected) {
    throw new GeneratorMetricsSummaryRefusal(
      "metrics_summary_cardinality_invalid",
    );
  }
}

function requireParentTrendCount(
  metrics: K6MetricMap,
  metricName: string,
  expected: number,
): void {
  if (requireMetricCount(readTrendMetric(metrics, metricName).count) !== expected) {
    throw new GeneratorMetricsSummaryRefusal(
      "metrics_summary_cardinality_invalid",
    );
  }
}

function requireExpectedTrendStats(actual: readonly string[]): void {
  if (
    actual.length !== GENERATOR_SUMMARY_TREND_STATS.length ||
    actual.some((value, index) => value !== GENERATOR_SUMMARY_TREND_STATS[index])
  ) {
    throw new GeneratorMetricsSummaryRefusal("metrics_summary_contract_invalid");
  }
}

function requireExpectedCustomMetricKeys(metrics: K6MetricMap): void {
  const expected = new Set(GENERATOR_METRIC_SERIES.map((entry) => entry.selector));
  for (const selector of expected) {
    if (!Object.hasOwn(metrics, selector)) {
      throw new GeneratorMetricsSummaryRefusal(
        "metrics_summary_cardinality_invalid",
      );
    }
  }
  for (const key of Object.keys(metrics)) {
    if (key.startsWith("generator_") && !expected.has(key)) {
      throw new GeneratorMetricsSummaryRefusal(
        "metrics_summary_cardinality_invalid",
      );
    }
  }
}

function requireMetricCount(value: number): number {
  if (
    !Number.isSafeInteger(value) ||
    value < 0 ||
    value > MAX_GENERATOR_METRIC_COUNT
  ) {
    throw new GeneratorMetricsSummaryRefusal("metrics_summary_numeric_invalid");
  }
  return value;
}

function addMetricCounts(left: number, right: number): number {
  return requireMetricCount(left + right);
}

function scaleHttpDuration(value: number): number {
  return scaleFiniteNumber(
    value,
    1_000,
    MAX_GENERATOR_HTTP_DURATION_MICROSECONDS,
  );
}

function scaleFiniteNumber(value: number, scale: number, maximum: number): number {
  const scaled = Math.round(value * scale);
  if (
    !Number.isFinite(value) ||
    value < 0 ||
    !Number.isSafeInteger(scaled) ||
    scaled < 0 ||
    scaled > maximum
  ) {
    throw new GeneratorMetricsSummaryRefusal("metrics_summary_numeric_invalid");
  }
  return scaled;
}

function recordFromKeys<Keys extends readonly [string, ...string[]], Value>(
  keys: Keys,
  create: (key: Keys[number], index: number) => Value,
): Record<Keys[number], Value> {
  return Object.fromEntries(
    keys.map((key, index) => [key, create(key, index)]),
  ) as Record<Keys[number], Value>;
}
