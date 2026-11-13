import { readFileSync } from "node:fs";
import { performance } from "node:perf_hooks";

export const HOST_RESOURCE_LIMIT_PERCENT = 80;
export const HOST_RESOURCE_SUSTAINED_MILLISECONDS = 30_000;
export const HOST_RESOURCE_POLL_MILLISECONDS = 1_000;
export const HOST_SCHEDULER_LAG_LIMIT_MILLISECONDS = 50;

export type HostGuardRefusalCode =
  | "host_guard_clock_invalid"
  | "host_guard_configuration_invalid"
  | "host_guard_proc_invalid"
  | "host_guard_proc_unavailable";

export type HostAbortReason =
  | "host_cpu_limit_sustained"
  | "host_memory_limit_sustained"
  | "host_scheduler_lag_sustained";

export class HostGuardRefusal extends Error {
  readonly code: HostGuardRefusalCode;

  constructor(code: HostGuardRefusalCode) {
    super(code);
    this.name = "HostGuardRefusal";
    this.code = code;
  }
}

export interface HostResourceSample {
  readonly observedAtMilliseconds: number;
  readonly cpuPercent: number | null;
  readonly memoryPercent: number;
  readonly schedulerLagMilliseconds: number | null;
  readonly abortReason: HostAbortReason | null;
}

export interface LinuxHostGuardOptions {
  readonly readText?: (path: string) => string;
  readonly monotonicNow?: () => number;
  readonly thresholdPercent?: number;
  readonly sustainedMilliseconds?: number;
  readonly expectedPollMilliseconds?: number;
  readonly schedulerLagThresholdMilliseconds?: number;
}

interface CpuCounters {
  readonly total: bigint;
  readonly idle: bigint;
}

interface ResourceMeasurement {
  readonly displayPercent: number;
  readonly isOverLimit: boolean;
}

/** Linux /procをmonotonic clockで観測し、CPUとmemoryの連続超過を別々に判定する。 */
export class LinuxHostGuard {
  readonly #readText: (path: string) => string;
  readonly #monotonicNow: () => number;
  readonly #thresholdPercent: number;
  readonly #sustainedMilliseconds: number;
  readonly #expectedPollMilliseconds: number;
  readonly #schedulerLagThresholdMilliseconds: number;

  #previousCpu: CpuCounters | null = null;
  #previousObservedAt: number | null = null;
  #cpuExceededSince: number | null = null;
  #memoryExceededSince: number | null = null;
  #schedulerExceededSince: number | null = null;

  constructor(options: LinuxHostGuardOptions = {}) {
    this.#readText = options.readText ?? readProcText;
    this.#monotonicNow = options.monotonicNow ?? (() => performance.now());
    this.#thresholdPercent = options.thresholdPercent ?? HOST_RESOURCE_LIMIT_PERCENT;
    this.#sustainedMilliseconds =
      options.sustainedMilliseconds ?? HOST_RESOURCE_SUSTAINED_MILLISECONDS;
    this.#expectedPollMilliseconds =
      options.expectedPollMilliseconds ?? HOST_RESOURCE_POLL_MILLISECONDS;
    this.#schedulerLagThresholdMilliseconds =
      options.schedulerLagThresholdMilliseconds ??
      HOST_SCHEDULER_LAG_LIMIT_MILLISECONDS;
    if (
      !Number.isSafeInteger(this.#thresholdPercent) ||
      this.#thresholdPercent < 1 ||
      this.#thresholdPercent >= 100 ||
      !Number.isFinite(this.#sustainedMilliseconds) ||
      this.#sustainedMilliseconds <= 0 ||
      !Number.isFinite(this.#expectedPollMilliseconds) ||
      this.#expectedPollMilliseconds <= 0 ||
      !Number.isSafeInteger(this.#schedulerLagThresholdMilliseconds) ||
      this.#schedulerLagThresholdMilliseconds < 0
    ) {
      throw new HostGuardRefusal("host_guard_configuration_invalid");
    }
  }

  poll(): HostResourceSample {
    const observedAt = this.#monotonicNow();
    if (
      !Number.isFinite(observedAt) ||
      (this.#previousObservedAt !== null && observedAt < this.#previousObservedAt)
    ) {
      throw new HostGuardRefusal("host_guard_clock_invalid");
    }

    let statBody: string;
    let memoryBody: string;
    try {
      statBody = this.#readText("/proc/stat");
      memoryBody = this.#readText("/proc/meminfo");
    } catch {
      throw new HostGuardRefusal("host_guard_proc_unavailable");
    }

    const currentCpu = parseCpuCounters(statBody);
    const memory = measureMemory(memoryBody, this.#thresholdPercent);
    let cpu: ResourceMeasurement | null = null;
    const schedulerLag =
      this.#previousObservedAt === null
        ? null
        : Math.max(
            0,
            observedAt - this.#previousObservedAt - this.#expectedPollMilliseconds,
          );
    if (this.#previousCpu !== null) {
      cpu = measureCpu(this.#previousCpu, currentCpu, this.#thresholdPercent);
    }

    this.#cpuExceededSince = updateExceededSince(
      this.#cpuExceededSince,
      cpu?.isOverLimit ?? false,
      observedAt,
    );
    this.#memoryExceededSince = updateExceededSince(
      this.#memoryExceededSince,
      memory.isOverLimit,
      observedAt,
    );
    this.#schedulerExceededSince = updateExceededSince(
      this.#schedulerExceededSince,
      schedulerLag !== null && schedulerLag > this.#schedulerLagThresholdMilliseconds,
      observedAt,
    );

    const abortReason = this.#abortReason(observedAt);
    this.#previousCpu = currentCpu;
    this.#previousObservedAt = observedAt;
    return {
      observedAtMilliseconds: observedAt,
      cpuPercent: cpu?.displayPercent ?? null,
      memoryPercent: memory.displayPercent,
      schedulerLagMilliseconds: schedulerLag,
      abortReason,
    };
  }

  #abortReason(observedAt: number): HostAbortReason | null {
    if (
      this.#cpuExceededSince !== null &&
      observedAt - this.#cpuExceededSince >= this.#sustainedMilliseconds
    ) {
      return "host_cpu_limit_sustained";
    }
    if (
      this.#memoryExceededSince !== null &&
      observedAt - this.#memoryExceededSince >= this.#sustainedMilliseconds
    ) {
      return "host_memory_limit_sustained";
    }
    if (
      this.#schedulerExceededSince !== null &&
      observedAt - this.#schedulerExceededSince >= this.#sustainedMilliseconds
    ) {
      return "host_scheduler_lag_sustained";
    }
    return null;
  }
}

function readProcText(path: string): string {
  return readFileSync(path, "utf8");
}

function parseCpuCounters(body: string): CpuCounters {
  const cpuLine = body.split("\n").find((line) => line.startsWith("cpu "));
  if (cpuLine === undefined) {
    throw new HostGuardRefusal("host_guard_proc_invalid");
  }
  const fields = cpuLine.trim().split(/\s+/u).slice(1);
  if (fields.length < 8 || fields.some((value) => !/^\d+$/u.test(value))) {
    throw new HostGuardRefusal("host_guard_proc_invalid");
  }
  try {
    // guest/guest_niceはuser/niceへ既に含まれるため二重加算しない。
    const counters = fields.slice(0, 8).map((value) => BigInt(value));
    const total = counters.reduce((sum, value) => sum + value, 0n);
    const idleCounter = counters[3];
    const ioWaitCounter = counters[4];
    if (idleCounter === undefined || ioWaitCounter === undefined) {
      throw new HostGuardRefusal("host_guard_proc_invalid");
    }
    const idle = idleCounter + ioWaitCounter;
    if (total <= 0n || idle > total) {
      throw new HostGuardRefusal("host_guard_proc_invalid");
    }
    return { total, idle };
  } catch (error: unknown) {
    if (error instanceof HostGuardRefusal) {
      throw error;
    }
    throw new HostGuardRefusal("host_guard_proc_invalid");
  }
}

function measureCpu(
  previous: CpuCounters,
  current: CpuCounters,
  thresholdPercent: number,
): ResourceMeasurement {
  const totalDelta = current.total - previous.total;
  const idleDelta = current.idle - previous.idle;
  if (totalDelta <= 0n || idleDelta < 0n || idleDelta > totalDelta) {
    throw new HostGuardRefusal("host_guard_proc_invalid");
  }
  const busyDelta = totalDelta - idleDelta;
  return ratioMeasurement(busyDelta, totalDelta, thresholdPercent);
}

function measureMemory(body: string, thresholdPercent: number): ResourceMeasurement {
  const values = new Map<string, bigint>();
  for (const line of body.split("\n")) {
    const match = /^(MemTotal|MemAvailable):\s+(\d+)\s+kB$/u.exec(line);
    if (match !== null) {
      const name = match[1];
      const rawValue = match[2];
      if (name === undefined || rawValue === undefined || values.has(name)) {
        throw new HostGuardRefusal("host_guard_proc_invalid");
      }
      values.set(name, BigInt(rawValue));
    }
  }
  const total = values.get("MemTotal");
  const available = values.get("MemAvailable");
  if (total === undefined || available === undefined || total <= 0n || available > total) {
    throw new HostGuardRefusal("host_guard_proc_invalid");
  }
  return ratioMeasurement(total - available, total, thresholdPercent);
}

function ratioMeasurement(
  used: bigint,
  total: bigint,
  thresholdPercent: number,
): ResourceMeasurement {
  const scaled = (used * 10_000n) / total;
  return {
    displayPercent: Number(scaled) / 100,
    isOverLimit: used * 100n > BigInt(thresholdPercent) * total,
  };
}

function updateExceededSince(
  current: number | null,
  isOverLimit: boolean,
  observedAt: number,
): number | null {
  if (!isOverLimit) {
    return null;
  }
  return current ?? observedAt;
}
