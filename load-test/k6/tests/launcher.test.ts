import { EventEmitter } from "node:events";
import {
  chmodSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  realpathSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { PassThrough } from "node:stream";
import type { ChildProcess } from "node:child_process";

import { afterEach, describe, expect, it, vi } from "vitest";

import { sealArtifact } from "../src/artifacts.js";
import type { HostResourceSample } from "../src/host-guard.js";
import {
  K6_COMMAND,
  LauncherRefusal,
  buildK6ChildEnvironment,
  runK6Launcher,
} from "../src/launcher.js";
import type {
  CompletedRoundArtifact,
  GeneratorStreamProcessor,
  K6LauncherOptions,
  K6SpawnOptions,
  LauncherSignalSource,
} from "../src/launcher.js";
import { loadManifest } from "../src/manifest.js";
import { validManifestFixture } from "./fixtures/manifest.js";

const REPOSITORY_ROOT = realpathSync(
  join(dirname(fileURLToPath(import.meta.url)), "../../.."),
);
const SCRIPT_PATH = join(REPOSITORY_ROOT, "load-test/k6/src/launcher.ts");
const temporaryRoots: string[] = [];

class FakeChild extends EventEmitter {
  readonly stdout = new PassThrough();
  readonly signals: NodeJS.Signals[] = [];
  autoCloseOnKill = true;

  kill(signal: NodeJS.Signals = "SIGTERM"): boolean {
    this.signals.push(signal);
    if (this.autoCloseOnKill) {
      queueMicrotask(() => this.emit("close", null, signal));
    }
    return true;
  }

  asChildProcess(): ChildProcess {
    return this as unknown as ChildProcess;
  }
}

class FakeSignalSource implements LauncherSignalSource {
  readonly #listeners = new Map<string, Set<() => void>>();

  on(signal: "SIGINT" | "SIGTERM", listener: () => void): void {
    const listeners = this.#listeners.get(signal) ?? new Set<() => void>();
    listeners.add(listener);
    this.#listeners.set(signal, listeners);
  }

  off(signal: "SIGINT" | "SIGTERM", listener: () => void): void {
    this.#listeners.get(signal)?.delete(listener);
  }

  emit(signal: "SIGINT" | "SIGTERM"): void {
    for (const listener of this.#listeners.get(signal) ?? []) {
      listener();
    }
  }
}

function temporaryDirectory(): string {
  const directory = realpathSync(mkdtempSync(join(tmpdir(), "k6-launcher-")));
  chmodSync(directory, 0o700);
  temporaryRoots.push(directory);
  return directory;
}

function manifestFile(): string {
  const directory = temporaryDirectory();
  const path = join(directory, "manifest.json");
  writeFileSync(path, JSON.stringify(validManifestFixture()), { mode: 0o600 });
  chmodSync(path, 0o600);
  return path;
}

function launcherOptions(
  outputDirectory: string,
  manifestPath: string,
  processor: GeneratorStreamProcessor,
): K6LauncherOptions {
  return {
    manifestPath,
    scriptPath: SCRIPT_PATH,
    outputDirectory,
    createProcessor: () => processor,
  };
}

function healthySample(abortReason: HostResourceSample["abortReason"] = null) {
  return {
    observedAtMilliseconds: 0,
    cpuPercent: 10,
    memoryPercent: 20,
    schedulerLagMilliseconds: 0,
    abortReason,
  } satisfies HostResourceSample;
}

function tenArtifacts(): readonly CompletedRoundArtifact[] {
  return Array.from({ length: 10 }, (_, index) => ({
    roundNumber: index + 1,
    name: `generator-round-${String(index + 1).padStart(2, "0")}.json`,
    body: Buffer.from(`{"round_number":${index + 1}}\n`),
  }));
}

function successfulProcessor(): GeneratorStreamProcessor {
  let emitted = false;
  return {
    push: () => {
      if (emitted) {
        return [];
      }
      emitted = true;
      return tenArtifacts();
    },
    finish: () => [],
  };
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
  for (const directory of temporaryRoots.splice(0)) {
    rmSync(directory, { force: true, recursive: true });
  }
});

describe("k6 launcher", () => {
  it("ignores a forged caller root and rejects a script outside the trusted repository", async () => {
    const manifestPath = manifestFile();
    const output = temporaryDirectory();
    const fakeRoot = temporaryDirectory();
    const outsideScript = join(fakeRoot, "generator.js");
    writeFileSync(outsideScript, "export default {};\n", { mode: 0o600 });
    const base = launcherOptions(output, manifestPath, successfulProcessor());
    const forged = {
      ...base,
      scriptPath: outsideScript,
      repositoryRoot: fakeRoot,
    } as K6LauncherOptions & { readonly repositoryRoot: string };
    const spawnK6 = vi.fn(() => new FakeChild().asChildProcess());

    await expect(
      runK6Launcher(forged, {
        hostGuard: { poll: () => healthySample() },
        signalSource: new FakeSignalSource(),
        spawnK6,
      }),
    ).rejects.toEqual(
      expect.objectContaining<Partial<LauncherRefusal>>({
        code: "launcher_script_invalid",
      }),
    );
    expect(spawnK6).not.toHaveBeenCalled();
  });

  it("uses fixed command/argv, shell false, and a fully managed secret-free environment", async () => {
    vi.stubEnv("K6_CLOUD_TOKEN", "never-forward-this-secret");
    vi.stubEnv("K6_CONFIG", "/secret/config.json");
    vi.stubEnv("NODE_OPTIONS", "--require=/secret/hook.js");
    vi.stubEnv("HTTPS_PROXY", "https://secret-proxy.invalid");
    const manifestPath = manifestFile();
    const output = temporaryDirectory();
    const child = new FakeChild();
    const expectedEnvironment = buildK6ChildEnvironment(loadManifest(manifestPath));
    let invocation:
      | {
          readonly command: string;
          readonly arguments_: readonly string[];
          readonly options: K6SpawnOptions;
        }
      | undefined;

    const resultPromise = runK6Launcher(
      launcherOptions(output, manifestPath, successfulProcessor()),
      {
        hostGuard: { poll: () => healthySample() },
        resourcePollMilliseconds: 60_000,
        signalSource: new FakeSignalSource(),
        spawnK6: (command, arguments_, options) => {
          invocation = { command, arguments_, options };
          const replacement = validManifestFixture();
          replacement.public_origin = "https://replacement.aws.internal";
          writeFileSync(manifestPath, JSON.stringify(replacement), { mode: 0o600 });
          return child.asChildProcess();
        },
      },
    );
    child.stdout.write(Buffer.from("events"));
    child.emit("close", 0, null);
    const result = await resultPromise;

    expect(invocation?.command).toBe(K6_COMMAND);
    expect(invocation?.arguments_).toEqual([
      "run",
      "--quiet",
      "--summary-mode=full",
      "--no-color",
      "--no-usage-report",
      "--log-output=stdout",
      "--log-format=raw",
      realpathSync(SCRIPT_PATH),
    ]);
    expect(invocation?.arguments_).not.toContain("--summary-mode=disabled");
    expect(invocation?.options.shell).toBe(false);
    expect(invocation?.options.stdio).toEqual(["ignore", "pipe", "ignore"]);
    expect(invocation?.options.env).toEqual(expectedEnvironment);
    expect(invocation?.options.env["LOAD_TEST_RUNTIME_MANIFEST_JSON"]).toContain(
      "https://quiz.aws.internal",
    );
    expect(invocation?.options.env["LOAD_TEST_RUNTIME_MANIFEST_JSON"]).not.toContain(
      "https://replacement.aws.internal",
    );
    expect(invocation?.options.env["LOAD_TEST_RUNTIME_MANIFEST_JSON"]).not.toContain(
      "通信確認用の架空問題",
    );
    expect(Object.keys(invocation?.options.env ?? {})).not.toContain(
      "LOAD_TEST_MANIFEST_PATH",
    );
    expect(Object.keys(invocation?.options.env ?? {})).not.toContain("K6_CLOUD_TOKEN");
    expect(Object.keys(invocation?.options.env ?? {})).not.toContain("K6_CONFIG");
    expect(Object.keys(invocation?.options.env ?? {})).not.toContain("NODE_OPTIONS");
    expect(Object.keys(invocation?.options.env ?? {})).not.toContain("HTTPS_PROXY");
    expect(result.artifacts).toHaveLength(10);
  });

  it("does not call finish or seal an unfinished round after nonzero child exit", async () => {
    const manifestPath = manifestFile();
    const output = temporaryDirectory();
    const child = new FakeChild();
    const finish = vi.fn(() => []);
    const resultPromise = runK6Launcher(
      launcherOptions(output, manifestPath, { push: () => [], finish }),
      {
        hostGuard: { poll: () => healthySample() },
        resourcePollMilliseconds: 60_000,
        signalSource: new FakeSignalSource(),
        spawnK6: () => child.asChildProcess(),
      },
    );

    child.emit("close", 17, null);

    await expect(resultPromise).rejects.toEqual(
      expect.objectContaining<Partial<LauncherRefusal>>({
        code: "launcher_child_exit_failed",
        message: "launcher_child_exit_failed",
      }),
    );
    expect(finish).not.toHaveBeenCalled();
    expect(readFileNames(output)).toEqual([]);
  });

  it("fails closed when the child itself exits by signal", async () => {
    const manifestPath = manifestFile();
    const output = temporaryDirectory();
    const child = new FakeChild();
    const finish = vi.fn(() => []);
    const resultPromise = runK6Launcher(
      launcherOptions(output, manifestPath, { push: () => [], finish }),
      {
        hostGuard: { poll: () => healthySample() },
        resourcePollMilliseconds: 60_000,
        signalSource: new FakeSignalSource(),
        spawnK6: () => child.asChildProcess(),
      },
    );

    child.emit("close", null, "SIGKILL");

    await expect(resultPromise).rejects.toEqual(
      expect.objectContaining<Partial<LauncherRefusal>>({
        code: "launcher_child_exit_failed",
      }),
    );
    expect(finish).not.toHaveBeenCalled();
    expect(readFileNames(output)).toEqual([]);
  });

  it("rejects exit zero when the processor has not completed all ten rounds", async () => {
    const manifestPath = manifestFile();
    const output = temporaryDirectory();
    const child = new FakeChild();
    const resultPromise = runK6Launcher(
      launcherOptions(output, manifestPath, { push: () => [], finish: () => [] }),
      {
        hostGuard: { poll: () => healthySample() },
        resourcePollMilliseconds: 60_000,
        signalSource: new FakeSignalSource(),
        spawnK6: () => child.asChildProcess(),
      },
    );

    child.emit("close", 0, null);

    await expect(resultPromise).rejects.toEqual(
      expect.objectContaining<Partial<LauncherRefusal>>({
        code: "launcher_round_incomplete",
      }),
    );
    expect(readFileNames(output)).toEqual([]);
  });

  it("kills the child and returns a fixed error on invalid stream input", async () => {
    const manifestPath = manifestFile();
    const output = temporaryDirectory();
    const child = new FakeChild();
    const resultPromise = runK6Launcher(
      launcherOptions(output, manifestPath, {
        push: () => {
          throw new Error("raw-line-with-secret");
        },
        finish: () => [],
      }),
      {
        hostGuard: { poll: () => healthySample() },
        resourcePollMilliseconds: 60_000,
        signalSource: new FakeSignalSource(),
        spawnK6: () => child.asChildProcess(),
      },
    );
    child.stdout.write(Buffer.from("invalid"));

    await expect(resultPromise).rejects.toEqual(
      expect.objectContaining<Partial<LauncherRefusal>>({
        code: "launcher_stream_invalid",
        message: "launcher_stream_invalid",
      }),
    );
    expect(child.signals).toEqual(["SIGTERM"]);
    expect(readFileNames(output)).toEqual([]);
  });

  it("forwards an interruption to the child and fails closed", async () => {
    const manifestPath = manifestFile();
    const output = temporaryDirectory();
    const child = new FakeChild();
    const signals = new FakeSignalSource();
    const resultPromise = runK6Launcher(
      launcherOptions(output, manifestPath, { push: () => [], finish: () => [] }),
      {
        hostGuard: { poll: () => healthySample() },
        resourcePollMilliseconds: 60_000,
        signalSource: signals,
        spawnK6: () => child.asChildProcess(),
      },
    );

    signals.emit("SIGINT");

    await expect(resultPromise).rejects.toEqual(
      expect.objectContaining<Partial<LauncherRefusal>>({ code: "launcher_interrupted" }),
    );
    expect(child.signals).toEqual(["SIGTERM"]);
    expect(readFileNames(output)).toEqual([]);
  });

  it("escalates SIGTERM to SIGKILL and gives up at a bounded hard limit", async () => {
    const manifestPath = manifestFile();
    const output = temporaryDirectory();
    const child = new FakeChild();
    child.autoCloseOnKill = false;
    const signals = new FakeSignalSource();
    const resultPromise = runK6Launcher(
      launcherOptions(output, manifestPath, { push: () => [], finish: () => [] }),
      {
        hostGuard: { poll: () => healthySample() },
        resourcePollMilliseconds: 60_000,
        signalSource: signals,
        spawnK6: () => child.asChildProcess(),
        terminationGraceMilliseconds: 1,
        terminationHardLimitMilliseconds: 1,
      },
    );

    signals.emit("SIGTERM");

    await expect(resultPromise).rejects.toEqual(
      expect.objectContaining<Partial<LauncherRefusal>>({
        code: "launcher_child_termination_failed",
      }),
    );
    expect(child.signals).toEqual(["SIGTERM", "SIGKILL"]);
    expect(readFileNames(output)).toEqual([]);
  });

  it.each([
    ["host_cpu_limit_sustained", "launcher_host_cpu_limit_sustained"],
    ["host_memory_limit_sustained", "launcher_host_memory_limit_sustained"],
    ["host_scheduler_lag_sustained", "launcher_host_scheduler_lag_sustained"],
  ] as const)(
    "aborts on %s and never seals a partial round",
    async (abortReason, expectedCode) => {
      const manifestPath = manifestFile();
      const output = temporaryDirectory();
      const child = new FakeChild();
      let polls = 0;
      const resultPromise = runK6Launcher(
        launcherOptions(output, manifestPath, { push: () => [], finish: () => [] }),
        {
          hostGuard: {
            poll: () => {
              polls += 1;
              return healthySample(polls >= 2 ? abortReason : null);
            },
          },
          resourcePollMilliseconds: 1,
          signalSource: new FakeSignalSource(),
          spawnK6: () => child.asChildProcess(),
        },
      );

      await expect(resultPromise).rejects.toEqual(
        expect.objectContaining<Partial<LauncherRefusal>>({
          code: expectedCode,
        }),
      );
      expect(child.signals).toEqual(["SIGTERM"]);
      expect(readFileNames(output)).toEqual([]);
    },
  );

  it("preserves a colliding artifact and aborts the child", async () => {
    const manifestPath = manifestFile();
    const output = temporaryDirectory();
    const child = new FakeChild();
    const original = Buffer.from('{"existing":true}\n');
    sealArtifact({
      outputDirectory: output,
      name: "generator-round-01.json",
      body: original,
    });
    const resultPromise = runK6Launcher(
      launcherOptions(output, manifestPath, {
        push: () => [tenArtifacts()[0] as CompletedRoundArtifact],
        finish: () => [],
      }),
      {
        hostGuard: { poll: () => healthySample() },
        resourcePollMilliseconds: 60_000,
        signalSource: new FakeSignalSource(),
        spawnK6: () => child.asChildProcess(),
      },
    );
    child.stdout.write(Buffer.from("round-complete"));

    await expect(resultPromise).rejects.toEqual(
      expect.objectContaining<Partial<LauncherRefusal>>({
        code: "launcher_artifact_failed",
      }),
    );
    expect(child.signals).toEqual(["SIGTERM"]);
    expect(readFileSync(join(output, "generator-round-01.json"))).toEqual(original);
  });
});

function readFileNames(directory: string): string[] {
  return readdirSync(directory).sort();
}

describe("host canary watchdog", () => {
  it("stdoutの追加行がなくてもhost pollがramp進捗証拠をsealする", async () => {
    vi.useFakeTimers();
    try {
      const output = temporaryDirectory();
      const child = new FakeChild();
      let sealed = false;
      const processor: GeneratorStreamProcessor = {
        push: () => [], finish: () => tenArtifacts(),
        takeProgressArtifact: () => {
          if (sealed) return null;
          sealed = true;
          return { name: "generator-ramp-ready.json", body: Buffer.from("{}\n") };
        },
      };
      const run = runK6Launcher(launcherOptions(output, manifestFile(), processor), {
        hostGuard: { poll: () => healthySample() }, spawnK6: () => child.asChildProcess(),
        signalSource: new FakeSignalSource(), resourcePollMilliseconds: 1000,
      });
      await vi.advanceTimersByTimeAsync(1000);
      expect(readFileNames(output)).toEqual(["generator-ramp-ready.json"]);
      child.emit("close", 0, null);
      await run;
    } finally { vi.useRealTimers(); }
  });
  it("VUが応答を全く出さない場合もT0+30秒で終了させる", async () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date(validManifestFixture().scheduled_start_at));
      const child = new FakeChild();
      const run = runK6Launcher(
        launcherOptions(temporaryDirectory(), manifestFile(), { push: () => [], finish: () => [], isCanarySatisfied: () => false }),
        { hostGuard: { poll: () => healthySample() }, spawnK6: () => child.asChildProcess(), signalSource: new FakeSignalSource() },
      );
      const outcome = expect(run).rejects.toMatchObject({ code: "launcher_canary_timeout" });
      await vi.advanceTimersByTimeAsync(29_999);
      expect(child.signals).toEqual([]);
      await vi.advanceTimersByTimeAsync(1);
      await outcome;
      expect(child.signals).toEqual(["SIGTERM"]);
    } finally { vi.useRealTimers(); }
  });
});
