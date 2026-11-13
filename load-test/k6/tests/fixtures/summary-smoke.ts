import type { Options } from "k6/options";

import { K6GeneratorMetrics } from "../../src/generator/k6-metrics.js";
import {
  GENERATOR_METRIC_THRESHOLDS,
  GENERATOR_SUMMARY_TREND_STATS,
} from "../../src/generator/metric-taxonomy.js";
import { renderGeneratorMetricsSummary } from "../../src/generator/metrics-summary.js";

const metrics = new K6GeneratorMetrics();

export const options: Options = {
  handleSummaryTimeout: "5s",
  iterations: 1,
  summaryTrendStats: [...GENERATOR_SUMMARY_TREND_STATS],
  thresholds: Object.fromEntries(
    Object.entries(GENERATOR_METRIC_THRESHOLDS).map(([selector, thresholds]) => [
      selector,
      [...thresholds],
    ]),
  ),
  vus: 1,
};

/** 外部通信を行わず、各metric種別の代表値だけを実k6 collectorへ記録する。 */
export default function runSummarySmoke(): void {
  metrics.recordHttp("readiness", "success", 12.5);
  metrics.recordStaticAssetFailure("cold", "index_request_failed");
  metrics.recordWebSocketConnect("initial", "opened");
  metrics.recordWebSocketMessage("state_invalidated");
  metrics.recordWebSocketClose(1_008);
  metrics.recordProtocolRefusal("payload");
}

/** 本番と同じstrict projectionを、再現可能な固定bindingで実行する。 */
export function handleSummary(data: unknown): { readonly stdout: string } {
  return {
    stdout: renderGeneratorMetricsSummary(
      data,
      {
        runId: "11111111-1111-4111-8111-111111111111",
        eventId: "22222222-2222-4222-8222-222222222222",
        sourceCommit: "a".repeat(40),
        schemaRevision: "summary-smoke-v1",
        manifestSha256: "b".repeat(64),
        scenarioSha256: "c".repeat(64),
        runtimeManifestSha256: "d".repeat(64),
      },
      new Date("2026-09-04T00:00:00Z"),
    ),
  };
}
