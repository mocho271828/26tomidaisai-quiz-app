import { EventEmitter } from "node:events";
import {
  chmodSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { PassThrough } from "node:stream";
import type { ChildProcess } from "node:child_process";

import { afterEach, describe, expect, it } from "vitest";

import { sealArtifact } from "../src/artifacts.js";
import { GeneratorStreamProcessingRefusal } from "../src/generator-stream-refusal.js";
import {
  GENERATOR_METRICS_ARTIFACT_NAME,
} from "../src/generator/metrics-summary.js";
import { LauncherRefusal, runK6Launcher } from "../src/launcher.js";
import type {
  CompletedFinalArtifact,
  CompletedRoundArtifact,
  GeneratorStreamProcessor,
  K6LauncherOptions,
  LauncherSignalSource,
} from "../src/launcher.js";
import { validManifestFixture } from "./fixtures/manifest.js";

const REPOSITORY_ROOT = realpathSync(
  join(dirname(fileURLToPath(import.meta.url)), "../../.."),
);
const SCRIPT_PATH = join(REPOSITORY_ROOT, "load-test/k6/src/launcher.ts");
const temporaryRoots: string[] = [];

class FakeChild extends EventEmitter {
  readonly stdout = new PassThrough();
  readonly signals: NodeJS.Signals[] = [];

  kill(signal: NodeJS.Signals = "SIGTERM"): boolean {
    this.signals.push(signal);
    queueMicrotask(() => this.emit("close", null, signal));
    return true;
  }

  asChildProcess(): ChildProcess {
    return this as unknown as ChildProcess;
  }
}

class FakeSignalSource implements LauncherSignalSource {
  on(signal: "SIGINT" | "SIGTERM", listener: () => void): void {
    void signal;
    void listener;
  }
  off(signal: "SIGINT" | "SIGTERM", listener: () => void): void {
    void signal;
    void listener;
  }
}

function temporaryDirectory(): string {
  const directory = realpathSync(mkdtempSync(join(tmpdir(), "k6-metrics-launcher-")));
  chmodSync(directory, 0o700);
  temporaryRoots.push(directory);
  return directory;
}

function manifestFile(): string {
  const directory = temporaryDirectory();
  const path = join(directory, "manifest.json");
  writeFileSync(path, JSON.stringify(validManifestFixture()), { mode: 0o600 });
  return path;
}

function roundArtifacts(): readonly CompletedRoundArtifact[] {
  return Array.from({ length: 10 }, (_, index) => ({
    roundNumber: index + 1,
    name: `generator-round-${String(index + 1).padStart(2, "0")}.json`,
    body: Buffer.from(`{"round_number":${index + 1}}\n`),
  }));
}

function processorWithFinal(
  takeFinalArtifact: () => CompletedFinalArtifact | null,
): GeneratorStreamProcessor {
  let emitted = false;
  return {
    push: () => {
      if (emitted) {
        return [];
      }
      emitted = true;
      return roundArtifacts();
    },
    finish: () => [],
    takeFinalArtifact,
  };
}

function options(
  outputDirectory: string,
  processor: GeneratorStreamProcessor,
): K6LauncherOptions {
  return {
    manifestPath: manifestFile(),
    scriptPath: SCRIPT_PATH,
    outputDirectory,
    createProcessor: () => processor,
    requireMetricsArtifact: true,
  };
}

function finalArtifact(body = Buffer.from('{"format":"metrics"}\n')) {
  return {
    name: GENERATOR_METRICS_ARTIFACT_NAME,
    body,
  } satisfies CompletedFinalArtifact;
}

afterEach(() => {
  for (const directory of temporaryRoots.splice(0)) {
    rmSync(directory, { force: true, recursive: true });
  }
});

describe("k6 launcher metrics artifact", () => {
  it("同一chunk後半の拒否前に完結したRoundをsealする", async () => {
    const output = temporaryDirectory();
    const child = new FakeChild();
    const firstRound = roundArtifacts()[0];
    if (firstRound === undefined) {
      throw new Error("test_fixture_missing");
    }
    const resultPromise = runK6Launcher(
      options(output, {
        push: () => {
          throw new GeneratorStreamProcessingRefusal("stream_refused", [firstRound]);
        },
        finish: () => [],
        takeFinalArtifact: () => null,
      }),
      {
        hostGuard: { poll: () => healthySample() },
        resourcePollMilliseconds: 60_000,
        signalSource: new FakeSignalSource(),
        spawnK6: () => child.asChildProcess(),
      },
    );

    child.stdout.write("round then invalid record");

    await expect(resultPromise).rejects.toEqual(
      expect.objectContaining<Partial<LauncherRefusal>>({
        code: "launcher_stream_invalid",
      }),
    );
    expect(readFileSync(join(output, "generator-round-01.json"))).toEqual(
      firstRound.body,
    );
  });

  it("formal modeでfinal artifact seamがなければspawn前に拒否する", async () => {
    const output = temporaryDirectory();
    let wasSpawned = false;

    await expect(
      runK6Launcher(
        options(output, { push: () => [], finish: () => [] }),
        {
          hostGuard: { poll: () => healthySample() },
          signalSource: new FakeSignalSource(),
          spawnK6: () => {
            wasSpawned = true;
            return new FakeChild().asChildProcess();
          },
        },
      ),
    ).rejects.toEqual(
      expect.objectContaining<Partial<LauncherRefusal>>({
        code: "launcher_stream_invalid",
      }),
    );
    expect(wasSpawned).toBe(false);
  });

  it("正常終了と10 Round完結後にだけmetricsを0600でsealする", async () => {
    const output = temporaryDirectory();
    const child = new FakeChild();
    const resultPromise = runK6Launcher(
      options(output, processorWithFinal(() => finalArtifact())),
      {
        hostGuard: { poll: () => healthySample() },
        resourcePollMilliseconds: 60_000,
        signalSource: new FakeSignalSource(),
        spawnK6: () => child.asChildProcess(),
      },
    );

    child.stdout.write("complete");
    child.emit("close", 0, null);
    const result = await resultPromise;

    expect(result.artifacts).toHaveLength(10);
    expect(result.metricsArtifact?.name).toBe(GENERATOR_METRICS_ARTIFACT_NAME);
    expect(
      statSync(join(output, GENERATOR_METRICS_ARTIFACT_NAME)).mode & 0o777,
    ).toBe(0o600);
  });

  it("metrics欠落を専用codeで拒否し、完結済みRoundだけを残す", async () => {
    const output = temporaryDirectory();
    const child = new FakeChild();
    const resultPromise = runK6Launcher(
      options(output, processorWithFinal(() => null)),
      {
        hostGuard: { poll: () => healthySample() },
        resourcePollMilliseconds: 60_000,
        signalSource: new FakeSignalSource(),
        spawnK6: () => child.asChildProcess(),
      },
    );

    child.stdout.write("complete");
    child.emit("close", 0, null);

    await expect(resultPromise).rejects.toEqual(
      expect.objectContaining<Partial<LauncherRefusal>>({
        code: "launcher_metrics_incomplete",
      }),
    );
    expect(readFileSync(join(output, "generator-round-10.json"))).toBeDefined();
  });

  it("nonzero exitではfinal artifactを取得せずmetricsをsealしない", async () => {
    const output = temporaryDirectory();
    const child = new FakeChild();
    let wasTaken = false;
    const resultPromise = runK6Launcher(
      options(
        output,
        processorWithFinal(() => {
          wasTaken = true;
          return finalArtifact();
        }),
      ),
      {
        hostGuard: { poll: () => healthySample() },
        resourcePollMilliseconds: 60_000,
        signalSource: new FakeSignalSource(),
        spawnK6: () => child.asChildProcess(),
      },
    );

    child.emit("close", 9, null);

    await expect(resultPromise).rejects.toEqual(
      expect.objectContaining<Partial<LauncherRefusal>>({
        code: "launcher_child_exit_failed",
      }),
    );
    expect(wasTaken).toBe(false);
    expect(() => statSync(join(output, GENERATOR_METRICS_ARTIFACT_NAME))).toThrow();
  });

  it("metrics衝突時は既存bytesを保持する", async () => {
    const output = temporaryDirectory();
    const existing = Buffer.from('{"existing":true}\n');
    sealArtifact({
      outputDirectory: output,
      name: GENERATOR_METRICS_ARTIFACT_NAME,
      body: existing,
    });
    const child = new FakeChild();
    const resultPromise = runK6Launcher(
      options(output, processorWithFinal(() => finalArtifact())),
      {
        hostGuard: { poll: () => healthySample() },
        resourcePollMilliseconds: 60_000,
        signalSource: new FakeSignalSource(),
        spawnK6: () => child.asChildProcess(),
      },
    );

    child.stdout.write("complete");
    child.emit("close", 0, null);

    await expect(resultPromise).rejects.toEqual(
      expect.objectContaining<Partial<LauncherRefusal>>({
        code: "launcher_artifact_failed",
      }),
    );
    expect(readFileSync(join(output, GENERATOR_METRICS_ARTIFACT_NAME))).toEqual(
      existing,
    );
  });

  it("unexpected final artifact名を拒否する", async () => {
    const output = temporaryDirectory();
    const child = new FakeChild();
    const resultPromise = runK6Launcher(
      options(
        output,
        processorWithFinal(() => ({
          name: "unexpected.json",
          body: Buffer.from("{}\n"),
        })),
      ),
      {
        hostGuard: { poll: () => healthySample() },
        resourcePollMilliseconds: 60_000,
        signalSource: new FakeSignalSource(),
        spawnK6: () => child.asChildProcess(),
      },
    );

    child.stdout.write("complete");
    child.emit("close", 0, null);

    await expect(resultPromise).rejects.toEqual(
      expect.objectContaining<Partial<LauncherRefusal>>({
        code: "launcher_stream_invalid",
      }),
    );
  });
});

function healthySample() {
  return {
    observedAtMilliseconds: 0,
    cpuPercent: 10,
    memoryPercent: 20,
    schedulerLagMilliseconds: 0,
    abortReason: null,
  } as const;
}
