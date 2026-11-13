import { describe, expect, it, vi } from "vitest";

import { canonicalSha256 } from "../src/canonical-json.js";
import {
  GeneratorOutputRefusal,
  GeneratorOutputStreamProcessor,
} from "../src/generator-output-stream-processor.js";
import { GeneratorStreamProcessingRefusal } from "../src/generator-stream-refusal.js";
import {
  GENERATOR_METRIC_NAMES,
  GENERATOR_METRIC_SERIES,
  GENERATOR_SUMMARY_TREND_STATS,
  metricSelector,
} from "../src/generator/metric-taxonomy.js";
import {
  GENERATOR_METRICS_ARTIFACT_NAME,
  GENERATOR_METRICS_PREFIX,
  MAX_GENERATOR_METRICS_ARTIFACT_BYTES,
  renderGeneratorMetricsSummary,
} from "../src/generator/metrics-summary.js";
import type { GeneratorMetricsBinding } from "../src/generator/metrics-summary.js";
import { projectK6RuntimeManifest } from "../src/generator/runtime-manifest.js";
import type { LoadedManifest } from "../src/manifest.js";
import {
  buildEvent,
  buildRoundEvents,
  encodeEventLine,
  loadedManifest,
  sealTime,
} from "./generator-event-fixtures.js";

const encoder = new TextEncoder();
const fixtureManifest = loadedManifest();
const fixtureBinding = metricsBinding(fixtureManifest);
const validMetricsLine = encoder.encode(
  renderGeneratorMetricsSummary(
    rawK6Summary(),
    fixtureBinding,
    new Date("2026-09-03T02:20:00Z"),
  ),
);
const allRoundLines = completeRoundStream(fixtureManifest);

function processor(includeCanary = true): GeneratorOutputStreamProcessor {
  const output = new GeneratorOutputStreamProcessor(fixtureManifest, {
    clock: (roundNumber) =>
      new Date(sealTime(fixtureManifest.manifest, roundNumber)),
  });
  if (includeCanary) {
    output.push(canaryLine(1));
    output.push(canaryLine(2));
  }
  return output;
}

function canaryLine(vuId: number, offset = 5_000): Uint8Array {
  return encoder.encode(`@@LOAD_TEST_CANARY@@${JSON.stringify({
    format: "load-test-generator-canary-v1", run_id: fixtureManifest.manifest.run_id,
    event_id: fixtureManifest.manifest.event.id, manifest_sha256: fixtureManifest.hashes.manifestSha256,
    vu_id: vuId, join_checked: true, session_checked: true, readiness_checked: true,
    websocket_checked: true,
    observed_at_unix_ms: Date.parse(fixtureManifest.manifest.scheduled_start_at) + offset,
  })}\n`);
}

describe("generator canary stream", () => {
  it("rampの後続close行がpartialなら、古い600集合でsealしない", () => {
    const start = Date.parse(fixtureManifest.manifest.scheduled_start_at);
    const clock = vi.spyOn(Date, "now").mockReturnValue(start + 600_000);
    const connection = (vuId: number, connected: boolean, offset: number): string =>
      `@@LOAD_TEST_SESSION_CONNECTION@@${JSON.stringify({
        format: "load-test-generator-session-connection-v1", run_id: fixtureManifest.manifest.run_id,
        event_id: fixtureManifest.manifest.event.id, manifest_sha256: fixtureManifest.hashes.manifestSha256,
        vu_id: vuId, session_checked: true, websocket_connected: connected,
        observed_at_unix_ms: start + offset,
      })}\n`;
    try {
      const output = processor();
      output.push(encoder.encode(Array.from({ length: 600 }, (_, index) => connection(index + 1, true, 599_000)).join("")));
      const close = connection(1, false, 600_000);
      output.push(encoder.encode(close.slice(0, -1)));
      expect(output.takeProgressArtifact()).toBeNull();
      output.push(encoder.encode("\n"));
      expect(output.takeProgressArtifact()).toBeNull();
      clock.mockReturnValue(start + 601_000);
      output.push(encoder.encode(connection(1, true, 601_000)));
      expect(output.takeProgressArtifact()?.name).toBe("generator-ramp-ready.json");
    } finally { clock.mockRestore(); }
  });
  it("VU1/2の完了を別々に受理して初期join確認を成立させる", () => {
    const output = processor(false);
    expect(output.isCanarySatisfied()).toBe(false);
    output.push(canaryLine(1));
    expect(output.isCanarySatisfied()).toBe(false);
    output.push(canaryLine(2));
    expect(output.isCanarySatisfied()).toBe(true);
  });
  it.each([[3, 5000], [1, 30001], [1, -1]])("対象外・期限外のcanaryを拒否する", (vuId, offset) => {
    expect(() => processor(false).push(canaryLine(vuId!, offset!))).toThrow("generator_output_sequence_invalid");
  });
  it("重複canaryとcanary未完了の正常終了を拒否する", () => {
    expect(() => processor().push(canaryLine(1))).toThrow("generator_output_sequence_invalid");
    expect(() => processor(false).finish()).toThrow("generator_output_sequence_invalid");
  });
});

function metricsBinding(loaded: LoadedManifest): GeneratorMetricsBinding {
  return {
    runId: loaded.manifest.run_id,
    eventId: loaded.manifest.event.id,
    sourceCommit: loaded.manifest.artifacts.source_commit,
    schemaRevision: loaded.manifest.schema_revision,
    manifestSha256: loaded.hashes.manifestSha256,
    scenarioSha256: loaded.hashes.scenarioSha256,
    runtimeManifestSha256: canonicalSha256(
      projectK6RuntimeManifest(loaded.manifest, loaded.hashes),
    ),
  };
}

function rawK6Summary(): unknown {
  const requestCount = 2;
  const metrics: Record<string, unknown> = {};
  for (const series of GENERATOR_METRIC_SERIES) {
    metrics[series.selector] =
      series.kind === "counter"
        ? {
            type: "counter",
            contains: "default",
            values: { count: 0, rate: 0 },
          }
        : {
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
            },
          };
  }
  metrics[
    metricSelector(GENERATOR_METRIC_NAMES.httpRequests, [
      ["route", "app_stylesheet"],
      ["outcome", "success"],
    ])
  ] = counterSummaryMetric(requestCount);
  metrics[
    metricSelector(GENERATOR_METRIC_NAMES.httpDuration, [
      ["route", "app_stylesheet"],
      ["outcome", "success"],
    ])
  ] = trendSummaryMetric(requestCount);
  metrics[GENERATOR_METRIC_NAMES.httpRequests] = counterSummaryMetric(requestCount);
  metrics[GENERATOR_METRIC_NAMES.httpDuration] = trendSummaryMetric(requestCount);
  return {
    metrics,
    options: { summaryTrendStats: [...GENERATOR_SUMMARY_TREND_STATS] },
    state: { testRunDurationMs: 4_800_000 },
  };
}

function counterSummaryMetric(count: number): unknown {
  return {
    type: "counter",
    contains: "default",
    values: { count, rate: count },
  };
}

function trendSummaryMetric(count: number): unknown {
  return {
    type: "trend",
    contains: "time",
    values: {
      count,
      min: 1,
      avg: 1,
      med: 1,
      max: 1,
      "p(50)": 1,
      "p(95)": 1,
      "p(99)": 1,
    },
  };
}

function completeRoundStream(loaded: LoadedManifest): Uint8Array {
  const chunks: Uint8Array[] = [];
  for (let roundNumber = 1; roundNumber <= 10; roundNumber += 1) {
    chunks.push(
      ...buildRoundEvents(loaded.manifest, roundNumber).map((event) =>
        encodeEventLine(event),
      ),
    );
  }
  return concatenate(chunks);
}

function concatenate(chunks: readonly Uint8Array[]): Uint8Array {
  const size = chunks.reduce((total, chunk) => total + chunk.byteLength, 0);
  const result = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return result;
}

function refusalCode(action: () => unknown): string {
  try {
    action();
  } catch (error: unknown) {
    if (error instanceof Error) {
      return error.message;
    }
    throw error;
  }
  throw new Error("expected_refusal");
}

function expectSuccessfulCompletion(
  output: GeneratorOutputStreamProcessor,
  names: readonly string[],
): void {
  expect(names).toEqual(
    Array.from(
      { length: 10 },
      (_, index) => `generator-round-${String(index + 1).padStart(2, "0")}.json`,
    ),
  );
  expect(output.finish()).toEqual([]);
  const finalArtifact = output.takeFinalArtifact();
  expect(finalArtifact?.name).toBe(GENERATOR_METRICS_ARTIFACT_NAME);
  expect(finalArtifact?.body).toEqual(
    validMetricsLine.subarray(encoder.encode(GENERATOR_METRICS_PREFIX).byteLength),
  );
}

describe("GeneratorOutputStreamProcessor", () => {
  it("10 Roundとsummaryを同一chunkから順序どおり完結する", () => {
    const output = processor();
    const completed = output.push(concatenate([allRoundLines, validMetricsLine]));

    expectSuccessfulCompletion(
      output,
      completed.map((artifact) => artifact.name),
    );
  });

  it(
    "10 Roundとsummaryを1-byte chunkへ分割しても完結する",
    () => {
      const output = processor();
      const stream = concatenate([allRoundLines, validMetricsLine]);
      const names: string[] = [];

      for (let index = 0; index < stream.byteLength; index += 1) {
        names.push(
          ...output
            .push(stream.subarray(index, index + 1))
            .map((artifact) => artifact.name),
        );
      }

      expectSuccessfulCompletion(output, names);
    },
    60_000,
  );

  it("10 Round完結前のsummaryを拒否する", () => {
    const output = processor();

    expect(refusalCode(() => output.push(validMetricsLine))).toBe(
      "generator_output_metrics_early",
    );
  });

  it("同一chunk後半を拒否しても前半で完結したRoundをrefusalへ渡す", () => {
    const output = processor();
    const roundOne = concatenate(
      buildRoundEvents(fixtureManifest.manifest, 1).map((event) =>
        encodeEventLine(event),
      ),
    );

    try {
      output.push(concatenate([roundOne, validMetricsLine]));
    } catch (error: unknown) {
      expect(error).toBeInstanceOf(GeneratorOutputRefusal);
      if (!(error instanceof GeneratorOutputRefusal)) {
        throw error;
      }
      expect(error.code).toBe("generator_output_metrics_early");
      expect(error.completedArtifacts.map((artifact) => artifact.name)).toEqual([
        "generator-round-01.json",
      ]);
      return;
    }
    throw new Error("expected_refusal");
  });

  it("別refusal型の不正summaryでも同一chunk前半の全Roundを渡す", () => {
    const output = processor();
    const mismatchedLine = encoder.encode(
      renderGeneratorMetricsSummary(
        rawK6Summary(),
        {
          ...fixtureBinding,
          runId: "ffffffff-ffff-4fff-8fff-ffffffffffff",
        },
        new Date("2026-09-03T02:20:00Z"),
      ),
    );

    try {
      output.push(concatenate([allRoundLines, mismatchedLine]));
    } catch (error: unknown) {
      expect(error).toBeInstanceOf(GeneratorStreamProcessingRefusal);
      if (!(error instanceof GeneratorStreamProcessingRefusal)) {
        throw error;
      }
      expect(error.message).toBe("generator_stream_refused_after_completion");
      expect(error.completedArtifacts).toHaveLength(10);
      expect(error.completedArtifacts[9]?.name).toBe("generator-round-10.json");
      return;
    }
    throw new Error("expected_refusal");
  });

  it("summaryの重複を拒否する", () => {
    const output = processor();
    output.push(allRoundLines);

    expect(
      refusalCode(() =>
        output.push(concatenate([validMetricsLine, validMetricsLine])),
      ),
    ).toBe("generator_output_metrics_duplicate");
  });

  it("summary後のeventを拒否する", () => {
    const output = processor();
    output.push(concatenate([allRoundLines, validMetricsLine]));

    expect(
      refusalCode(() =>
        output.push(encodeEventLine(buildEvent(fixtureManifest.manifest, 10, 1))),
      ),
    ).toBe("generator_output_sequence_invalid");
  });

  it("summaryがない正常EOFではfinal artifactを返さない", () => {
    const output = processor();
    expect(output.push(allRoundLines)).toHaveLength(10);

    expect(output.finish()).toEqual([]);
    expect(output.takeFinalArtifact()).toBeNull();
  });

  it.each([
    [
      "BOM",
      new Uint8Array([0xef, 0xbb, 0xbf, ...encoder.encode("k6 log\n")]),
      "generator_output_bom_forbidden",
    ],
    [
      "NUL",
      encoder.encode("k6\u0000log\n"),
      "generator_output_nul_forbidden",
    ],
    [
      "invalid UTF-8",
      Uint8Array.of(0xc3, 0x28, 0x0a),
      "generator_output_utf8_invalid",
    ],
    [
      "予約prefix",
      encoder.encode("@@LOAD_TEST_UNKNOWN@@{}\n"),
      "generator_output_reserved_prefix",
    ],
  ])("%s lineを固定codeで拒否する", (_name, bytes, code) => {
    const output = processor();

    expect(refusalCode(() => output.push(bytes as Uint8Array))).toBe(code);
    expect(refusalCode(() => output.finish())).toBe("generator_output_failed");
  });

  it("終端LFのないpartial lineを拒否する", () => {
    const output = processor();
    expect(output.push(encoder.encode("unterminated"))).toEqual([]);

    expect(refusalCode(() => output.finish())).toBe(
      "generator_output_eof_partial",
    );
  });

  it("最大長を1 byte超えるlineを拒否する", () => {
    const output = processor();
    const prefixBytes = encoder.encode(GENERATOR_METRICS_PREFIX).byteLength;
    const oversized = new Uint8Array(
      MAX_GENERATOR_METRICS_ARTIFACT_BYTES + prefixBytes + 1,
    ).fill(0x61);

    expect(refusalCode(() => output.push(oversized))).toBe(
      "generator_output_line_too_large",
    );
  });

  it("manifest bindingと一致しないsummaryを拒否する", () => {
    const output = processor();
    output.push(allRoundLines);
    const mismatchedLine = encoder.encode(
      renderGeneratorMetricsSummary(
        rawK6Summary(),
        {
          ...fixtureBinding,
          runId: "ffffffff-ffff-4fff-8fff-ffffffffffff",
        },
        new Date("2026-09-03T02:20:00Z"),
      ),
    );

    expect(refusalCode(() => output.push(mismatchedLine))).toBe(
      "metrics_summary_binding_invalid",
    );
  });
});
