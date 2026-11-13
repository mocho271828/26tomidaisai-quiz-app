import type { ChildProcess } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmodSync,
  mkdtempSync,
  realpathSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import { PreparationRefusal } from "../src/preparation.js";
import { buildK6ChildEnvironment } from "../src/k6-child-environment.js";
import {
  APPROVED_FORMAL_K6_IMAGE,
  DOCKER_COMMAND,
} from "../src/formal-docker.js";
import type { GeneratorBundle } from "../src/formal-docker.js";
import {
  FormalRuntimeRefusal,
  formalGeneratorScriptPath,
  runK6GeneratorRuntimeWithDependencies,
} from "../src/formal-runtime.js";
import type {
  FormalContainerCustodianPort,
  FormalRuntimeDependencies,
} from "../src/formal-runtime.js";
import { GeneratorOutputStreamProcessor } from "../src/generator-output-stream-processor.js";
import {
  K6_COMMAND,
  K6_FIXED_RUN_ARGUMENTS,
  LauncherRefusal,
} from "../src/launcher.js";
import type {
  K6LauncherOptions,
  K6LauncherResult,
  K6SpawnOptions,
  LauncherDependencies,
  LauncherSignalSource,
} from "../src/launcher.js";
import { loadManifest, runtimeRepositoryRoot } from "../src/manifest.js";
import { validManifestFixture } from "./fixtures/manifest.js";

const SCHEDULED_START_AT = "2030-01-01T00:00:10Z";
const SCHEDULED_START_MILLISECONDS = Date.parse(SCHEDULED_START_AT);
const BUNDLE_BODY = Buffer.from("export default function () {}\n");
const BUNDLE_SHA256 = createHash("sha256").update(BUNDLE_BODY).digest("hex");
const roots: string[] = [];

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

interface Harness {
  readonly dependencies: FormalRuntimeDependencies;
  readonly manifestPath: string;
  readonly preparationEvidencePath: string;
  readonly applicationCaFile: string;
  readonly outputDirectory: string;
  readonly signalSource: FakeSignalSource;
  readonly sealArm: ReturnType<typeof vi.fn>;
  readonly runLauncher: ReturnType<typeof vi.fn>;
  readonly spawnDocker: ReturnType<typeof vi.fn>;
  readonly assertAbsent: ReturnType<typeof vi.fn>;
  readonly cleanupAndAssertAbsent: ReturnType<typeof vi.fn>;
  readonly clock: { wall: number; monotonic: number };
}

function temporaryDirectory(prefix: string): string {
  const directory = realpathSync(mkdtempSync(join(tmpdir(), prefix)));
  chmodSync(directory, 0o700);
  roots.push(directory);
  return directory;
}

function manifestFile(
  mutate: (manifest: ReturnType<typeof validManifestFixture>) => void = () => {},
): string {
  const directory = temporaryDirectory("formal-runtime-manifest-");
  const manifest = validManifestFixture();
  manifest.scheduled_start_at = SCHEDULED_START_AT;
  manifest.artifacts.generator_image = APPROVED_FORMAL_K6_IMAGE;
  mutate(manifest);
  const path = join(directory, "manifest.json");
  writeFileSync(path, JSON.stringify(manifest), { mode: 0o600 });
  chmodSync(path, 0o600);
  return path;
}

function artifact(name: string, index: number) {
  return {
    name,
    sha256: index.toString(16).padStart(64, "0"),
    size_bytes: 100 + index,
  };
}

function successfulLauncherResult(options: K6LauncherOptions): K6LauncherResult {
  const loaded = loadManifest(options.manifestPath);
  return {
    artifacts: Array.from({ length: 10 }, (_, index) =>
      artifact(
        `generator-round-${String(index + 1).padStart(2, "0")}.json`,
        index + 1,
      ),
    ),
    metricsArtifact: artifact("generator-metrics-summary.json", 11),
    manifestSha256: loaded.hashes.manifestSha256,
    scenarioSha256: loaded.hashes.scenarioSha256,
  };
}

function harness(
  options: {
    readonly initialWall?: number;
    readonly manifestPath?: string;
    readonly sleep?: (
      milliseconds: number,
      signal: AbortSignal,
      clock: Harness["clock"],
      signalSource: FakeSignalSource,
    ) => Promise<void>;
    readonly launcherFailure?: Error;
  } = {},
): Harness {
  const manifestPath = options.manifestPath ?? manifestFile();
  const outputDirectory = temporaryDirectory("formal-runtime-output-");
  const signalSource = new FakeSignalSource();
  const clock = {
    wall: options.initialWall ?? SCHEDULED_START_MILLISECONDS - 5_000,
    monotonic: 50_000,
  };
  const assertAbsent = vi.fn();
  const cleanupAndAssertAbsent = vi.fn();
  const custodian: FormalContainerCustodianPort = {
    assertAbsent,
    cleanupAndAssertAbsent,
  };
  const bundle: GeneratorBundle = {
    body: BUNDLE_BODY,
    sha256: BUNDLE_SHA256,
    sizeBytes: BUNDLE_BODY.byteLength,
  };
  const sealArm = vi.fn(() => ({
    marker: artifact("arm.json", 20),
    bundle: {
      name: "generator.js",
      sha256: BUNDLE_SHA256,
      size_bytes: BUNDLE_BODY.byteLength,
    },
    bundlePath:
      "/run/user/1000/gs-loadtest-k6/11111111-1111-4111-8111-111111111111.generator.js",
  }));
  const spawnDocker = vi.fn(() => ({}) as ChildProcess);
  const runLauncher = vi.fn(
    async (
      launcherOptions: K6LauncherOptions,
      launcherDependencies?: LauncherDependencies,
    ): Promise<K6LauncherResult> => {
      expect(launcherOptions.scriptPath).toBe(formalGeneratorScriptPath());
      expect(launcherOptions.expectedManifestSha256).toBe(
        loadManifest(manifestPath).hashes.manifestSha256,
      );
      expect(launcherOptions.executionHardLimitMilliseconds).toBe(4_831_000);
      expect(launcherOptions.requireMetricsArtifact).toBe(true);
      expect(
        launcherOptions.createProcessor(loadManifest(manifestPath)),
      ).toBeInstanceOf(GeneratorOutputStreamProcessor);
      const spawnK6 = launcherDependencies?.spawnK6;
      if (spawnK6 === undefined) {
        throw new Error("missing test spawn");
      }
      const loaded = loadManifest(manifestPath);
      const k6Options: K6SpawnOptions = {
        cwd: runtimeRepositoryRoot(),
        env: buildK6ChildEnvironment(loaded),
        shell: false,
        stdio: ["ignore", "pipe", "ignore"],
        windowsHide: true,
      };
      spawnK6(
        K6_COMMAND,
        [...K6_FIXED_RUN_ARGUMENTS, launcherOptions.scriptPath],
        k6Options,
      );
      if (options.launcherFailure !== undefined) {
        throw options.launcherFailure;
      }
      return successfulLauncherResult(launcherOptions);
    },
  );
  const sleep =
    options.sleep ??
    (async (milliseconds: number): Promise<void> => {
      clock.wall += milliseconds;
      clock.monotonic += milliseconds;
    });
  const dependencies: FormalRuntimeDependencies = {
    platform: "linux",
    nowMilliseconds: () => clock.wall,
    monotonicMilliseconds: () => clock.monotonic,
    sleep: (milliseconds, signal) =>
      sleep(milliseconds, signal, clock, signalSource),
    signalSource,
    effectiveUserId: () => 1000,
    effectiveGroupId: () => 1000,
    requireDockerConfig: vi.fn(),
    runtimeBaseDirectory: () => "/run/user/1000",
    readBundle: () => bundle,
    sealArm,
    createCustodian: () => custodian,
    validateBundleSnapshot: vi.fn(),
    spawnDocker,
    runLauncher,
  };
  return {
    dependencies,
    manifestPath, preparationEvidencePath: "/secure/preparation.json", applicationCaFile: "/secure/ca.pem",
    outputDirectory,
    signalSource,
    sealArm,
    runLauncher,
    spawnDocker,
    assertAbsent,
    cleanupAndAssertAbsent,
    clock,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  for (const root of roots.splice(0)) {
    rmSync(root, { force: true, recursive: true });
  }
});

describe("scheduled formal generator runtime", () => {
  it("arms, waits to the shared tolerance boundary, and runs exact Docker argv/env", async () => {
    const test = harness();
    process.env["FORMAL_RUNTIME_SECRET"] = "must-not-leak";

    const result = await runK6GeneratorRuntimeWithDependencies(
      {
        manifestPath: test.manifestPath, preparationEvidencePath: "/secure/preparation.json", applicationCaFile: "/secure/ca.pem",
        outputDirectory: test.outputDirectory,
      },
      test.dependencies,
    );

    expect(test.sealArm).toHaveBeenCalledTimes(1);
    expect(test.assertAbsent).toHaveBeenCalledTimes(2);
    expect(test.cleanupAndAssertAbsent).toHaveBeenCalledTimes(1);
    expect(test.spawnDocker).toHaveBeenCalledTimes(1);
    const [command, arguments_, spawnOptions] = test.spawnDocker.mock.calls[0] ?? [];
    expect(command).toBe(DOCKER_COMMAND);
    expect(arguments_).toContain("--pull=never");
    expect(arguments_).toContain(APPROVED_FORMAL_K6_IMAGE);
    expect(arguments_).toContain("--user=1000:1000");
    expect(spawnOptions?.env).not.toHaveProperty("FORMAL_RUNTIME_SECRET");
    expect(Object.keys(spawnOptions?.env ?? {}).sort()).toEqual([
      "LANG",
      "LC_ALL",
      "LOAD_TEST_RUNTIME_MANIFEST_JSON",
      "LOAD_TEST_RUNTIME_MANIFEST_SHA256",
      "PATH",
      "TZ",
    ]);
    expect(result.actualSpawnAt).toBe("2030-01-01T00:00:09Z");
    expect(result.generatorImage).toBe(APPROVED_FORMAL_K6_IMAGE);
    expect(result.generatorBundle.sha256).toBe(BUNDLE_SHA256);
    expect(result.metricsArtifact.name).toBe("generator-metrics-summary.json");
    delete process.env["FORMAL_RUNTIME_SECRET"];
  });

  it("refuses an invocation that was not armed before the scheduled time", async () => {
    const test = harness({ initialWall: SCHEDULED_START_MILLISECONDS });

    await expect(
      runK6GeneratorRuntimeWithDependencies(
        {
          manifestPath: test.manifestPath, preparationEvidencePath: "/secure/preparation.json", applicationCaFile: "/secure/ca.pem",
          outputDirectory: test.outputDirectory,
        },
        test.dependencies,
      ),
    ).rejects.toEqual(
      expect.objectContaining<Partial<FormalRuntimeRefusal>>({
        code: "generator_start_not_armed",
      }),
    );
    expect(test.sealArm).not.toHaveBeenCalled();
    expect(test.runLauncher).not.toHaveBeenCalled();
  });

  it("refuses a timer wake after the shared tolerance before child spawn", async () => {
    const test = harness({
      sleep: async (milliseconds, _signal, clock) => {
        clock.wall += milliseconds + 2_001;
        clock.monotonic += milliseconds + 2_001;
      },
    });

    await expect(
      runK6GeneratorRuntimeWithDependencies(
        {
          manifestPath: test.manifestPath, preparationEvidencePath: "/secure/preparation.json", applicationCaFile: "/secure/ca.pem",
          outputDirectory: test.outputDirectory,
        },
        test.dependencies,
      ),
    ).rejects.toEqual(
      expect.objectContaining<Partial<LauncherRefusal>>({
        code: "launcher_start_late",
      }),
    );
    expect(test.runLauncher).not.toHaveBeenCalled();
    expect(test.spawnDocker).not.toHaveBeenCalled();
  });

  it("fails closed when wall clock freezes during a positive wait", async () => {
    const test = harness({
      sleep: async (milliseconds, _signal, clock) => {
        clock.monotonic += milliseconds;
      },
    });

    await expect(
      runK6GeneratorRuntimeWithDependencies(
        {
          manifestPath: test.manifestPath, preparationEvidencePath: "/secure/preparation.json", applicationCaFile: "/secure/ca.pem",
          outputDirectory: test.outputDirectory,
        },
        test.dependencies,
      ),
    ).rejects.toEqual(
      expect.objectContaining<Partial<FormalRuntimeRefusal>>({
        code: "generator_clock_invalid",
      }),
    );
    expect(test.spawnDocker).not.toHaveBeenCalled();
  });

  it("cancels a scheduled wait on SIGTERM without spawning", async () => {
    const test = harness({
      sleep: async (_milliseconds, _signal, _clock, signalSource) => {
        signalSource.emit("SIGTERM");
        throw new Error("redacted sleep error");
      },
    });

    await expect(
      runK6GeneratorRuntimeWithDependencies(
        {
          manifestPath: test.manifestPath, preparationEvidencePath: "/secure/preparation.json", applicationCaFile: "/secure/ca.pem",
          outputDirectory: test.outputDirectory,
        },
        test.dependencies,
      ),
    ).rejects.toEqual(
      expect.objectContaining<Partial<FormalRuntimeRefusal>>({
        code: "generator_interrupted",
      }),
    );
    expect(test.runLauncher).not.toHaveBeenCalled();
    expect(test.spawnDocker).not.toHaveBeenCalled();
  });

  it("runs cleanup after a child failure and returns only the fixed failure", async () => {
    const childFailure = new LauncherRefusal("launcher_child_exit_failed");
    const test = harness({ launcherFailure: childFailure });

    await expect(
      runK6GeneratorRuntimeWithDependencies(
        {
          manifestPath: test.manifestPath, preparationEvidencePath: "/secure/preparation.json", applicationCaFile: "/secure/ca.pem",
          outputDirectory: test.outputDirectory,
        },
        test.dependencies,
      ),
    ).rejects.toBe(childFailure);
    expect(test.cleanupAndAssertAbsent).toHaveBeenCalledTimes(1);
  });

  it("準備証跡拒否時はarmやDockerより前で停止する", async () => {
    const test = harness();
    await expect(runK6GeneratorRuntimeWithDependencies({
      manifestPath: test.manifestPath, outputDirectory: test.outputDirectory,
      preparationEvidencePath: test.preparationEvidencePath, applicationCaFile: test.applicationCaFile,
    }, { ...test.dependencies, readPreparation: () => { throw new PreparationRefusal(); } })).rejects.toThrow("generator_preparation_invalid");
    expect(test.sealArm).not.toHaveBeenCalled();
    expect(test.spawnDocker).not.toHaveBeenCalled();
  });

  it("rejects an unapproved manifest image before arm or Docker", async () => {
    const path = manifestFile((manifest) => {
      manifest.artifacts.generator_image =
        `registry.aws.internal/generator@sha256:${"f".repeat(64)}`;
    });
    const test = harness({ manifestPath: path });

    await expect(
      runK6GeneratorRuntimeWithDependencies(
        { manifestPath: path, preparationEvidencePath: "/secure/preparation.json", applicationCaFile: "/secure/ca.pem", outputDirectory: test.outputDirectory },
        test.dependencies,
      ),
    ).rejects.toMatchObject({ code: "generator_image_invalid" });
    expect(test.sealArm).not.toHaveBeenCalled();
    expect(test.assertAbsent).not.toHaveBeenCalled();
  });

  it("rejects a preflight-only Event before arm or Docker", async () => {
    const path = manifestFile((manifest) => {
      manifest.event.slug = `load-test-preflight-${manifest.run_id.replaceAll("-", "")}`;
      manifest.event.title = `[LOAD-TEST] ${manifest.event.slug}`;
    });
    const test = harness({ manifestPath: path });

    await expect(
      runK6GeneratorRuntimeWithDependencies(
        { manifestPath: path, preparationEvidencePath: "/secure/preparation.json", applicationCaFile: "/secure/ca.pem", outputDirectory: test.outputDirectory },
        test.dependencies,
      ),
    ).rejects.toMatchObject({ code: "generator_preflight_event_forbidden" });
    expect(test.sealArm).not.toHaveBeenCalled();
    expect(test.assertAbsent).not.toHaveBeenCalled();
  });
});
