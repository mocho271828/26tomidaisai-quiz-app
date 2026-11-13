import { z } from "zod";

import { canonicalJson } from "../canonical-json-core.js";
import {
  GENERATOR_HTTP_OUTCOMES,
  GENERATOR_HTTP_ROUTES,
  GENERATOR_PROTOCOL_BOUNDARIES,
  GENERATOR_STATIC_ASSET_FAILURES,
  GENERATOR_STATIC_CACHE_MODES,
  GENERATOR_WEBSOCKET_ATTEMPTS,
  GENERATOR_WEBSOCKET_CLOSE_CODES,
  GENERATOR_WEBSOCKET_MESSAGES,
  GENERATOR_WEBSOCKET_OUTCOMES,
} from "./metric-taxonomy.js";

export const GENERATOR_METRICS_PREFIX = "@@LOAD_TEST_METRICS@@";
export const GENERATOR_METRICS_ARTIFACT_NAME =
  "generator-metrics-summary.json";
export const MAX_GENERATOR_METRICS_ARTIFACT_BYTES = 32 * 1_024;
export const MAX_GENERATOR_METRIC_COUNT = 100_000_000;
export const MAX_GENERATOR_HTTP_DURATION_MICROSECONDS = 120_000_000;
export const MAX_GENERATOR_TEST_RUN_DURATION_MILLISECONDS =
  24 * 60 * 60 * 1_000;

const SHA256_PATTERN = /^[0-9a-f]{64}(?![\s\S])/;
const SOURCE_COMMIT_PATTERN = /^[0-9a-f]{40}(?![\s\S])/;
const CANONICAL_AWARE_DATETIME_PATTERN =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z$/;
const canonicalAwareDatetimeSchema = z
  .iso
  .datetime({ offset: true })
  .regex(CANONICAL_AWARE_DATETIME_PATTERN)
  .refine((value) => !value.endsWith(".000000Z"), "canonical datetime required");
const metricCountSchema = z
  .number()
  .int()
  .min(0)
  .max(MAX_GENERATOR_METRIC_COUNT);
const durationMicrosecondsSchema = z
  .number()
  .int()
  .min(0)
  .max(MAX_GENERATOR_HTTP_DURATION_MICROSECONDS);
const countRecordSchema = <T extends readonly [string, ...string[]]>(values: T) =>
  z.record(z.enum(values), metricCountSchema);

const httpDurationSchema = z
  .strictObject({
    minimum_microseconds: durationMicrosecondsSchema,
    mean_microseconds: durationMicrosecondsSchema,
    p50_microseconds: durationMicrosecondsSchema,
    p95_microseconds: durationMicrosecondsSchema,
    p99_microseconds: durationMicrosecondsSchema,
    maximum_microseconds: durationMicrosecondsSchema,
  })
  .superRefine((duration, context) => {
    if (
      duration.minimum_microseconds > duration.mean_microseconds ||
      duration.mean_microseconds > duration.maximum_microseconds ||
      duration.minimum_microseconds > duration.p50_microseconds ||
      duration.p50_microseconds > duration.p95_microseconds ||
      duration.p95_microseconds > duration.p99_microseconds ||
      duration.p99_microseconds > duration.maximum_microseconds
    ) {
      context.addIssue({ code: "custom", message: "duration order invalid" });
    }
  });
const httpCellSchema = z
  .strictObject({
    request_count: metricCountSchema,
    request_rate_milli_per_second: z.number().int().nonnegative().safe(),
    duration_microseconds: z.union([httpDurationSchema, z.null()]),
  })
  .superRefine((cell, context) => {
    if (
      (cell.request_count === 0 && cell.duration_microseconds !== null) ||
      (cell.request_count > 0 && cell.duration_microseconds === null)
    ) {
      context.addIssue({ code: "custom", message: "duration presence invalid" });
    }
    if (cell.request_count === 0 && cell.request_rate_milli_per_second !== 0) {
      context.addIssue({ code: "custom", message: "zero count rate invalid" });
    }
  });

export const GeneratorMetricsSummarySchema = z.strictObject({
  format: z.literal("load-test-generator-metrics-summary-v3"),
  run_id: z.uuid(),
  event_id: z.uuid(),
  source_commit: z.string().regex(SOURCE_COMMIT_PATTERN),
  schema_revision: z.string().min(1).max(128),
  manifest_sha256: z.string().regex(SHA256_PATTERN),
  scenario_sha256: z.string().regex(SHA256_PATTERN),
  runtime_manifest_sha256: z.string().regex(SHA256_PATTERN),
  generated_at: canonicalAwareDatetimeSchema,
  test_run_duration_ms: z
    .number()
    .int()
    .min(0)
    .max(MAX_GENERATOR_TEST_RUN_DURATION_MILLISECONDS),
  http: z.record(
    z.enum(GENERATOR_HTTP_ROUTES),
    z.record(z.enum(GENERATOR_HTTP_OUTCOMES), httpCellSchema),
  ),
  static_asset_failures: z.record(
    z.enum(GENERATOR_STATIC_CACHE_MODES),
    countRecordSchema(GENERATOR_STATIC_ASSET_FAILURES),
  ),
  websocket: z.strictObject({
    connections: z.record(
      z.enum(GENERATOR_WEBSOCKET_ATTEMPTS),
      countRecordSchema(GENERATOR_WEBSOCKET_OUTCOMES),
    ),
    messages: countRecordSchema(GENERATOR_WEBSOCKET_MESSAGES),
    closes: countRecordSchema(GENERATOR_WEBSOCKET_CLOSE_CODES),
    error_count: metricCountSchema,
  }),
  protocol_refusals: countRecordSchema(GENERATOR_PROTOCOL_BOUNDARIES),
});
export type GeneratorMetricsSummary = z.infer<
  typeof GeneratorMetricsSummarySchema
>;

export interface GeneratorMetricsBinding {
  readonly runId: string;
  readonly eventId: string;
  readonly sourceCommit: string;
  readonly schemaRevision: string;
  readonly manifestSha256: string;
  readonly scenarioSha256: string;
  readonly runtimeManifestSha256: string;
}

export type GeneratorMetricsSummaryRefusalCode =
  | "metrics_summary_binding_invalid"
  | "metrics_summary_cardinality_invalid"
  | "metrics_summary_contract_invalid"
  | "metrics_summary_json_invalid"
  | "metrics_summary_numeric_invalid"
  | "metrics_summary_size_invalid";

export class GeneratorMetricsSummaryRefusal extends Error {
  readonly code: GeneratorMetricsSummaryRefusalCode;

  constructor(code: GeneratorMetricsSummaryRefusalCode) {
    super(code);
    this.name = "GeneratorMetricsSummaryRefusal";
    this.code = code;
  }
}

/** host側でstrict schema・binding・canonical bytesを同時に照合する。 */
export function parseGeneratorMetricsSummary(
  payload: string,
  binding: GeneratorMetricsBinding,
): GeneratorMetricsSummary {
  if (
    new TextEncoder().encode(`${payload}\n`).byteLength >
    MAX_GENERATOR_METRICS_ARTIFACT_BYTES
  ) {
    throw new GeneratorMetricsSummaryRefusal("metrics_summary_size_invalid");
  }
  let decoded: unknown;
  try {
    decoded = JSON.parse(payload) as unknown;
  } catch {
    throw new GeneratorMetricsSummaryRefusal("metrics_summary_json_invalid");
  }
  const result = GeneratorMetricsSummarySchema.safeParse(decoded);
  if (!result.success) {
    throw new GeneratorMetricsSummaryRefusal("metrics_summary_contract_invalid");
  }
  requireBinding(result.data, binding);
  if (payload !== canonicalJson(result.data)) {
    throw new GeneratorMetricsSummaryRefusal("metrics_summary_contract_invalid");
  }
  return result.data;
}

export function serializeGeneratorMetricsSummary(
  summary: GeneratorMetricsSummary,
): string {
  const result = GeneratorMetricsSummarySchema.safeParse(summary);
  if (!result.success) {
    throw new GeneratorMetricsSummaryRefusal("metrics_summary_contract_invalid");
  }
  const payload = canonicalJson(result.data);
  if (
    new TextEncoder().encode(`${payload}\n`).byteLength >
    MAX_GENERATOR_METRICS_ARTIFACT_BYTES
  ) {
    throw new GeneratorMetricsSummaryRefusal("metrics_summary_size_invalid");
  }
  return payload;
}

/** JavaScript millisecond時刻を既存Pydantic canonical datetimeへ揃える。 */
export function canonicalSummaryDatetime(date: Date): string {
  if (!(date instanceof Date) || !Number.isFinite(date.getTime())) {
    throw new GeneratorMetricsSummaryRefusal("metrics_summary_contract_invalid");
  }
  return date.toISOString().replace(/\.(\d{3})Z$/, (_match, milliseconds: string) =>
    milliseconds === "000" ? "Z" : `.${milliseconds}000Z`,
  );
}

function requireBinding(
  summary: GeneratorMetricsSummary,
  binding: GeneratorMetricsBinding,
): void {
  if (
    summary.run_id !== binding.runId ||
    summary.event_id !== binding.eventId ||
    summary.source_commit !== binding.sourceCommit ||
    summary.schema_revision !== binding.schemaRevision ||
    summary.manifest_sha256 !== binding.manifestSha256 ||
    summary.scenario_sha256 !== binding.scenarioSha256 ||
    summary.runtime_manifest_sha256 !== binding.runtimeManifestSha256
  ) {
    throw new GeneratorMetricsSummaryRefusal("metrics_summary_binding_invalid");
  }
}
