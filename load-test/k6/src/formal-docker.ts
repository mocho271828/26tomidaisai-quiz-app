import { spawn, spawnSync } from "node:child_process";
import type {
  ChildProcess,
  SpawnOptions,
  SpawnSyncOptionsWithStringEncoding,
} from "node:child_process";
import { createHash } from "node:crypto";
import {
  closeSync,
  constants,
  fstatSync,
  lstatSync,
  openSync,
  readSync,
  realpathSync,
  readdirSync,
} from "node:fs";
import { isAbsolute, relative, sep } from "node:path";

import { buildK6ChildEnvironment } from "./k6-child-environment.js";
import {
  K6_COMMAND,
  K6_FIXED_RUN_ARGUMENTS,
  LauncherRefusal,
} from "./launcher.js";
import type { K6SpawnOptions } from "./launcher.js";
import type { LoadedManifest } from "./manifest.js";

export const APPROVED_FORMAL_K6_IMAGE =
  "ghcr.io/grafana/k6:2.2.0@sha256:9bd01d6941fca969cb61bb57d2da5ee9b385fe2aa8881df3798c196564d6ace6";
export const DOCKER_COMMAND = "/usr/bin/docker";
export const DOCKER_CONFIG_DIRECTORY = "/etc/gs-loadtest/docker-cli";
export const DOCKER_ENDPOINT = "unix:///var/run/docker.sock";
export const CONTAINER_GENERATOR_PATH = "/load-test/generator.js";
const MAX_BUNDLE_BYTES = 16 * 1_024 * 1_024;
const DOCKER_OUTPUT_BYTES = 4_096;
export const FORMAL_DOCKER_CONTROL_TIMEOUT_MILLISECONDS = 10_000;
export const FORMAL_CONTAINER_CLEANUP_MAX_COMMANDS = 10;
export const FORMAL_CONTAINER_CLEANUP_WORST_CASE_MILLISECONDS =
  FORMAL_DOCKER_CONTROL_TIMEOUT_MILLISECONDS *
  FORMAL_CONTAINER_CLEANUP_MAX_COMMANDS;
const SHARED_WRITE_BITS = 0o022;
const CONTAINER_ID_PATTERN = /^[0-9a-f]{64}(?![\s\S])/u;
const CONTAINER_NAME_PATTERN =
  /^gs-loadtest-k6-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}(?![\s\S])/u;
const RUNTIME_ENVIRONMENT_KEYS = [
  "LANG",
  "LC_ALL",
  "LOAD_TEST_RUNTIME_MANIFEST_JSON",
  "LOAD_TEST_RUNTIME_MANIFEST_SHA256",
  "PATH",
  "TZ",
] as const;
const CONTAINER_ENVIRONMENT_KEYS = [
  "LOAD_TEST_RUNTIME_MANIFEST_JSON",
  "LOAD_TEST_RUNTIME_MANIFEST_SHA256",
] as const;
const DOCKER_CONTROL_ENVIRONMENT = Object.freeze({
  LANG: "C.UTF-8",
  LC_ALL: "C.UTF-8",
  TZ: "UTC",
});
const CONTAINER_INSPECT_FORMAT =
  '{"EventId":{{json (index .Config.Labels "org.tomidaisai.load-test.event-id")}},"Id":{{json .Id}},"Image":{{json .Config.Image}},"ManifestSha256":{{json (index .Config.Labels "org.tomidaisai.load-test.manifest-sha256")}},"Name":{{json .Name}},"RunId":{{json (index .Config.Labels "org.tomidaisai.load-test.run-id")}},"Running":{{json .State.Running}}}';

export type FormalDockerRefusalCode =
  | "generator_bundle_invalid"
  | "generator_container_cleanup_failed"
  | "generator_container_conflict"
  | "generator_docker_config_invalid"
  | "generator_image_invalid"
  | "generator_user_invalid";

export class FormalDockerRefusal extends Error {
  readonly code: FormalDockerRefusalCode;

  constructor(code: FormalDockerRefusalCode) {
    super(code);
    this.name = "FormalDockerRefusal";
    this.code = code;
  }
}

export interface GeneratorBundle {
  readonly body: Uint8Array;
  readonly sha256: string;
  readonly sizeBytes: number;
}

export interface FormalContainerBinding {
  readonly name: string;
  readonly runId: string;
  readonly eventId: string;
  readonly manifestSha256: string;
  readonly image: string;
}

export type DockerProcessSpawner = (
  command: string,
  arguments_: readonly string[],
  options: SpawnOptions,
) => ChildProcess;

export interface FormalDockerSpawnConfiguration {
  readonly binding: FormalContainerBinding;
  readonly bundlePath: string;
  readonly applicationCaPath?: string;
  readonly expectedBundle: Pick<GeneratorBundle, "sha256" | "sizeBytes">;
  readonly hostScriptPath: string;
  readonly loadedManifest: LoadedManifest;
  readonly repositoryRoot: string;
  readonly effectiveUserId: number;
  readonly effectiveGroupId: number;
  readonly requireDockerConfig: () => void;
  readonly validateBundleSnapshot: () => void;
  readonly nowMilliseconds: () => number;
  readonly earliestSpawnMilliseconds: number;
  readonly latestSpawnMilliseconds: number;
  readonly recordSpawnMilliseconds: (value: number) => void;
  readonly spawnDocker: DockerProcessSpawner;
}

/** formal runtimeのDocker processはabsolute binaryと管理済みenvだけで起動する。 */
export function spawnDockerProcess(
  command: string,
  arguments_: readonly string[],
  options: SpawnOptions,
): ChildProcess {
  return spawn(command, [...arguments_], options);
}

/** launcherが渡す固定k6 invocationをdigest固定Docker invocationへ変換する。 */
export function createFormalDockerSpawner(
  configuration: FormalDockerSpawnConfiguration,
): (command: string, arguments_: readonly string[], options: K6SpawnOptions) => ChildProcess {
  return (command, arguments_, options) => {
    validateLauncherInvocation(command, arguments_, options, configuration);
    configuration.requireDockerConfig();
    configuration.validateBundleSnapshot();
    const observedAt = configuration.nowMilliseconds();
    if (
      !Number.isSafeInteger(observedAt) ||
      observedAt < configuration.earliestSpawnMilliseconds ||
      observedAt > configuration.latestSpawnMilliseconds
    ) {
      throw new LauncherRefusal("launcher_start_late");
    }
    configuration.recordSpawnMilliseconds(observedAt);
    const dockerArguments = buildFormalDockerArguments(configuration);
    return configuration.spawnDocker(DOCKER_COMMAND, dockerArguments, {
      cwd: configuration.repositoryRoot,
      env: options.env,
      shell: false,
      stdio: ["ignore", "pipe", "ignore"],
      windowsHide: true,
    });
  };
}

export function buildFormalDockerArguments(
  configuration: Pick<
    FormalDockerSpawnConfiguration,
    "binding" | "bundlePath" | "effectiveGroupId" | "effectiveUserId" | "applicationCaPath"
  >,
): readonly string[] {
  const { binding } = configuration;
  requireApprovedImage(binding.image);
  requireContainerName(binding.name);
  const expectedBundlePath =
    `/run/user/${configuration.effectiveUserId}/gs-loadtest-k6/` +
    `${binding.runId}.generator.js`;
  if (configuration.bundlePath !== expectedBundlePath) {
    throw new FormalDockerRefusal("generator_bundle_invalid");
  }
  if (configuration.applicationCaPath !== undefined && configuration.applicationCaPath !==
      `/run/user/${configuration.effectiveUserId}/gs-loadtest-k6/${binding.runId}.ca.pem`) {
    throw new FormalDockerRefusal("generator_bundle_invalid");
  }
  return [
    "--config",
    DOCKER_CONFIG_DIRECTORY,
    "--host",
    DOCKER_ENDPOINT,
    "run",
    "--rm",
    "--pull=never",
    "--init",
    `--name=${binding.name}`,
    "--network=bridge",
    "--read-only",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges=true",
    `--user=${configuration.effectiveUserId}:${configuration.effectiveGroupId}`,
    `--label=org.tomidaisai.load-test.run-id=${binding.runId}`,
    `--label=org.tomidaisai.load-test.event-id=${binding.eventId}`,
    `--label=org.tomidaisai.load-test.manifest-sha256=${binding.manifestSha256}`,
    "--mount",
    `type=bind,src=${configuration.bundlePath},dst=${CONTAINER_GENERATOR_PATH},readonly`,
    ...(configuration.applicationCaPath === undefined ? [] : [
      "--mount", `type=bind,src=${configuration.applicationCaPath},dst=/load-test/ca.pem,readonly`,
      "--env=SSL_CERT_FILE=/load-test/ca.pem",
    ]),
    ...CONTAINER_ENVIRONMENT_KEYS.flatMap((key) => ["--env", key]),
    binding.image,
    ...K6_FIXED_RUN_ARGUMENTS,
    CONTAINER_GENERATOR_PATH,
  ];
}

/** source bundleをFDで一度読み、arm snapshotの正本bytesとする。 */
export function readFormalGeneratorBundle(
  scriptPath: string,
  repositoryRoot: string,
  effectiveUserId: number,
  effectiveGroupId: number,
): GeneratorBundle {
  if (!isAbsolute(scriptPath) || !isAbsolute(repositoryRoot)) {
    throw new FormalDockerRefusal("generator_bundle_invalid");
  }
  try {
    const resolvedRoot = realpathSync.native(repositoryRoot);
    const resolvedScript = realpathSync.native(scriptPath);
    const difference = relative(resolvedRoot, resolvedScript);
    if (
      difference === ".." ||
      difference.startsWith(`..${sep}`) ||
      isAbsolute(difference)
    ) {
      throw new FormalDockerRefusal("generator_bundle_invalid");
    }
    return readOwnedBundle(
      scriptPath,
      effectiveUserId,
      effectiveGroupId,
      null,
    );
  } catch (error: unknown) {
    if (error instanceof FormalDockerRefusal) {
      throw error;
    }
    throw new FormalDockerRefusal("generator_bundle_invalid");
  }
}

export function validateGeneratorBundleSnapshot(
  path: string,
  expected: Pick<GeneratorBundle, "sha256" | "sizeBytes">,
  effectiveUserId: number,
  effectiveGroupId: number,
): void {
  const observed = readOwnedBundle(
    path,
    effectiveUserId,
    effectiveGroupId,
    0o400,
  );
  if (
    observed.sha256 !== expected.sha256 ||
    observed.sizeBytes !== expected.sizeBytes
  ) {
    throw new FormalDockerRefusal("generator_bundle_invalid");
  }
}

/** root-owned空configでDocker credential/helper/contextのambient差替えを防ぐ。 */
export function requireRootOwnedDockerConfigDirectory(): void {
  let descriptor = -1;
  try {
    for (const ancestor of ["/", "/etc", "/etc/gs-loadtest"]) {
      const status = lstatSync(ancestor, { bigint: true });
      if (
        realpathSync.native(ancestor) !== ancestor ||
        !status.isDirectory() ||
        status.uid !== 0n ||
        status.gid !== 0n ||
        (permissionBits(status.mode) & BigInt(SHARED_WRITE_BITS)) !== 0n
      ) {
        throw new FormalDockerRefusal("generator_docker_config_invalid");
      }
    }
    const before = lstatSync(DOCKER_CONFIG_DIRECTORY, { bigint: true });
    descriptor = openSync(
      DOCKER_CONFIG_DIRECTORY,
      constants.O_RDONLY |
        requiredOpenFlag("O_DIRECTORY") |
        requiredOpenFlag("O_NOFOLLOW"),
    );
    const opened = fstatSync(descriptor, { bigint: true });
    if (
      realpathSync.native(DOCKER_CONFIG_DIRECTORY) !== DOCKER_CONFIG_DIRECTORY ||
      !opened.isDirectory() ||
      before.dev !== opened.dev ||
      before.ino !== opened.ino ||
      permissionBits(opened.mode) !== 0o755n ||
      opened.uid !== 0n ||
      opened.gid !== 0n ||
      readdirSync(`/proc/self/fd/${descriptor}`).length !== 0
    ) {
      throw new FormalDockerRefusal("generator_docker_config_invalid");
    }
  } catch (error: unknown) {
    if (error instanceof FormalDockerRefusal) {
      throw error;
    }
    throw new FormalDockerRefusal("generator_docker_config_invalid");
  } finally {
    closeDescriptor(descriptor);
  }
}

export function requireNonRootIdentity(
  effectiveUserId: number | undefined,
  effectiveGroupId: number | undefined,
): { readonly userId: number; readonly groupId: number } {
  if (
    effectiveUserId === undefined ||
    effectiveGroupId === undefined ||
    !Number.isSafeInteger(effectiveUserId) ||
    !Number.isSafeInteger(effectiveGroupId) ||
    effectiveUserId <= 0 ||
    effectiveGroupId <= 0
  ) {
    throw new FormalDockerRefusal("generator_user_invalid");
  }
  return { userId: effectiveUserId, groupId: effectiveGroupId };
}

export function requireApprovedImage(image: string): void {
  if (image !== APPROVED_FORMAL_K6_IMAGE) {
    throw new FormalDockerRefusal("generator_image_invalid");
  }
}

/** deterministic nameのcontainerだけを所有label照合後に回収する。 */
export class FormalContainerCustodian {
  readonly #binding: FormalContainerBinding;
  readonly #execute: (arguments_: readonly string[]) => DockerCommandResult;

  constructor(
    binding: FormalContainerBinding,
    execute: (arguments_: readonly string[]) => DockerCommandResult =
      executeDockerControlCommand,
  ) {
    this.#binding = binding;
    this.#execute = execute;
  }

  assertAbsent(): void {
    if (this.#listContainerIds().length !== 0) {
      throw new FormalDockerRefusal("generator_container_conflict");
    }
  }

  cleanupAndAssertAbsent(): void {
    const ids = this.#listContainerIds();
    if (ids.length === 0) {
      return;
    }
    const state = this.#inspectOwnedContainer();
    if (state.id !== ids[0]) {
      throw new FormalDockerRefusal("generator_container_cleanup_failed");
    }
    if (state.isRunning) {
      this.#requireMutationSuccess(["container", "stop", "--time=5", state.id]);
    }
    if (this.#listContainerIds().length === 0) {
      return;
    }
    const afterStop = this.#inspectOwnedContainer();
    if (afterStop.isRunning) {
      this.#requireMutationSuccess(["container", "kill", afterStop.id]);
    }
    if (this.#listContainerIds().length === 0) {
      return;
    }
    const beforeRemoval = this.#inspectOwnedContainer();
    if (beforeRemoval.isRunning) {
      throw new FormalDockerRefusal("generator_container_cleanup_failed");
    }
    this.#requireMutationSuccess(["container", "rm", beforeRemoval.id]);
    if (this.#listContainerIds().length !== 0) {
      throw new FormalDockerRefusal("generator_container_cleanup_failed");
    }
  }

  #listContainerIds(): readonly string[] {
    const result = this.#execute([
      "container",
      "ls",
      "--all",
      "--quiet",
      "--no-trunc",
      "--filter",
      `name=^/${this.#binding.name}$`,
    ]);
    if (result.status !== 0) {
      throw new FormalDockerRefusal("generator_container_cleanup_failed");
    }
    const lines = result.stdout.split("\n").filter((line) => line.length !== 0);
    if (
      lines.length > 1 ||
      lines.some((line) => !CONTAINER_ID_PATTERN.test(line))
    ) {
      throw new FormalDockerRefusal("generator_container_cleanup_failed");
    }
    return lines;
  }

  #inspectOwnedContainer(): ContainerState {
    const result = this.#execute([
      "container",
      "inspect",
      "--format",
      CONTAINER_INSPECT_FORMAT,
      this.#binding.name,
    ]);
    if (result.status !== 0) {
      throw new FormalDockerRefusal("generator_container_cleanup_failed");
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(result.stdout);
    } catch {
      throw new FormalDockerRefusal("generator_container_cleanup_failed");
    }
    if (!isOwnedContainerState(parsed, this.#binding)) {
      throw new FormalDockerRefusal("generator_container_cleanup_failed");
    }
    return { id: parsed.Id, isRunning: parsed.Running };
  }

  #requireMutationSuccess(arguments_: readonly string[]): void {
    const result = this.#execute(arguments_);
    if (result.status !== 0) {
      throw new FormalDockerRefusal("generator_container_cleanup_failed");
    }
  }
}

export interface DockerCommandResult {
  readonly status: number;
  readonly stdout: string;
}

function executeDockerControlCommand(
  arguments_: readonly string[],
): DockerCommandResult {
  requireRootOwnedDockerConfigDirectory();
  const options: SpawnSyncOptionsWithStringEncoding = {
    encoding: "utf8",
    env: DOCKER_CONTROL_ENVIRONMENT,
    maxBuffer: DOCKER_OUTPUT_BYTES,
    shell: false,
    stdio: ["ignore", "pipe", "pipe"],
    timeout: FORMAL_DOCKER_CONTROL_TIMEOUT_MILLISECONDS,
    windowsHide: true,
  };
  const result = spawnSync(
    DOCKER_COMMAND,
    ["--config", DOCKER_CONFIG_DIRECTORY, "--host", DOCKER_ENDPOINT, ...arguments_],
    options,
  );
  if (result.error !== undefined || result.status === null) {
    throw new FormalDockerRefusal("generator_container_cleanup_failed");
  }
  return { status: result.status, stdout: result.stdout };
}

function validateLauncherInvocation(
  command: string,
  arguments_: readonly string[],
  options: K6SpawnOptions,
  configuration: FormalDockerSpawnConfiguration,
): void {
  const expectedArguments = [
    ...K6_FIXED_RUN_ARGUMENTS,
    configuration.hostScriptPath,
  ];
  const expectedEnvironment = buildK6ChildEnvironment(
    configuration.loadedManifest,
  );
  if (
    command !== K6_COMMAND ||
    arguments_.length !== expectedArguments.length ||
    arguments_.some((value, index) => value !== expectedArguments[index]) ||
    options.cwd !== configuration.repositoryRoot ||
    options.shell !== false ||
    options.windowsHide !== true ||
    options.stdio.join(",") !== "ignore,pipe,ignore" ||
    !sameEnvironment(options.env, expectedEnvironment)
  ) {
    throw new LauncherRefusal("launcher_child_spawn_failed");
  }
}

function sameEnvironment(
  actual: NodeJS.ProcessEnv,
  expected: NodeJS.ProcessEnv,
): boolean {
  const actualKeys = Object.keys(actual).sort();
  if (
    actualKeys.length !== RUNTIME_ENVIRONMENT_KEYS.length ||
    actualKeys.some((key, index) => key !== RUNTIME_ENVIRONMENT_KEYS[index])
  ) {
    return false;
  }
  return RUNTIME_ENVIRONMENT_KEYS.every(
    (key) => actual[key] === expected[key] && typeof actual[key] === "string",
  );
}

function readOwnedBundle(
  path: string,
  effectiveUserId: number,
  effectiveGroupId: number,
  exactMode: number | null,
): GeneratorBundle {
  let descriptor = -1;
  try {
    const before = lstatSync(path, { bigint: true });
    if (
      realpathSync.native(path) !== path ||
      !before.isFile() ||
      before.nlink !== 1n ||
      before.uid !== BigInt(effectiveUserId) ||
      before.gid !== BigInt(effectiveGroupId) ||
      before.size < 1n ||
      before.size > BigInt(MAX_BUNDLE_BYTES) ||
      (exactMode === null
        ? (permissionBits(before.mode) & BigInt(SHARED_WRITE_BITS)) !== 0n
        : permissionBits(before.mode) !== BigInt(exactMode))
    ) {
      throw new FormalDockerRefusal("generator_bundle_invalid");
    }
    descriptor = openSync(
      path,
      constants.O_RDONLY | requiredOpenFlag("O_NOFOLLOW"),
    );
    const opened = fstatSync(descriptor, { bigint: true });
    if (
      opened.dev !== before.dev ||
      opened.ino !== before.ino ||
      opened.mode !== before.mode ||
      opened.uid !== before.uid ||
      opened.gid !== before.gid ||
      opened.nlink !== before.nlink ||
      opened.size !== before.size ||
      opened.mtimeNs !== before.mtimeNs ||
      opened.ctimeNs !== before.ctimeNs
    ) {
      throw new FormalDockerRefusal("generator_bundle_invalid");
    }
    const body = Buffer.alloc(Number(opened.size));
    let offset = 0;
    while (offset < body.byteLength) {
      const count = readSync(
        descriptor,
        body,
        offset,
        body.byteLength - offset,
        offset,
      );
      if (count <= 0) {
        throw new FormalDockerRefusal("generator_bundle_invalid");
      }
      offset += count;
    }
    const after = fstatSync(descriptor, { bigint: true });
    if (
      after.size !== opened.size ||
      after.mtimeNs !== opened.mtimeNs ||
      after.ctimeNs !== opened.ctimeNs ||
      after.nlink !== opened.nlink
    ) {
      throw new FormalDockerRefusal("generator_bundle_invalid");
    }
    return {
      body,
      sha256: createHash("sha256").update(body).digest("hex"),
      sizeBytes: body.byteLength,
    };
  } catch (error: unknown) {
    if (error instanceof FormalDockerRefusal) {
      throw error;
    }
    throw new FormalDockerRefusal("generator_bundle_invalid");
  } finally {
    closeDescriptor(descriptor);
  }
}

interface ContainerState {
  readonly id: string;
  readonly isRunning: boolean;
}

interface ContainerInspectRecord {
  readonly EventId: string;
  readonly Id: string;
  readonly Image: string;
  readonly ManifestSha256: string;
  readonly Name: string;
  readonly RunId: string;
  readonly Running: boolean;
}

function isOwnedContainerState(
  value: unknown,
  binding: FormalContainerBinding,
): value is ContainerInspectRecord {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const record = value as Record<string, unknown>;
  return (
    Object.keys(record).sort().join(",") ===
      "EventId,Id,Image,ManifestSha256,Name,RunId,Running" &&
    record["EventId"] === binding.eventId &&
    typeof record["Id"] === "string" &&
    CONTAINER_ID_PATTERN.test(record["Id"]) &&
    record["Image"] === binding.image &&
    record["ManifestSha256"] === binding.manifestSha256 &&
    record["Name"] === `/${binding.name}` &&
    record["RunId"] === binding.runId &&
    typeof record["Running"] === "boolean"
  );
}

function requireContainerName(value: string): void {
  if (!CONTAINER_NAME_PATTERN.test(value)) {
    throw new FormalDockerRefusal("generator_container_conflict");
  }
}

function permissionBits(mode: bigint): bigint {
  return mode & 0o7777n;
}

function requiredOpenFlag(name: "O_DIRECTORY" | "O_NOFOLLOW"): number {
  const value: unknown = Reflect.get(constants, name);
  if (typeof value !== "number" || value === 0) {
    throw new FormalDockerRefusal("generator_bundle_invalid");
  }
  return value;
}

function closeDescriptor(descriptor: number): void {
  if (descriptor < 0) {
    return;
  }
  try {
    closeSync(descriptor);
  } catch {
    // close error詳細を出さず、先行する固定codeを保つ。
  }
}
