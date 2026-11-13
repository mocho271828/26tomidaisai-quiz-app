import { distribution } from "./analyze.js";

export type MetricSample = {source: string; event: string; wallMs: number; value: number | null; reason?: string};
export const resourceMetricNames = new Set([
  "generator_cpu_percent", "generator_memory_used_bytes", "generator_swap_used_bytes", "resource_sample_overhead",
  "host_sample_lag_ms", "resource_sample_failed", "generator_node_rss_bytes", "worker_cpu_percent",
  "runner_timer_lag_ms", "generator_node_loop_p95_ms", "generator_node_loop_max_ms",
  "evidence_pending_records", "evidence_dropped_records",
]);
export const connectionMetricNames = new Set(["planned_vus", "simultaneous_ws", "simultaneous_joined", "simultaneous_ready", "worker_status"]);

export function summarizeResourceMetrics(samples: MetricSample[], hostIntervalMs: number | null) {
  const values = (source: string, event: string): number[] => samples
    .filter(sample => sample.source === source && sample.event === event && sample.value !== null)
    .map(sample => sample.value!);
  const hostTicks = samples.filter(sample => sample.source === "controller" && sample.event === "host_sample_lag_ms");
  const sources = [...new Set(samples.map(sample => sample.source))];
  const gaps = hostTicks.flatMap((sample, index) => index === 0 ? [] : [sample.wallMs - hostTicks[index - 1]!.wallMs]);
  return {
    host: {
      intervalMs: hostIntervalMs, sampleCount: hostTicks.length,
      unavailableSamples: samples.filter(sample => sample.event === "resource_sample_failed").length,
      largeIntervalGaps: hostIntervalMs === null ? null : gaps.filter(gap => gap > hostIntervalMs * 2).length,
      observedSampleSpacingMs: distribution(gaps),
      cpuPercent: distribution(values("controller", "generator_cpu_percent")),
      usedMemoryBytes: distribution(values("controller", "generator_memory_used_bytes")),
      swapBytes: distribution(values("controller", "generator_swap_used_bytes")),
      sampleOverheadMs: distribution(values("controller", "resource_sample_overhead")),
      sampleLagMs: distribution(values("controller", "host_sample_lag_ms")),
    },
    workers: sources.map(source => ({source,
      rssBytes: distribution(values(source, "generator_node_rss_bytes")),
      cpuOneCorePercent: distribution(values(source, "worker_cpu_percent")),
      timerLagMs: distribution(values(source, "runner_timer_lag_ms")),
      loopIntervalP95Samples: values(source, "generator_node_loop_p95_ms").length,
      maximumLoopIntervalP95Ms: values(source, "generator_node_loop_p95_ms").reduce<number | null>((max, value) => Math.max(max ?? value, value), null),
      maximumLoopDelayMs: values(source, "generator_node_loop_max_ms").reduce<number | null>((max, value) => Math.max(max ?? value, value), null),
    })),
    interpretation: "CPU/RSS/loopはprocess別。loopはinterval p95の最大値であり全標本p95ではない。最小計測のhistogram未採取は0扱いしない。",
  };
}

export function metricCsv(samples: MetricSample[]): string {
  return "source,event,wall_ms,value,reason\n" + samples.map(sample =>
    [sample.source, sample.event, sample.wallMs, sample.value, sample.reason ?? ""].map(csvCell).join(",")).join("\n") + "\n";
}

export function csvCell(value: string | number | null): string {
  if (value === null) return "";
  return typeof value === "number" ? String(value) : `"${value.replaceAll('"', '""')}"`;
}
