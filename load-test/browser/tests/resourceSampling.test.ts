import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { monitorEventLoopDelay } from "node:perf_hooks";
import { afterEach, expect, it, vi } from "vitest";
import { configSchema } from "../src/config.js";
import { Evidence, startWorkerSampling } from "../src/evidence.js";

vi.mock("node:perf_hooks", () => ({monitorEventLoopDelay: vi.fn(() => ({
  enable: vi.fn(), disable: vi.fn(), reset: vi.fn(), count: 1, max: 40e6,
  percentile: vi.fn(() => 20e6),
}))}));
afterEach(() => { vi.useRealTimers(); vi.clearAllMocks(); });

it.each(["minimal", "normal"] as const)("keeps %s worker sampling separate from host probes", async measurementLevel => {
  const directory = await mkdtemp(join(tmpdir(), "quiz-worker-sampling-"));
  const config = configSchema.parse({runId: "sampling", sourceCommit: "0".repeat(40),
    baseUrl: "https://127.0.0.1", count: 1, ordinalStart: 1, startAt: new Date().toISOString(),
    outputDirectory: directory, channel: "chromium", headless: true,
    durationSeconds: 180, joinSpreadMs: 0, operatorStartDelaySeconds: 10, preScheduleMarginMs: 1000,
    resultHoldMs: 3000, answerDelayMs: 0, scenario: "normal", faultAfterSeconds: 5, faultDurationMs: 100});
  vi.useFakeTimers();
  const evidence = new Evidence(config, "test", {measurementLevel});
  const stop = startWorkerSampling(evidence, {measurementLevel});
  try {
    await vi.advanceTimersByTimeAsync(2999);
    expect(evidence.count("generator_node_rss_bytes")).toBe(0);
    await vi.advanceTimersByTimeAsync(1);
    expect(evidence.count("generator_node_rss_bytes")).toBe(1);
    expect(evidence.count("worker_cpu_percent")).toBe(1);
    expect(evidence.count("runner_timer_lag_ms")).toBe(1);
    expect(evidence.count("generator_cpu_percent")).toBe(0);
    expect(evidence.count("generator_swap_used_bytes")).toBe(0);
    expect(monitorEventLoopDelay).toHaveBeenCalledTimes(measurementLevel === "normal" ? 1 : 0);
    expect(evidence.count("generator_node_loop_p95_ms")).toBe(measurementLevel === "normal" ? 1 : 0);
    stop(); stop();
    expect(evidence.count("generator_node_rss_bytes")).toBe(2);
  } finally {
    stop();
    await evidence.close();
    expect(vi.getTimerCount()).toBe(0);
    vi.useRealTimers();
    await rm(directory, {recursive: true});
  }
});
