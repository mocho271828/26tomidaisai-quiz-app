import { CANARY_DEADLINE_MILLISECONDS } from "./generator/canary.js";
import { lstatSync, realpathSync } from "node:fs";
import { isAbsolute, relative, sep } from "node:path";
import type { ChildProcess } from "node:child_process";

import {
  ArtifactRefusal,
  sealArtifact,
  validateSecureExternalDirectory,
} from "./artifacts.js";
import type { ArtifactIdentity } from "./artifacts.js";
import {
  HOST_RESOURCE_POLL_MILLISECONDS,
  HostGuardRefusal,
  LinuxHostGuard,
} from "./host-guard.js";
import type { HostResourceSample } from "./host-guard.js";
import { buildK6ChildEnvironment } from "./k6-child-environment.js";
import {
  K6_COMMAND,
  K6_FIXED_RUN_ARGUMENTS,
  spawnK6Process,
} from "./k6-process.js";
import type { K6SpawnOptions } from "./k6-process.js";
import { GeneratorStreamProcessingRefusal } from "./generator-stream-refusal.js";
import {
  LauncherFinalArtifactRefusal,
  sealFinalMetricsArtifact,
} from "./launcher-final-artifact.js";
import {
  loadManifest,
  ManifestRefusal,
  runtimeRepositoryRoot,
} from "./manifest.js";
import type { LoadedManifest } from "./manifest.js";
export { buildK6ChildEnvironment } from "./k6-child-environment.js";
export { K6_COMMAND, K6_FIXED_RUN_ARGUMENTS } from "./k6-process.js";
export type { K6SpawnOptions } from "./k6-process.js";
export const K6_RESOURCE_POLL_MILLISECONDS = HOST_RESOURCE_POLL_MILLISECONDS;
export const K6_TERMINATION_GRACE_MILLISECONDS = 5_000;
export const K6_TERMINATION_HARD_LIMIT_MILLISECONDS = 1_000;

export type LauncherRefusalCode =
  | "launcher_artifact_failed"
  | "launcher_child_exit_failed"
  | "launcher_child_spawn_failed"
  | "launcher_child_termination_failed"
  | "launcher_canary_timeout"
  | "launcher_execution_timeout"
  | "launcher_host_cpu_limit_sustained"
  | "launcher_host_guard_failed"
  | "launcher_host_memory_limit_sustained"
  | "launcher_host_scheduler_lag_sustained"
  | "launcher_interrupted"
  | "launcher_manifest_invalid"
  | "launcher_metrics_incomplete"
  | "launcher_platform_unsupported"
  | "launcher_round_incomplete"
  | "launcher_round_sequence_invalid"
  | "launcher_script_invalid"
  | "launcher_start_late"
  | "launcher_stream_invalid";

export class LauncherRefusal extends Error {
  readonly code: LauncherRefusalCode;

  constructor(code: LauncherRefusalCode) {
    super(code);
    this.name = "LauncherRefusal";
    this.code = code;
  }
}

export interface CompletedRoundArtifact {
  readonly roundNumber: number;
  readonly name: string;
  readonly body: Uint8Array;
}

export interface CompletedFinalArtifact {
  readonly name: string;
  readonly body: Uint8Array;
}

export interface GeneratorStreamProcessor {
  isCanarySatisfied?(): boolean;
  takeProgressArtifact?(): CompletedFinalArtifact | null;
  /** 完結したRoundだけを返し、partial lineやinvalid eventではthrowする。 */
  push(chunk: Uint8Array): readonly CompletedRoundArtifact[];
  /** EOF framingを検証し、最後の完結済みRoundだけを返す。 */
  finish(): readonly CompletedRoundArtifact[];
  /** 正常終了とRound完結をlauncherが確認した後にだけsealする最終artifact。 */
  takeFinalArtifact?(): CompletedFinalArtifact | null;
}

export interface K6LauncherOptions {
  readonly manifestPath: string;
  readonly scriptPath: string;
  readonly outputDirectory: string;
  readonly createProcessor: (manifest: LoadedManifest) => GeneratorStreamProcessor;
  /** pre-arm時に読んだmanifestとspawn直前の再読込みを束縛する。 */
  readonly expectedManifestSha256?: string;
  /** formal runtimeが80分の実行上限と後処理graceを固定する。 */
  readonly executionHardLimitMilliseconds?: number;
  readonly requireMetricsArtifact?: true;
}

export interface K6LauncherResult {
  /** 完結順のRound evidence。最終metricsは別identityで返す。 */
  readonly artifacts: readonly ArtifactIdentity[];
  readonly metricsArtifact?: ArtifactIdentity;
  readonly manifestSha256: string;
  readonly scenarioSha256: string;
}

export interface LauncherDependencies {
  readonly spawnK6?: (
    command: string,
    arguments_: readonly string[],
    options: K6SpawnOptions,
  ) => ChildProcess;
  readonly hostGuard?: Pick<LinuxHostGuard, "poll">;
  readonly resourcePollMilliseconds?: number;
  readonly terminationGraceMilliseconds?: number;
  readonly terminationHardLimitMilliseconds?: number;
  readonly signalSource?: LauncherSignalSource;
}

export interface LauncherSignalSource {
  on(signal: "SIGINT" | "SIGTERM", listener: () => void): void;
  off(signal: "SIGINT" | "SIGTERM", listener: () => void): void;
}

/**
 * k6を固定command/argv/envで起動し、完結Roundだけを途中sealする。
 * child・stream・host guardの異常時は固定codeで停止し、未完結Roundを残さない。
 */
export async function runK6Launcher(
  options: K6LauncherOptions,
  dependencies: LauncherDependencies = {},
): Promise<K6LauncherResult> {
  const loadedManifest = loadManifestForLauncher(options);
  const repositoryRoot = trustedRepositoryRoot();
  const resolvedScript = validateScript(options.scriptPath, repositoryRoot);
  try {
    validateSecureExternalDirectory(options.outputDirectory);
  } catch (error: unknown) {
    if (error instanceof ArtifactRefusal) {
      throw new LauncherRefusal("launcher_artifact_failed");
    }
    throw error;
  }

  let processor: GeneratorStreamProcessor;
  try {
    processor = options.createProcessor(loadedManifest);
  } catch {
    throw new LauncherRefusal("launcher_stream_invalid");
  }
  if (
    options.requireMetricsArtifact === true &&
    typeof processor.takeFinalArtifact !== "function"
  ) {
    throw new LauncherRefusal("launcher_stream_invalid");
  }
  const pollMilliseconds =
    dependencies.resourcePollMilliseconds ?? K6_RESOURCE_POLL_MILLISECONDS;
  const terminationGraceMilliseconds =
    dependencies.terminationGraceMilliseconds ?? K6_TERMINATION_GRACE_MILLISECONDS;
  const terminationHardLimitMilliseconds =
    dependencies.terminationHardLimitMilliseconds ?? K6_TERMINATION_HARD_LIMIT_MILLISECONDS;
  requirePositiveDuration(pollMilliseconds);
  requirePositiveDuration(terminationGraceMilliseconds);
  requirePositiveDuration(terminationHardLimitMilliseconds);
  if (options.executionHardLimitMilliseconds !== undefined) {
    requirePositiveDuration(options.executionHardLimitMilliseconds);
  }
  const resourceAbort = loadedManifest.manifest.scenario.resource_abort;
  const hostGuard =
    dependencies.hostGuard ??
    new LinuxHostGuard({
      expectedPollMilliseconds: pollMilliseconds,
      schedulerLagThresholdMilliseconds: resourceAbort.scheduler_lag_ms_over,
      sustainedMilliseconds: resourceAbort.consecutive_seconds * 1_000,
      thresholdPercent: resourceAbort.generator_cpu_percent_over,
    });

  pollHostGuard(hostGuard);
  const spawnK6 = dependencies.spawnK6 ?? spawnK6Process;
  let childEnvironment: NodeJS.ProcessEnv;
  try {
    childEnvironment = buildK6ChildEnvironment(loadedManifest);
  } catch {
    throw new LauncherRefusal("launcher_manifest_invalid");
  }
  let child: ChildProcess;
  try {
    child = spawnK6(K6_COMMAND, [...K6_FIXED_RUN_ARGUMENTS, resolvedScript], {
      cwd: repositoryRoot,
      env: childEnvironment,
      shell: false,
      stdio: ["ignore", "pipe", "ignore"],
      windowsHide: true,
    });
  } catch (error: unknown) {
    if (error instanceof LauncherRefusal) {
      throw error;
    }
    throw new LauncherRefusal("launcher_child_spawn_failed");
  }
  if (child.stdout === null) {
    terminateChild(child, "SIGKILL");
    throw new LauncherRefusal("launcher_child_spawn_failed");
  }

  return await superviseChild({
    child,
    processor,
    hostGuard,
    loadedManifest,
    outputDirectory: options.outputDirectory,
    requireMetricsArtifact: options.requireMetricsArtifact === true,
    pollMilliseconds,
    terminationGraceMilliseconds,
    terminationHardLimitMilliseconds,
    signalSource: dependencies.signalSource ?? process,
    ...(options.executionHardLimitMilliseconds === undefined
      ? {}
      : {
          executionHardLimitMilliseconds:
            options.executionHardLimitMilliseconds,
        }),
  });
}

interface SupervisionOptions {
  readonly child: ChildProcess;
  readonly processor: GeneratorStreamProcessor;
  readonly hostGuard: Pick<LinuxHostGuard, "poll">;
  readonly loadedManifest: LoadedManifest;
  readonly outputDirectory: string;
  readonly requireMetricsArtifact: boolean;
  readonly pollMilliseconds: number;
  readonly terminationGraceMilliseconds: number;
  readonly terminationHardLimitMilliseconds: number;
  readonly signalSource: LauncherSignalSource;
  readonly executionHardLimitMilliseconds?: number;
}

function superviseChild(options: SupervisionOptions): Promise<K6LauncherResult> {
  return new Promise<K6LauncherResult>((resolve, reject) => {
    const artifacts: ArtifactIdentity[] = [];
    const expectedRoundCount = options.loadedManifest.manifest.scenario.rounds.length;
    let failure: LauncherRefusal | null = null;
    let settled = false;
    let terminationTimer: NodeJS.Timeout | null = null;
    let hardLimitTimer: NodeJS.Timeout | null = null;
    let executionTimer: NodeJS.Timeout | null = null;
    let canaryTimer: NodeJS.Timeout | null = null;

    const cleanup = (): void => {
      clearInterval(resourceTimer);
      if (canaryTimer !== null) clearTimeout(canaryTimer);
      if (terminationTimer !== null) {
        clearTimeout(terminationTimer);
      }
      if (hardLimitTimer !== null) {
        clearTimeout(hardLimitTimer);
      }
      if (executionTimer !== null) {
        clearTimeout(executionTimer);
      }
      options.signalSource.off("SIGINT", onInterrupt);
      options.signalSource.off("SIGTERM", onInterrupt);
    };

    const fail = (refusal: LauncherRefusal): void => {
      if (failure !== null || settled) {
        return;
      }
      failure = refusal;
      terminateChild(options.child, "SIGTERM");
      terminationTimer = setTimeout(() => {
        terminateChild(options.child, "SIGKILL");
        hardLimitTimer = setTimeout(() => {
          if (settled) {
            return;
          }
          settled = true;
          cleanup();
          reject(new LauncherRefusal("launcher_child_termination_failed"));
        }, options.terminationHardLimitMilliseconds);
      }, options.terminationGraceMilliseconds);
    };

    const sealCompleted = (completed: readonly CompletedRoundArtifact[]): void => {
      let expectedNumber = artifacts.length + 1;
      for (const artifact of completed) {
        if (
          !Number.isSafeInteger(artifact.roundNumber) ||
          artifact.roundNumber !== expectedNumber ||
          artifact.roundNumber > expectedRoundCount ||
          !(artifact.body instanceof Uint8Array)
        ) {
          throw new LauncherRefusal("launcher_round_sequence_invalid");
        }
        expectedNumber += 1;
      }
      for (const artifact of completed) {
        artifacts.push(
          sealArtifact({
            outputDirectory: options.outputDirectory,
            name: artifact.name,
            body: artifact.body,
          }),
        );
      }
    };

    const onInterrupt = (): void => {
      fail(new LauncherRefusal("launcher_interrupted"));
    };

    const sealProgress = (): void => {
      const progress = options.processor.takeProgressArtifact?.();
      if (progress !== undefined && progress !== null) {
        sealArtifact({ outputDirectory: options.outputDirectory, ...progress });
      }
    };

    options.child.stdout?.on("data", (chunk: Buffer | string) => {
      if (failure !== null || settled) {
        return;
      }
      try {
        const bytes = typeof chunk === "string" ? Buffer.from(chunk) : chunk;
        sealCompleted(options.processor.push(bytes));
        sealProgress();
      } catch (error: unknown) {
        try {
          sealCompleted(completedArtifactsBeforeRefusal(error));
        } catch (sealError: unknown) {
          fail(mapProcessingFailure(sealError));
          return;
        }
        fail(mapProcessingFailure(error));
      }
    });
    options.child.stdout?.on("error", () => {
      fail(new LauncherRefusal("launcher_stream_invalid"));
    });
    options.child.once("error", () => {
      fail(new LauncherRefusal("launcher_child_spawn_failed"));
    });
    options.child.once("close", (code, signal) => {
      if (settled) {
        return;
      }
      settled = true;
      cleanup();
      if (failure !== null) {
        reject(failure);
        return;
      }
      if (code !== 0 || signal !== null) {
        reject(new LauncherRefusal("launcher_child_exit_failed"));
        return;
      }
      try {
        sealCompleted(options.processor.finish());
        if (artifacts.length !== expectedRoundCount) {
          throw new LauncherRefusal("launcher_round_incomplete");
        }
        const metricsArtifact = sealFinalMetricsArtifact(
          options.processor,
          options.outputDirectory,
          options.requireMetricsArtifact,
        );
        resolve({
          artifacts,
          ...(metricsArtifact === null ? {} : { metricsArtifact }),
          manifestSha256: options.loadedManifest.hashes.manifestSha256,
          scenarioSha256: options.loadedManifest.hashes.scenarioSha256,
        });
      } catch (error: unknown) {
        try {
          sealCompleted(completedArtifactsBeforeRefusal(error));
        } catch (sealError: unknown) {
          reject(mapProcessingFailure(sealError));
          return;
        }
        reject(mapProcessingFailure(error));
      }
    });

    options.signalSource.on("SIGINT", onInterrupt);
    options.signalSource.on("SIGTERM", onInterrupt);
    if (options.executionHardLimitMilliseconds !== undefined) {
      executionTimer = setTimeout(() => {
        fail(new LauncherRefusal("launcher_execution_timeout"));
      }, options.executionHardLimitMilliseconds);
    }
    if (options.processor.isCanarySatisfied !== undefined) {
      canaryTimer = setTimeout(() => {
        if (options.processor.isCanarySatisfied?.() !== true) fail(new LauncherRefusal("launcher_canary_timeout"));
      }, Math.max(1, Date.parse(options.loadedManifest.manifest.scheduled_start_at) +
        CANARY_DEADLINE_MILLISECONDS - Date.now()));
    }
    const resourceTimer = setInterval(() => {
      if (failure !== null || settled) {
        return;
      }
      try {
        const sample = options.hostGuard.poll();
        if (sample.abortReason === "host_cpu_limit_sustained") {
          fail(new LauncherRefusal("launcher_host_cpu_limit_sustained"));
        } else if (sample.abortReason === "host_memory_limit_sustained") {
          fail(new LauncherRefusal("launcher_host_memory_limit_sustained"));
        } else if (sample.abortReason === "host_scheduler_lag_sustained") {
          fail(new LauncherRefusal("launcher_host_scheduler_lag_sustained"));
        }
        if (failure === null) sealProgress();
      } catch {
        fail(new LauncherRefusal("launcher_host_guard_failed"));
      }
    }, options.pollMilliseconds);
  });
}

function loadManifestForLauncher(options: K6LauncherOptions): LoadedManifest {
  try {
    const loaded = loadManifest(options.manifestPath);
    if (
      options.expectedManifestSha256 !== undefined &&
      loaded.hashes.manifestSha256 !== options.expectedManifestSha256
    ) {
      throw new LauncherRefusal("launcher_manifest_invalid");
    }
    return loaded;
  } catch (error: unknown) {
    if (error instanceof ManifestRefusal || error instanceof ArtifactRefusal) {
      throw new LauncherRefusal("launcher_manifest_invalid");
    }
    throw new LauncherRefusal("launcher_manifest_invalid");
  }
}

function trustedRepositoryRoot(): string {
  try {
    return runtimeRepositoryRoot();
  } catch {
    throw new LauncherRefusal("launcher_script_invalid");
  }
}

function validateScript(scriptPath: string, repositoryRoot: string): string {
  try {
    if (!isAbsolute(scriptPath) || !isAbsolute(repositoryRoot)) {
      throw new LauncherRefusal("launcher_script_invalid");
    }
    const lexicalStatus = lstatSync(scriptPath, { bigint: true });
    const resolvedScript = realpathSync.native(scriptPath);
    const resolvedRepository = realpathSync.native(repositoryRoot);
    const difference = relative(resolvedRepository, resolvedScript);
    if (
      lexicalStatus.isSymbolicLink() ||
      !lexicalStatus.isFile() ||
      lexicalStatus.nlink !== 1n ||
      difference === ".." ||
      difference.startsWith(`..${sep}`) ||
      isAbsolute(difference)
    ) {
      throw new LauncherRefusal("launcher_script_invalid");
    }
    return resolvedScript;
  } catch (error: unknown) {
    if (error instanceof LauncherRefusal) {
      throw error;
    }
    throw new LauncherRefusal("launcher_script_invalid");
  }
}

function pollHostGuard(hostGuard: Pick<LinuxHostGuard, "poll">): HostResourceSample {
  try {
    const sample = hostGuard.poll();
    if (sample.abortReason === "host_cpu_limit_sustained") {
      throw new LauncherRefusal("launcher_host_cpu_limit_sustained");
    }
    if (sample.abortReason === "host_memory_limit_sustained") {
      throw new LauncherRefusal("launcher_host_memory_limit_sustained");
    }
    if (sample.abortReason === "host_scheduler_lag_sustained") {
      throw new LauncherRefusal("launcher_host_scheduler_lag_sustained");
    }
    return sample;
  } catch (error: unknown) {
    if (error instanceof LauncherRefusal) {
      throw error;
    }
    if (error instanceof HostGuardRefusal) {
      throw new LauncherRefusal("launcher_host_guard_failed");
    }
    throw new LauncherRefusal("launcher_host_guard_failed");
  }
}

function mapProcessingFailure(error: unknown): LauncherRefusal {
  if (error instanceof LauncherRefusal) {
    return error;
  }
  if (error instanceof ArtifactRefusal) {
    return new LauncherRefusal("launcher_artifact_failed");
  }
  if (error instanceof LauncherFinalArtifactRefusal) {
    return new LauncherRefusal(
      error.code === "final_artifact_metrics_incomplete"
        ? "launcher_metrics_incomplete"
        : "launcher_stream_invalid",
    );
  }
  return new LauncherRefusal("launcher_stream_invalid");
}

function completedArtifactsBeforeRefusal(
  error: unknown,
): readonly CompletedRoundArtifact[] {
  return error instanceof GeneratorStreamProcessingRefusal
    ? error.completedArtifacts
    : [];
}

function terminateChild(child: ChildProcess, signal: NodeJS.Signals): void {
  try {
    child.kill(signal);
  } catch {
    // kill error本文はcredentialやargvを含み得るため固定code以外へ流さない。
  }
}

function requirePositiveDuration(value: number): void {
  if (!Number.isFinite(value) || value <= 0) {
    throw new LauncherRefusal("launcher_host_guard_failed");
  }
}
