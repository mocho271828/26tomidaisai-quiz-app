import { describe, expect, it } from "vitest";

import {
  HostGuardRefusal,
  LinuxHostGuard,
} from "../src/host-guard.js";

interface SampleSpec {
  readonly at: number;
  readonly cpuBusyIncrement: number;
  readonly memoryUsedPercent: number;
}

function guardFor(samples: readonly SampleSpec[]): LinuxHostGuard {
  let index = 0;
  let busy = 0;
  let idle = 100;
  const cpuBodies = samples.map((sample, sampleIndex) => {
    if (sampleIndex > 0) {
      busy += sample.cpuBusyIncrement;
      idle += 100 - sample.cpuBusyIncrement;
    }
    return `cpu ${busy} 0 0 ${idle} 0 0 0 0\n`;
  });
  const memoryBodies = samples.map(
    (sample) =>
      `MemTotal:       100 kB\nMemAvailable:   ${100 - sample.memoryUsedPercent} kB\n`,
  );
  return new LinuxHostGuard({
    schedulerLagThresholdMilliseconds: Number.MAX_SAFE_INTEGER,
    monotonicNow: () => samples[index]?.at ?? Number.NaN,
    readText: (path) => {
      if (path === "/proc/stat") {
        return cpuBodies[index] ?? "";
      }
      if (path === "/proc/meminfo") {
        const body = memoryBodies[index] ?? "";
        index += 1;
        return body;
      }
      throw new Error("unexpected path");
    },
  });
}

describe("LinuxHostGuard", () => {
  it("allows exactly 80 percent and resets both sustained windows", () => {
    const guard = guardFor([
      { at: 0, cpuBusyIncrement: 0, memoryUsedPercent: 81 },
      { at: 10_000, cpuBusyIncrement: 81, memoryUsedPercent: 80 },
      { at: 40_000, cpuBusyIncrement: 80, memoryUsedPercent: 81 },
      { at: 69_999, cpuBusyIncrement: 81, memoryUsedPercent: 81 },
    ]);

    expect(guard.poll().abortReason).toBeNull();
    const exactBoundary = guard.poll();
    expect(exactBoundary.cpuPercent).toBe(81);
    expect(exactBoundary.memoryPercent).toBe(80);
    expect(exactBoundary.abortReason).toBeNull();
    expect(guard.poll().abortReason).toBeNull();
    expect(guard.poll().abortReason).toBeNull();
  });

  it("aborts CPU at exactly 30 seconds of continuous over-80 use", () => {
    const guard = guardFor([
      { at: 0, cpuBusyIncrement: 0, memoryUsedPercent: 0 },
      { at: 1_000, cpuBusyIncrement: 81, memoryUsedPercent: 0 },
      { at: 30_999, cpuBusyIncrement: 81, memoryUsedPercent: 0 },
      { at: 31_000, cpuBusyIncrement: 81, memoryUsedPercent: 0 },
    ]);

    expect(guard.poll().abortReason).toBeNull();
    expect(guard.poll().abortReason).toBeNull();
    expect(guard.poll().abortReason).toBeNull();
    expect(guard.poll().abortReason).toBe("host_cpu_limit_sustained");
  });

  it("aborts memory at exactly 30 seconds of continuous over-80 use", () => {
    const guard = guardFor([
      { at: 0, cpuBusyIncrement: 0, memoryUsedPercent: 81 },
      { at: 29_999, cpuBusyIncrement: 0, memoryUsedPercent: 81 },
      { at: 30_000, cpuBusyIncrement: 0, memoryUsedPercent: 81 },
    ]);

    expect(guard.poll().abortReason).toBeNull();
    expect(guard.poll().abortReason).toBeNull();
    expect(guard.poll().abortReason).toBe("host_memory_limit_sustained");
  });

  it("does not combine a memory streak with a later CPU streak", () => {
    const guard = guardFor([
      { at: 0, cpuBusyIncrement: 0, memoryUsedPercent: 81 },
      { at: 20_000, cpuBusyIncrement: 80, memoryUsedPercent: 81 },
      { at: 20_001, cpuBusyIncrement: 81, memoryUsedPercent: 80 },
      { at: 50_000, cpuBusyIncrement: 81, memoryUsedPercent: 80 },
    ]);

    expect(guard.poll().abortReason).toBeNull();
    expect(guard.poll().abortReason).toBeNull();
    expect(guard.poll().abortReason).toBeNull();
    expect(guard.poll().abortReason).toBeNull();
  });

  it("aborts scheduler lag only after over-50ms remains continuous for 30 seconds", () => {
    let index = 0;
    const observed = Array.from({ length: 31 }, (_, sampleIndex) => sampleIndex * 1_051);
    const guard = new LinuxHostGuard({
      monotonicNow: () => observed[index] ?? Number.NaN,
      readText: (path) => {
        if (path === "/proc/stat") {
          return `cpu ${index} 0 0 ${100 + index * 100} 0 0 0 0\n`;
        }
        if (path === "/proc/meminfo") {
          index += 1;
          return "MemTotal: 100 kB\nMemAvailable: 100 kB\n";
        }
        throw new Error("unexpected path");
      },
    });

    for (let sampleIndex = 0; sampleIndex < observed.length - 1; sampleIndex += 1) {
      const sample = guard.poll();
      expect(sample.abortReason).toBeNull();
      expect(sample.schedulerLagMilliseconds).toBe(sampleIndex === 0 ? null : 51);
    }
    expect(guard.poll().abortReason).toBe("host_scheduler_lag_sustained");
  });

  it("fails closed on missing proc files, malformed counters, and clock reversal", () => {
    const missing = new LinuxHostGuard({
      readText: () => {
        throw new Error("secret path detail");
      },
      monotonicNow: () => 0,
    });
    expect(() => missing.poll()).toThrowError(
      expect.objectContaining<Partial<HostGuardRefusal>>({
        code: "host_guard_proc_unavailable",
        message: "host_guard_proc_unavailable",
      }),
    );

    const malformed = new LinuxHostGuard({
      readText: (path) =>
        path === "/proc/stat"
          ? "cpu 1 2 invalid 4 5 6 7 8\n"
          : "MemTotal: 100 kB\nMemAvailable: 20 kB\n",
      monotonicNow: () => 0,
    });
    expect(() => malformed.poll()).toThrowError(
      expect.objectContaining<Partial<HostGuardRefusal>>({
        code: "host_guard_proc_invalid",
      }),
    );

    let now = 1;
    const reversed = new LinuxHostGuard({
      readText: (path) =>
        path === "/proc/stat"
          ? "cpu 0 0 0 100 0 0 0 0\n"
          : "MemTotal: 100 kB\nMemAvailable: 100 kB\n",
      monotonicNow: () => now,
    });
    reversed.poll();
    now = 0;
    expect(() => reversed.poll()).toThrowError(
      expect.objectContaining<Partial<HostGuardRefusal>>({
        code: "host_guard_clock_invalid",
      }),
    );
  });
});
