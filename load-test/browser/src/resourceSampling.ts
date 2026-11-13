import { execFile } from "node:child_process";
import { readFile } from "node:fs/promises";
import { cpus, freemem, platform, totalmem } from "node:os";
import { monitorEventLoopDelay } from "node:perf_hooks";
import { promisify } from "node:util";
import { z } from "zod";
import type { Evidence } from "./evidence.js";

const execute = promisify(execFile);
const samplingOptionsSchema = z.object({
  intervalMs: z.number().int().min(100).max(60000).default(3000),
  measurementLevel: z.enum(["minimal", "normal"]).default("normal"),
}).strict();
export type SamplingOptions = z.input<typeof samplingOptionsSchema>;

export function startEventLoopSampling(evidence: Evidence, options: SamplingOptions = {}): () => void {
  const {intervalMs, measurementLevel} = samplingOptionsSchema.parse(options);
  const histogram = measurementLevel === "normal" ? monitorEventLoopDelay({resolution: 20}) : undefined;
  histogram?.enable();
  let expectedAt = performance.now() + intervalMs;
  const sample = (): void => {
    if (histogram !== undefined && histogram.count > 0) {
      evidence.event({event: "generator_node_loop_p95_ms", durationMs: histogram.percentile(95) / 1e6, scope: "worker"});
      evidence.event({event: "generator_node_loop_max_ms", durationMs: histogram.max / 1e6, scope: "worker"});
      histogram.reset();
    }
  };
  const timer = setInterval(() => {
    evidence.event({event: "runner_timer_lag_ms", durationMs: Math.max(0, performance.now() - expectedAt), scope: "worker"});
    expectedAt = performance.now() + intervalMs;
    sample();
  }, intervalMs);
  timer.unref();
  let stopped = false;
  return () => {
    if (stopped) return;
    stopped = true; clearInterval(timer); histogram?.disable(); sample();
  };
}

async function swapBytes(): Promise<number> {
  if (platform() === "linux") {
    const mem = await readFile("/proc/meminfo", "utf8");
    const total = /SwapTotal:\s+(\d+)/u.exec(mem)?.[1];
    const free = /SwapFree:\s+(\d+)/u.exec(mem)?.[1];
    if (total !== undefined && free !== undefined) return (Number(total) - Number(free)) * 1024;
  }
  if (platform() === "darwin") {
    // timeoutと出力上限を設け、停止処理をOSコマンドの無期限待ちにしない。
    const { stdout } = await execute("/usr/sbin/sysctl", ["-n", "vm.swapusage"], {timeout: 3000, maxBuffer: 4096});
    const match = /used = ([\d.]+)([MG])/u.exec(stdout);
    if (match) return Number(match[1]) * (match[2] === "G" ? 1024 ** 3 : 1024 ** 2);
  }
  throw new Error("swap_measurement_unavailable");
}

function cpuSnapshot(): { idle: number; total: number } {
  return cpus().reduce((sum, cpu) => ({ idle: sum.idle + cpu.times.idle,
    total: sum.total + Object.values(cpu.times).reduce((a, b) => a + b, 0) }), {idle: 0, total: 0});
}

/** 同じPCではsupervisor一箇所だけで起動する。workerの負荷は別に記録する。 */
export function startResourceSampling(evidence: Evidence, options: SamplingOptions = {}): () => Promise<void> {
  const {intervalMs} = samplingOptionsSchema.parse(options);
  let prior: ReturnType<typeof cpuSnapshot> | undefined;
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let pending: Promise<void> = Promise.resolve();
  let expectedAt = performance.now();
  const sample = async (): Promise<void> => {
    const started = performance.now();
    const next = cpuSnapshot();
    const cpuPercent = prior === undefined ? null :
      100 * (1 - (next.idle - prior.idle) / Math.max(1, next.total - prior.total));
    prior = next;
    evidence.event({event: "host_sample_lag_ms", durationMs: Math.max(0, started - expectedAt), scope: "host"});
    try {
      // 初回は比較区間がないため、snapshot差を実負荷100%と誤記録しない。
      if (cpuPercent !== null) evidence.event({event: "generator_cpu_percent", durationMs: cpuPercent, scope: "host"});
      evidence.event({event: "generator_memory_used_bytes", count: totalmem() - freemem(), scope: "host"});
      evidence.event({event: "generator_swap_used_bytes", count: await swapBytes(), scope: "host"});
    } catch {
      evidence.event({event: "resource_sample_failed", reason: "host_sample_unavailable", scope: "host"});
    } finally {
      evidence.event({event: "resource_sample_overhead", durationMs: performance.now() - started, scope: "host"});
    }
    if (!stopped) {
      expectedAt = performance.now() + intervalMs;
      timer = setTimeout(() => { pending = sample(); }, intervalMs);
      timer.unref();
    }
  };
  pending = sample();
  return async () => { stopped = true; clearTimeout(timer); await pending; };
}

export function startWorkerSampling(evidence: Evidence, options: SamplingOptions = {}): () => void {
  const {intervalMs} = samplingOptionsSchema.parse(options);
  const stopLoop = startEventLoopSampling(evidence, options);
  let lastAt = performance.now();
  let lastCpu = process.cpuUsage();
  const sample = (): void => {
    const now = performance.now();
    const cpu = process.cpuUsage();
    const elapsed = now - lastAt;
    evidence.event({event: "generator_node_rss_bytes", count: process.memoryUsage().rss, scope: "worker"});
    // NodeのCPU率は1core=100%。host全core比のgenerator_cpu_percentと合算しない。
    if (elapsed > 0) evidence.event({event: "worker_cpu_percent", scope: "worker",
      durationMs: (cpu.user + cpu.system - lastCpu.user - lastCpu.system) / (elapsed * 10)});
    const health = evidence.health();
    evidence.event({event: "evidence_pending_records", count: health.pendingRecords, scope: "worker"});
    evidence.event({event: "evidence_dropped_records", count: health.droppedRecords, scope: "worker"});
    lastAt = now; lastCpu = cpu;
  };
  const timer = setInterval(sample, intervalMs);
  timer.unref();
  let stopped = false;
  return () => {
    if (stopped) return;
    stopped = true; clearInterval(timer); stopLoop(); sample();
  };
}
