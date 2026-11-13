import { setTimeout as wait } from "node:timers/promises";
import { dirname, join } from "node:path";
import { loadPreparedApplicationCa } from "./preparation.js";

import {
  ArtifactRefusal,
  sealArtifact,
  readSecureExternalArtifact,
  validateSecureExternalDirectory,
} from "./artifacts.js";
import type { ArtifactIdentity } from "./artifacts.js";
import {
  FormalContainerCustodian,
  FormalDockerRefusal,
  createFormalDockerSpawner,
  readFormalGeneratorBundle,
  requireApprovedImage,
  requireNonRootIdentity,
  requireRootOwnedDockerConfigDirectory,
  spawnDockerProcess,
  validateGeneratorBundleSnapshot,
} from "./formal-docker.js";
import type {
  DockerProcessSpawner,
  FormalContainerBinding,
  GeneratorBundle,
} from "./formal-docker.js";
import {
  GeneratorArmRefusal,
  productionGeneratorRuntimeBaseDirectory,
  sealGeneratorArmMarker,
} from "./generator-arm.js";
import type {
  GeneratorArmArtifacts,
  GeneratorArmBinding,
} from "./generator-arm.js";
import { GeneratorOutputStreamProcessor } from "./generator-output-stream-processor.js";
import { GENERATOR_METRICS_ARTIFACT_NAME } from "./generator/metrics-summary-contract.js";
import {
  LauncherRefusal,
  runK6Launcher,
} from "./launcher.js";
import type {
  K6LauncherOptions,
  K6LauncherResult,
  LauncherDependencies,
  LauncherSignalSource,
} from "./launcher.js";
import {
  loadManifest,
  ManifestRefusal,
  runtimeRepositoryRoot,
} from "./manifest.js";
import type { LoadedManifest } from "./manifest.js";
import { SCENARIO_START_TOLERANCE_MILLISECONDS } from "./generator/k6-runtime-policy.js";

const FORMAL_RUNTIME_MILLISECONDS = 4_800_000;
const FORMAL_RUNTIME_GRACE_MILLISECONDS = 30_000;
const MAX_WAIT_SLICE_MILLISECONDS = 60_000;

export type FormalRuntimeRefusalCode =
  | "generator_clock_invalid"
  | "generator_interrupted"
  | "generator_result_binding_invalid"
  | "generator_preflight_event_forbidden"
  | "generator_start_not_armed";

export class FormalRuntimeRefusal extends Error {
  readonly code: FormalRuntimeRefusalCode;

  constructor(code: FormalRuntimeRefusalCode) {
    super(code);
    this.name = "FormalRuntimeRefusal";
    this.code = code;
  }
}

export interface K6GeneratorRuntimeOptions {
  readonly manifestPath: string;
  readonly outputDirectory: string;
  readonly preparationEvidencePath: string;
  readonly applicationCaFile: string;
}

export interface K6GeneratorRuntimeResult
  extends Omit<K6LauncherResult, "metricsArtifact"> {
  readonly format: "load-test-generator-result-v1";
  readonly runId: string;
  readonly eventId: string;
  readonly scheduledStartAt: string;
  readonly actualSpawnAt: string;
  readonly generatorImage: string;
  readonly generatorBundle: ArtifactIdentity;
  readonly armMarker: ArtifactIdentity;
  readonly containerName: string;
  readonly metricsArtifact: ArtifactIdentity;
}

export interface FormalContainerCustodianPort {
  assertAbsent(): void;
  cleanupAndAssertAbsent(): void;
}

export interface FormalRuntimeDependencies {
  readonly readPreparation?: typeof loadPreparedApplicationCa;
  readonly platform: NodeJS.Platform;
  readonly nowMilliseconds: () => number;
  readonly monotonicMilliseconds: () => number;
  readonly sleep: (milliseconds: number, signal: AbortSignal) => Promise<void>;
  readonly signalSource: LauncherSignalSource;
  readonly effectiveUserId: () => number | undefined;
  readonly effectiveGroupId: () => number | undefined;
  readonly requireDockerConfig: () => void;
  readonly runtimeBaseDirectory: (userId: number, groupId: number) => string;
  readonly readBundle: (
    scriptPath: string,
    repositoryRoot: string,
    userId: number,
    groupId: number,
  ) => GeneratorBundle;
  readonly sealArm: (
    binding: GeneratorArmBinding,
    bundleBody: Uint8Array,
    options: {
      readonly runtimeBaseDirectory: string;
      readonly effectiveUserId: number;
      readonly effectiveGroupId: number;
      readonly platform: NodeJS.Platform;
    },
  ) => GeneratorArmArtifacts;
  readonly createCustodian: (
    binding: FormalContainerBinding,
  ) => FormalContainerCustodianPort;
  readonly validateBundleSnapshot: (
    path: string,
    expected: Pick<GeneratorBundle, "sha256" | "sizeBytes">,
    userId: number,
    groupId: number,
  ) => void;
  readonly spawnDocker: DockerProcessSpawner;
  readonly runLauncher: (
    options: K6LauncherOptions,
    dependencies?: LauncherDependencies,
  ) => Promise<K6LauncherResult>;
}

const PRODUCTION_DEPENDENCIES: FormalRuntimeDependencies = {
  readPreparation: loadPreparedApplicationCa,
  platform: process.platform,
  nowMilliseconds: Date.now,
  monotonicMilliseconds: () =>
    Number(process.hrtime.bigint() / 1_000_000n),
  sleep: async (milliseconds, signal) => {
    await wait(milliseconds, undefined, { signal });
  },
  signalSource: process,
  effectiveUserId: () => process.geteuid?.(),
  effectiveGroupId: () => process.getegid?.(),
  requireDockerConfig: requireRootOwnedDockerConfigDirectory,
  runtimeBaseDirectory: productionGeneratorRuntimeBaseDirectory,
  readBundle: readFormalGeneratorBundle,
  sealArm: sealGeneratorArmMarker,
  createCustodian: (binding) => new FormalContainerCustodian(binding),
  validateBundleSnapshot: validateGeneratorBundleSnapshot,
  spawnDocker: spawnDockerProcess,
  runLauncher: runK6Launcher,
};

/** CLIから呼ぶ唯一のformal runtime。dependencyやscriptは差し替えできない。 */
export function runK6GeneratorRuntime(
  options: K6GeneratorRuntimeOptions,
): Promise<K6GeneratorRuntimeResult> {
  if (process.platform !== "linux") {
    throw new LauncherRefusal("launcher_platform_unsupported");
  }
  return runK6GeneratorRuntimeWithDependencies(options, PRODUCTION_DEPENDENCIES);
}

/** fake clock/processでformal境界を検査するためのsource-only seam。 */
export async function runK6GeneratorRuntimeWithDependencies(
  options: K6GeneratorRuntimeOptions,
  dependencies: FormalRuntimeDependencies,
): Promise<K6GeneratorRuntimeResult> {
  if (dependencies.platform !== "linux") {
    throw new LauncherRefusal("launcher_platform_unsupported");
  }
  const identity = requireNonRootIdentity(
    dependencies.effectiveUserId(),
    dependencies.effectiveGroupId(),
  );
  const loadedManifest = loadFormalManifest(options.manifestPath);
  const caBody = dependencies.readPreparation?.(options, loadedManifest, dependencies.nowMilliseconds());
  requireApprovedImage(loadedManifest.manifest.artifacts.generator_image);
  if (loadedManifest.manifest.event.slug.startsWith("load-test-preflight-")) {
    throw new FormalRuntimeRefusal("generator_preflight_event_forbidden");
  }
  const repositoryRoot = formalRepositoryRoot();
  const hostScriptPath = formalGeneratorScriptPath();
  validateFormalOutputDirectory(options.outputDirectory);
  dependencies.requireDockerConfig();
  const bundle = dependencies.readBundle(
    hostScriptPath,
    repositoryRoot,
    identity.userId,
    identity.groupId,
  );
  const schedule = scheduledWindow(loadedManifest);
  const initialNow = requireClock(dependencies.nowMilliseconds());
  const initialMonotonic = requireClock(dependencies.monotonicMilliseconds());
  if (initialNow >= schedule.scheduledMilliseconds) {
    throw new FormalRuntimeRefusal("generator_start_not_armed");
  }

  const containerBinding = buildContainerBinding(loadedManifest);
  const custodian = dependencies.createCustodian(containerBinding);
  custodian.assertAbsent();
  const runtimeBaseDirectory = dependencies.runtimeBaseDirectory(
    identity.userId,
    identity.groupId,
  );
  const armBinding = buildArmBinding(
    loadedManifest,
    containerBinding,
    bundle,
  );
  const arm = dependencies.sealArm(armBinding, bundle.body, {
    runtimeBaseDirectory,
    effectiveUserId: identity.userId,
    effectiveGroupId: identity.groupId,
    platform: dependencies.platform,
  });

  const caPath = caBody === undefined ? undefined : join(dirname(arm.bundlePath), `${containerBinding.runId}.ca.pem`);
  const caIdentity = caBody === undefined ? undefined : sealArtifact({
    outputDirectory: dirname(arm.bundlePath), name: `${containerBinding.runId}.ca.pem`, body: caBody,
  });

  const abortController = new AbortController();
  let wasInterrupted = false;
  const onInterrupt = (): void => {
    wasInterrupted = true;
    abortController.abort();
  };
  dependencies.signalSource.on("SIGINT", onInterrupt);
  dependencies.signalSource.on("SIGTERM", onInterrupt);

  let shouldCleanup = false;
  let launcherResult: K6LauncherResult | null = null;
  let launchFailure: unknown;
  let actualSpawnMilliseconds: number | null = null;
  try {
    await waitForSpawnWindow(
      initialNow,
      initialMonotonic,
      schedule.earliestMilliseconds,
      dependencies,
      abortController.signal,
      () => wasInterrupted,
    );
    if (wasInterrupted) {
      throw new FormalRuntimeRefusal("generator_interrupted");
    }
    const beforeLaunch = requireClock(dependencies.nowMilliseconds());
    if (
      beforeLaunch < schedule.earliestMilliseconds ||
      beforeLaunch > schedule.latestMilliseconds
    ) {
      throw new LauncherRefusal("launcher_start_late");
    }
    dependencies.requireDockerConfig();
    dependencies.validateBundleSnapshot(
      arm.bundlePath,
      bundle,
      identity.userId,
      identity.groupId,
    );
    custodian.assertAbsent();
    shouldCleanup = true;
    const spawnK6 = createFormalDockerSpawner({
      binding: containerBinding,
      bundlePath: arm.bundlePath,
      ...(caPath === undefined ? {} : { applicationCaPath: caPath }),
      expectedBundle: bundle,
      hostScriptPath,
      loadedManifest,
      repositoryRoot,
      effectiveUserId: identity.userId,
      effectiveGroupId: identity.groupId,
      requireDockerConfig: dependencies.requireDockerConfig,
      validateBundleSnapshot: () => {
        if (caPath !== undefined && readSecureExternalArtifact(caPath, { maximumBytes: 65_536 }).artifact.sha256 !== caIdentity?.sha256) {
          throw new FormalRuntimeRefusal("generator_result_binding_invalid");
        }
        dependencies.validateBundleSnapshot(
          arm.bundlePath,
          bundle,
          identity.userId,
          identity.groupId,
        );
      },
      nowMilliseconds: dependencies.nowMilliseconds,
      earliestSpawnMilliseconds: schedule.earliestMilliseconds,
      latestSpawnMilliseconds: schedule.latestMilliseconds,
      recordSpawnMilliseconds: (value) => {
        actualSpawnMilliseconds = value;
      },
      spawnDocker: dependencies.spawnDocker,
    });
    const executionHardLimitMilliseconds =
      schedule.scheduledMilliseconds +
      FORMAL_RUNTIME_MILLISECONDS +
      FORMAL_RUNTIME_GRACE_MILLISECONDS -
      beforeLaunch;
    if (
      !Number.isSafeInteger(executionHardLimitMilliseconds) ||
      executionHardLimitMilliseconds <= 0
    ) {
      throw new FormalRuntimeRefusal("generator_clock_invalid");
    }
    const launcherPromise = dependencies.runLauncher(
      {
        manifestPath: options.manifestPath,
        scriptPath: hostScriptPath,
        outputDirectory: options.outputDirectory,
        createProcessor: (manifest) =>
          new GeneratorOutputStreamProcessor(manifest),
        expectedManifestSha256: loadedManifest.hashes.manifestSha256,
        executionHardLimitMilliseconds,
        requireMetricsArtifact: true,
      },
      { signalSource: dependencies.signalSource, spawnK6 },
    );
    launcherResult = await launcherPromise;
  } catch (error: unknown) {
    launchFailure = mapWaitFailure(error, wasInterrupted);
  }

  try {
    if (shouldCleanup) {
      custodian.cleanupAndAssertAbsent();
    }
  } catch (error: unknown) {
    launchFailure = error;
  } finally {
    dependencies.signalSource.off("SIGINT", onInterrupt);
    dependencies.signalSource.off("SIGTERM", onInterrupt);
  }
  if (wasInterrupted && launchFailure === undefined) {
    launchFailure = new FormalRuntimeRefusal("generator_interrupted");
  }
  if (launchFailure !== undefined) {
    throw launchFailure;
  }
  if (
    launcherResult === null ||
    actualSpawnMilliseconds === null ||
    launcherResult.metricsArtifact === undefined ||
    launcherResult.manifestSha256 !== loadedManifest.hashes.manifestSha256 ||
    launcherResult.scenarioSha256 !== loadedManifest.hashes.scenarioSha256 ||
    !hasCompleteArtifactInventory(
      launcherResult,
      loadedManifest.manifest.scenario.rounds.length,
    )
  ) {
    throw new FormalRuntimeRefusal("generator_result_binding_invalid");
  }
  return {
    ...launcherResult,
    format: "load-test-generator-result-v1",
    runId: loadedManifest.manifest.run_id,
    eventId: loadedManifest.manifest.event.id,
    scheduledStartAt: loadedManifest.manifest.scheduled_start_at,
    actualSpawnAt: canonicalTimestamp(actualSpawnMilliseconds),
    generatorImage: loadedManifest.manifest.artifacts.generator_image,
    generatorBundle: arm.bundle,
    armMarker: arm.marker,
    containerName: containerBinding.name,
    metricsArtifact: launcherResult.metricsArtifact,
  };
}

export function formalGeneratorScriptPath(): string {
  return join(runtimeRepositoryRoot(), "load-test/k6/dist/generator.js");
}

function buildContainerBinding(
  loadedManifest: LoadedManifest,
): FormalContainerBinding {
  return {
    name: `gs-loadtest-k6-${loadedManifest.manifest.run_id}`,
    runId: loadedManifest.manifest.run_id,
    eventId: loadedManifest.manifest.event.id,
    manifestSha256: loadedManifest.hashes.manifestSha256,
    image: loadedManifest.manifest.artifacts.generator_image,
  };
}

function buildArmBinding(
  loadedManifest: LoadedManifest,
  containerBinding: FormalContainerBinding,
  bundle: GeneratorBundle,
): GeneratorArmBinding {
  return {
    format: "load-test-generator-arm-v1",
    run_id: loadedManifest.manifest.run_id,
    manifest_sha256: loadedManifest.hashes.manifestSha256,
    generator_image: loadedManifest.manifest.artifacts.generator_image,
    generator_bundle_sha256: bundle.sha256,
    generator_bundle_size_bytes: bundle.sizeBytes,
    scheduled_start_at: loadedManifest.manifest.scheduled_start_at,
    container_name: containerBinding.name,
  };
}

async function waitForSpawnWindow(
  initialNow: number,
  initialMonotonic: number,
  earliestSpawnMilliseconds: number,
  dependencies: FormalRuntimeDependencies,
  signal: AbortSignal,
  isInterrupted: () => boolean,
): Promise<void> {
  let previous = initialNow;
  let previousMonotonic = initialMonotonic;
  while (previous < earliestSpawnMilliseconds) {
    if (isInterrupted()) {
      throw new FormalRuntimeRefusal("generator_interrupted");
    }
    const duration = Math.min(
      earliestSpawnMilliseconds - previous,
      MAX_WAIT_SLICE_MILLISECONDS,
    );
    try {
      await dependencies.sleep(duration, signal);
    } catch (error: unknown) {
      throw mapWaitFailure(error, isInterrupted());
    }
    const observed = requireClock(dependencies.nowMilliseconds());
    const observedMonotonic = requireClock(
      dependencies.monotonicMilliseconds(),
    );
    if (observed <= previous || observedMonotonic <= previousMonotonic) {
      throw new FormalRuntimeRefusal("generator_clock_invalid");
    }
    previous = observed;
    previousMonotonic = observedMonotonic;
  }
}

function scheduledWindow(loadedManifest: LoadedManifest): {
  readonly scheduledMilliseconds: number;
  readonly earliestMilliseconds: number;
  readonly latestMilliseconds: number;
} {
  const scheduledMilliseconds = Date.parse(
    loadedManifest.manifest.scheduled_start_at,
  );
  if (
    !Number.isSafeInteger(scheduledMilliseconds) ||
    scheduledMilliseconds < SCENARIO_START_TOLERANCE_MILLISECONDS ||
    scheduledMilliseconds >
      Number.MAX_SAFE_INTEGER - SCENARIO_START_TOLERANCE_MILLISECONDS
  ) {
    throw new FormalRuntimeRefusal("generator_clock_invalid");
  }
  return {
    scheduledMilliseconds,
    earliestMilliseconds:
      scheduledMilliseconds - SCENARIO_START_TOLERANCE_MILLISECONDS,
    latestMilliseconds:
      scheduledMilliseconds + SCENARIO_START_TOLERANCE_MILLISECONDS,
  };
}

function loadFormalManifest(path: string): LoadedManifest {
  try {
    return loadManifest(path);
  } catch (error: unknown) {
    if (error instanceof ManifestRefusal) {
      throw new LauncherRefusal("launcher_manifest_invalid");
    }
    throw new LauncherRefusal("launcher_manifest_invalid");
  }
}

function formalRepositoryRoot(): string {
  try {
    return runtimeRepositoryRoot();
  } catch {
    throw new LauncherRefusal("launcher_script_invalid");
  }
}

function validateFormalOutputDirectory(path: string): void {
  try {
    validateSecureExternalDirectory(path);
  } catch (error: unknown) {
    if (error instanceof ArtifactRefusal) {
      throw new LauncherRefusal("launcher_artifact_failed");
    }
    throw new LauncherRefusal("launcher_artifact_failed");
  }
}

function requireClock(value: number): number {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new FormalRuntimeRefusal("generator_clock_invalid");
  }
  return value;
}

function mapWaitFailure(error: unknown, wasInterrupted: boolean): unknown {
  if (wasInterrupted) {
    return new FormalRuntimeRefusal("generator_interrupted");
  }
  if (
    error instanceof FormalRuntimeRefusal ||
    error instanceof FormalDockerRefusal ||
    error instanceof GeneratorArmRefusal ||
    error instanceof LauncherRefusal
  ) {
    return error;
  }
  return new FormalRuntimeRefusal("generator_clock_invalid");
}

function canonicalTimestamp(milliseconds: number): string {
  const timestamp = new Date(milliseconds).toISOString();
  return timestamp.replace(/\.000Z$/u, "Z");
}

function hasCompleteArtifactInventory(
  result: K6LauncherResult,
  expectedRoundCount: number,
): boolean {
  if (
    result.artifacts.length !== expectedRoundCount ||
    result.metricsArtifact === undefined ||
    result.metricsArtifact.name !== GENERATOR_METRICS_ARTIFACT_NAME ||
    !isArtifactIdentity(result.metricsArtifact)
  ) {
    return false;
  }
  return result.artifacts.every(
    (artifact, index) =>
      artifact.name ===
        `generator-round-${String(index + 1).padStart(2, "0")}.json` &&
      isArtifactIdentity(artifact),
  );
}

function isArtifactIdentity(artifact: ArtifactIdentity): boolean {
  return (
    /^[0-9a-f]{64}(?![\s\S])/u.test(artifact.sha256) &&
    Number.isSafeInteger(artifact.size_bytes) &&
    artifact.size_bytes > 0
  );
}
