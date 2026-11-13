import { execFileSync, spawnSync } from "node:child_process";
import { Buffer } from "node:buffer";
import { realpathSync } from "node:fs";
import { dirname, join } from "node:path";
import process from "node:process";
import { URL, fileURLToPath } from "node:url";

const K6_IMAGE =
  "ghcr.io/grafana/k6:2.2.0@sha256:9bd01d6941fca969cb61bb57d2da5ee9b385fe2aa8881df3798c196564d6ace6";
const GENERATOR_METRICS_PREFIX = "@@LOAD_TEST_METRICS@@";
const SUMMARY_SMOKE_BINDING = Object.freeze({
  runId: "11111111-1111-4111-8111-111111111111",
  eventId: "22222222-2222-4222-8222-222222222222",
  sourceCommit: "a".repeat(40),
  schemaRevision: "summary-smoke-v1",
  manifestSha256: "b".repeat(64),
  scenarioSha256: "c".repeat(64),
  runtimeManifestSha256: "d".repeat(64),
});
const EXPECTED_SUMMARY_CELL_COUNT = 76;
const PACKAGE_ROOT = realpathSync(join(dirname(fileURLToPath(import.meta.url)), ".."));
const REPOSITORY_ROOT = realpathSync(join(PACKAGE_ROOT, "../.."));

execFileSync("pnpm", ["build"], {
  cwd: PACKAGE_ROOT,
  stdio: "inherit",
});
const metricsSummaryBundle = await import(
  new URL("../dist/metrics-summary.js", import.meta.url)
);
if (typeof metricsSummaryBundle.parseGeneratorMetricsSummary !== "function") {
  throw new Error("k6_summary_parser_entrypoint_missing");
}

const fixtureOutput = execFileSync(
  process.execPath,
  [join(PACKAGE_ROOT, "dist/inspect-fixture.js")],
  { cwd: REPOSITORY_ROOT, encoding: "utf8" },
);
const fixture = parseFixture(fixtureOutput);
const dockerArguments = [
  "run",
  "--rm",
  "--network=none",
  "--read-only",
  "--cap-drop=ALL",
  "--security-opt=no-new-privileges:true",
  "--user=65534:65534",
  "--mount",
  `type=bind,src=${REPOSITORY_ROOT},dst=/work,readonly`,
  "--workdir=/work",
  "--env=LOAD_TEST_RUNTIME_MANIFEST_JSON",
  "--env=LOAD_TEST_RUNTIME_MANIFEST_SHA256",
  K6_IMAGE,
  "inspect",
  "--include-system-env-vars",
  "--execution-requirements",
  "/work/load-test/k6/dist/generator.js",
];
const inspectOutput = execFileSync(
  "docker",
  dockerArguments,
  {
    cwd: REPOSITORY_ROOT,
    encoding: "utf8",
    env: { ...process.env, ...fixture },
    maxBuffer: 2 * 1_024 * 1_024,
    stdio: ["ignore", "pipe", "inherit"],
  },
);

const inspected = parseRecord(inspectOutput, "k6_inspect_json_invalid");
const stages = requireStages(inspected);
const expectedStages = [
  ["2m30s", 100],
  ["2m30s", 300],
  ["2m30s", 500],
  ["2m30s", 600],
  ["1h5m0s", 600],
  ["5m0s", 0],
];
if (
  inspected["totalDuration"] !== "1h20m0s" ||
  inspected["maxVUs"] !== 600 ||
  stages.length !== expectedStages.length ||
  stages.some((stage, index) => {
    const expected = expectedStages[index];
    return expected === undefined ||
      stage["duration"] !== expected[0] ||
      stage["target"] !== expected[1];
  })
) {
  throw new Error("k6_inspect_execution_requirements_mismatch");
}
requireMetricsInspectOptions(
  inspected,
  metricsSummaryBundle.GENERATOR_METRIC_THRESHOLDS,
  metricsSummaryBundle.GENERATOR_SUMMARY_TREND_STATS,
);

const tamperedInspect = spawnSync("docker", dockerArguments, {
  cwd: REPOSITORY_ROOT,
  encoding: "utf8",
  env: {
    ...process.env,
    ...fixture,
    LOAD_TEST_RUNTIME_MANIFEST_SHA256: "0".repeat(64),
  },
  maxBuffer: 2 * 1_024 * 1_024,
  stdio: ["ignore", "pipe", "pipe"],
});
if (
  tamperedInspect.status === 0 ||
  !`${tamperedInspect.stdout}${tamperedInspect.stderr}`.includes(
    "generator_manifest_binding_mismatch",
  )
) {
  throw new Error("k6_inspect_tamper_was_not_rejected");
}

const summarySmoke = spawnSync(
  "docker",
  [
    "run",
    "--rm",
    "--network=none",
    "--read-only",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges:true",
    "--user=65534:65534",
    "--mount",
    `type=bind,src=${REPOSITORY_ROOT},dst=/work,readonly`,
    "--workdir=/work",
    K6_IMAGE,
    "run",
    "--quiet",
    "--summary-mode=full",
    "--no-color",
    "--no-usage-report",
    "--log-output=stdout",
    "--log-format=raw",
    "/work/load-test/k6/dist/summary-smoke.js",
  ],
  {
    cwd: REPOSITORY_ROOT,
    encoding: "utf8",
    maxBuffer: 2 * 1_024 * 1_024,
    stdio: ["ignore", "pipe", "pipe"],
  },
);
if (summarySmoke.error !== undefined || summarySmoke.status !== 0) {
  throw new Error(
    `k6_summary_smoke_failed:${boundedProcessOutput(summarySmoke)}`,
  );
}
const summaryPayload = extractSoleMetricsPayload(summarySmoke.stdout);
const summary = metricsSummaryBundle.parseGeneratorMetricsSummary(
  summaryPayload,
  SUMMARY_SMOKE_BINDING,
);
const summaryCellCount = validateSummarySmoke(summary);

process.stdout.write(
  `${JSON.stringify({
    image: K6_IMAGE,
    maxVUs: inspected["maxVUs"],
    summarySmoke: {
      artifactBytes: Buffer.byteLength(`${summaryPayload}\n`, "utf8"),
      cellCount: summaryCellCount,
      format: summary.format,
    },
    stages,
    totalDuration: inspected["totalDuration"],
  })}\n`,
);

function extractSoleMetricsPayload(output) {
  const lines = output.split(/\r?\n/).filter((line) => line.length > 0);
  if (lines.length !== 1 || !lines[0]?.startsWith(GENERATOR_METRICS_PREFIX)) {
    throw new Error("k6_summary_prefix_cardinality_invalid");
  }
  const line = lines[0];
  if (line === undefined || line.length === GENERATOR_METRICS_PREFIX.length) {
    throw new Error("k6_summary_payload_missing");
  }
  return line.slice(GENERATOR_METRICS_PREFIX.length);
}

function validateSummarySmoke(summary) {
  const duration = summary.http.readiness.success.duration_microseconds;
  const durationValues = duration === null ? [] : Object.values(duration);
  if (
    summary.format !== "load-test-generator-metrics-summary-v3" ||
    summary.generated_at !== "2026-09-04T00:00:00Z" ||
    summary.http.readiness.success.request_count !== 1 ||
    summary.http.readiness.success.request_rate_milli_per_second <= 0 ||
    durationValues.length !== 6 ||
    durationValues.some((value) => value !== 12_500) ||
    summary.static_asset_failures.cold.index_request_failed !== 1 ||
    summary.websocket.connections.initial.opened !== 1 ||
    summary.websocket.messages.state_invalidated !== 1 ||
    summary.websocket.closes["1008"] !== 1 ||
    summary.websocket.error_count !== 0 ||
    summary.protocol_refusals.payload !== 1
  ) {
    throw new Error("k6_summary_representative_metrics_mismatch");
  }

  const cellCount =
    Object.values(summary.http).reduce(
      (total, outcomes) => total + Object.keys(outcomes).length,
      0,
    ) +
    Object.values(summary.static_asset_failures).reduce(
      (total, failures) => total + Object.keys(failures).length,
      0,
    ) +
    Object.values(summary.websocket.connections).reduce(
      (total, outcomes) => total + Object.keys(outcomes).length,
      0,
    ) +
    Object.keys(summary.websocket.messages).length +
    Object.keys(summary.websocket.closes).length +
    1 +
    Object.keys(summary.protocol_refusals).length;
  if (cellCount !== EXPECTED_SUMMARY_CELL_COUNT) {
    throw new Error("k6_summary_cell_cardinality_invalid");
  }
  return cellCount;
}

function requireMetricsInspectOptions(
  inspected,
  expectedThresholds,
  expectedTrendStats,
) {
  const actualThresholds = inspected["thresholds"];
  if (
    expectedThresholds === null ||
    typeof expectedThresholds !== "object" ||
    Array.isArray(expectedThresholds) ||
    actualThresholds === null ||
    typeof actualThresholds !== "object" ||
    Array.isArray(actualThresholds) ||
    !Array.isArray(expectedTrendStats)
  ) {
    throw new Error("k6_inspect_thresholds_invalid");
  }

  const expectedKeys = Object.keys(expectedThresholds).sort();
  const actualKeys = Object.keys(actualThresholds).sort();
  if (
    expectedKeys.length !== 133 ||
    actualKeys.length !== 133 ||
    actualKeys.some((key, index) => key !== expectedKeys[index])
  ) {
    throw new Error("k6_inspect_thresholds_invalid");
  }

  let counterThresholdCount = 0;
  let trendThresholdCount = 0;
  for (const selector of expectedKeys) {
    const expected = expectedThresholds[selector];
    const actual = actualThresholds[selector];
    if (
      !Array.isArray(expected) ||
      expected.length !== 1 ||
      !Array.isArray(actual) ||
      actual.length !== 1 ||
      actual[0] !== expected[0]
    ) {
      throw new Error("k6_inspect_thresholds_invalid");
    }
    counterThresholdCount += actual[0] === "count>=0" ? 1 : 0;
    trendThresholdCount += actual[0] === "max>=0" ? 1 : 0;
  }
  if (counterThresholdCount !== 82 || trendThresholdCount !== 51) {
    throw new Error("k6_inspect_thresholds_invalid");
  }

  const actualTrendStats = inspected["summaryTrendStats"];
  if (
    !Array.isArray(actualTrendStats) ||
    actualTrendStats.length !== expectedTrendStats.length ||
    actualTrendStats.some((value, index) => value !== expectedTrendStats[index])
  ) {
    throw new Error("k6_inspect_summary_trend_stats_invalid");
  }
  if (inspected["handleSummaryTimeout"] !== "5s") {
    throw new Error("k6_inspect_handle_summary_timeout_invalid");
  }
}

function boundedProcessOutput(result) {
  const output = `${result.stdout ?? ""}${result.stderr ?? ""}`.trim();
  return output.slice(0, 2_048);
}

function parseFixture(value) {
  const fixture = parseRecord(value, "k6_inspect_fixture_invalid");
  const keys = Object.keys(fixture).sort();
  if (
    keys.join(",") !==
      "LOAD_TEST_RUNTIME_MANIFEST_JSON,LOAD_TEST_RUNTIME_MANIFEST_SHA256" ||
    typeof fixture["LOAD_TEST_RUNTIME_MANIFEST_JSON"] !== "string" ||
    fixture["LOAD_TEST_RUNTIME_MANIFEST_JSON"].length >= 64 * 1_024 ||
    typeof fixture["LOAD_TEST_RUNTIME_MANIFEST_SHA256"] !== "string" ||
    !/^[0-9a-f]{64}$/.test(fixture["LOAD_TEST_RUNTIME_MANIFEST_SHA256"])
  ) {
    throw new Error("k6_inspect_fixture_invalid");
  }
  return fixture;
}

function parseRecord(value, code) {
  let parsed;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new Error(code);
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(code);
  }
  return parsed;
}

function requireStages(inspected) {
  const scenarios = inspected["scenarios"];
  if (scenarios === null || typeof scenarios !== "object") {
    throw new Error("k6_inspect_execution_requirements_invalid");
  }
  const scenario = scenarios["participant_sessions"];
  if (scenario === null || typeof scenario !== "object") {
    throw new Error("k6_inspect_execution_requirements_invalid");
  }
  const stages = scenario["stages"];
  if (
    scenario["executor"] !== "ramping-vus" ||
    scenario["exec"] !== "runParticipantSession" ||
    scenario["startVUs"] !== 0 ||
    scenario["gracefulStop"] !== "0s" ||
    scenario["gracefulRampDown"] !== "0s" ||
    !Array.isArray(stages) ||
    stages.some(
      (stage) => stage === null || typeof stage !== "object" || Array.isArray(stage),
    )
  ) {
    throw new Error("k6_inspect_execution_requirements_invalid");
  }
  return stages;
}
