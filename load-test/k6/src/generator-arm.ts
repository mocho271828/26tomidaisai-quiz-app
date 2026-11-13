import { createHash } from "node:crypto";
import {
  closeSync,
  constants,
  fchmodSync,
  fstatSync,
  fsyncSync,
  lstatSync,
  mkdirSync,
  openSync,
  readdirSync,
  realpathSync,
  unlinkSync,
  writeSync,
} from "node:fs";
import type { BigIntStats } from "node:fs";
import { isAbsolute, join } from "node:path";

import type { ArtifactIdentity } from "./artifacts.js";
import { canonicalJson } from "./canonical-json.js";

const ARM_DIRECTORY_NAME = "gs-loadtest-k6";
const ARM_DIRECTORY_MODE = 0o700;
const ARM_FILE_MODE = 0o600;
const BUNDLE_FILE_MODE = 0o400;
const MAX_ARM_MARKER_BYTES = 4_096;
const MAX_GENERATOR_BUNDLE_BYTES = 16 * 1_024 * 1_024;
const SHARED_WRITE_BITS = 0o022;
const RUN_ID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}(?![\s\S])/u;
const ARM_FILE_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.arm\.json(?![\s\S])/u;
const CA_FILE_PATTERN = new RegExp(ARM_FILE_PATTERN.source.replace("arm\\.json", "ca\\.pem"), "u");
const BUNDLE_FILE_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.generator\.js(?![\s\S])/u;

export type GeneratorArmRefusalCode =
  | "generator_already_armed"
  | "generator_arm_directory_invalid"
  | "generator_arm_write_failed";

export class GeneratorArmRefusal extends Error {
  readonly code: GeneratorArmRefusalCode;

  constructor(code: GeneratorArmRefusalCode) {
    super(code);
    this.name = "GeneratorArmRefusal";
    this.code = code;
  }
}

export interface GeneratorArmBinding {
  readonly format: "load-test-generator-arm-v1";
  readonly run_id: string;
  readonly manifest_sha256: string;
  readonly generator_image: string;
  readonly generator_bundle_sha256: string;
  readonly generator_bundle_size_bytes: number;
  readonly scheduled_start_at: string;
  readonly container_name: string;
}

export interface GeneratorArmOptions {
  readonly runtimeBaseDirectory: string;
  readonly effectiveUserId: number;
  readonly effectiveGroupId: number;
  readonly platform: NodeJS.Platform;
}

export interface GeneratorArmArtifacts {
  readonly marker: ArtifactIdentity;
  readonly bundle: ArtifactIdentity;
  /** Docker bind sourceにだけ使い、CLI resultやerrorには出さない。 */
  readonly bundlePath: string;
}

/** Linux user runtime配下にrun IDごと1度だけのarm markerを永続化する。 */
export function sealGeneratorArmMarker(
  binding: GeneratorArmBinding,
  bundleBody: Uint8Array,
  options: GeneratorArmOptions,
): GeneratorArmArtifacts {
  if (!RUN_ID_PATTERN.test(binding.run_id)) {
    throw new GeneratorArmRefusal("generator_arm_write_failed");
  }
  const armDirectory = ensureArmDirectory(options);
  const markerName = `${binding.run_id}.arm.json`;
  const bundleName = `${binding.run_id}.generator.js`;
  const body = Buffer.from(`${canonicalJson(binding)}\n`, "utf8");
  const bundle = Buffer.from(bundleBody);
  const bundleSha256 = createHash("sha256").update(bundle).digest("hex");
  if (
    body.byteLength < 1 ||
    body.byteLength > MAX_ARM_MARKER_BYTES ||
    bundle.byteLength < 1 ||
    bundle.byteLength > MAX_GENERATOR_BUNDLE_BYTES ||
    bundle.byteLength !== binding.generator_bundle_size_bytes ||
    bundleSha256 !== binding.generator_bundle_sha256
  ) {
    throw new GeneratorArmRefusal("generator_arm_write_failed");
  }

  const bundleArtifact = sealArmFile({
    armDirectory,
    body: bundle,
    effectiveGroupId: options.effectiveGroupId,
    effectiveUserId: options.effectiveUserId,
    mode: BUNDLE_FILE_MODE,
    name: bundleName,
    platform: options.platform,
  });
  const markerArtifact = sealArmFile({
    armDirectory,
    body,
    effectiveGroupId: options.effectiveGroupId,
    effectiveUserId: options.effectiveUserId,
    mode: ARM_FILE_MODE,
    name: markerName,
    platform: options.platform,
  });
  return {
    marker: markerArtifact,
    bundle: bundleArtifact,
    bundlePath: join(armDirectory, bundleName),
  };
}

interface SealArmFileOptions {
  readonly armDirectory: string;
  readonly body: Uint8Array;
  readonly effectiveUserId: number;
  readonly effectiveGroupId: number;
  readonly mode: number;
  readonly name: string;
  readonly platform: NodeJS.Platform;
}

function sealArmFile(options: SealArmFileOptions): ArtifactIdentity {

  let directoryDescriptor = -1;
  let markerDescriptor = -1;
  let created = false;
  let target = join(options.armDirectory, options.name);
  try {
    directoryDescriptor = openSync(
      options.armDirectory,
      constants.O_RDONLY |
        requiredOpenFlag("O_DIRECTORY") |
        requiredOpenFlag("O_NOFOLLOW"),
    );
    const openedDirectory = fstatSync(directoryDescriptor, { bigint: true });
    validateOwnedDirectory(
      openedDirectory,
      options.effectiveUserId,
      options.effectiveGroupId,
    );
    target =
      options.platform === "linux"
        ? `/proc/self/fd/${directoryDescriptor}/${options.name}`
        : target;
    markerDescriptor = openSync(
      target,
      constants.O_WRONLY |
        constants.O_CREAT |
        constants.O_EXCL |
        requiredOpenFlag("O_NOFOLLOW"),
      options.mode,
    );
    created = true;
    fchmodSync(markerDescriptor, options.mode);
    writeAll(markerDescriptor, options.body);
    fsyncSync(markerDescriptor);
    const markerStatus = fstatSync(markerDescriptor, { bigint: true });
    if (
      !markerStatus.isFile() ||
      permissionBits(markerStatus.mode) !== BigInt(options.mode) ||
      markerStatus.uid !== BigInt(options.effectiveUserId) ||
      markerStatus.gid !== BigInt(options.effectiveGroupId) ||
      markerStatus.nlink !== 1n ||
      markerStatus.size !== BigInt(options.body.byteLength)
    ) {
      throw new GeneratorArmRefusal("generator_arm_write_failed");
    }
    closeSync(markerDescriptor);
    markerDescriptor = -1;
    fsyncSync(directoryDescriptor);
    return {
      name: options.name,
      sha256: createHash("sha256").update(options.body).digest("hex"),
      size_bytes: options.body.byteLength,
    };
  } catch (error: unknown) {
    if (created) {
      try {
        unlinkSync(target);
        if (directoryDescriptor >= 0) {
          fsyncSync(directoryDescriptor);
        }
      } catch {
        // partial markerは自動で安全に回収できないため、原因は固定codeへ閉じる。
      }
    }
    if (isErrnoCode(error, "EEXIST")) {
      throw new GeneratorArmRefusal("generator_already_armed");
    }
    if (error instanceof GeneratorArmRefusal) {
      throw error;
    }
    throw new GeneratorArmRefusal("generator_arm_write_failed");
  } finally {
    closeDescriptor(markerDescriptor);
    closeDescriptor(directoryDescriptor);
  }
}

/** ambient HOME/XDGに依存しないsystemd user runtimeを導出する。 */
export function productionGeneratorRuntimeBaseDirectory(
  effectiveUserId: number,
  effectiveGroupId: number,
): string {
  const runtimeBase = `/run/user/${effectiveUserId}`;
  try {
    validateRootAncestor("/run");
    validateRootAncestor("/run/user");
    const status = lstatSync(runtimeBase, { bigint: true });
    if (
      realpathSync.native(runtimeBase) !== runtimeBase ||
      !status.isDirectory() ||
      permissionBits(status.mode) !== BigInt(ARM_DIRECTORY_MODE) ||
      status.uid !== BigInt(effectiveUserId) ||
      status.gid !== BigInt(effectiveGroupId)
    ) {
      throw new GeneratorArmRefusal("generator_arm_directory_invalid");
    }
    return runtimeBase;
  } catch (error: unknown) {
    if (error instanceof GeneratorArmRefusal) {
      throw error;
    }
    throw new GeneratorArmRefusal("generator_arm_directory_invalid");
  }
}

function ensureArmDirectory(options: GeneratorArmOptions): string {
  const runtimeBase = options.runtimeBaseDirectory;
  if (!isAbsolute(runtimeBase)) {
    throw new GeneratorArmRefusal("generator_arm_directory_invalid");
  }
  const armDirectory = join(runtimeBase, ARM_DIRECTORY_NAME);
  try {
    const baseStatus = lstatSync(runtimeBase, { bigint: true });
    validateOwnedDirectory(
      baseStatus,
      options.effectiveUserId,
      options.effectiveGroupId,
    );
    if (realpathSync.native(runtimeBase) !== runtimeBase) {
      throw new GeneratorArmRefusal("generator_arm_directory_invalid");
    }
    try {
      mkdirSync(armDirectory, { mode: ARM_DIRECTORY_MODE });
    } catch (error: unknown) {
      if (!isErrnoCode(error, "EEXIST")) {
        throw error;
      }
    }
    const armStatus = lstatSync(armDirectory, { bigint: true });
    validateOwnedDirectory(
      armStatus,
      options.effectiveUserId,
      options.effectiveGroupId,
    );
    if (realpathSync.native(armDirectory) !== armDirectory) {
      throw new GeneratorArmRefusal("generator_arm_directory_invalid");
    }
    validateArmInventory(
      armDirectory,
      options.effectiveUserId,
      options.effectiveGroupId,
    );
    return armDirectory;
  } catch (error: unknown) {
    if (error instanceof GeneratorArmRefusal) {
      throw error;
    }
    throw new GeneratorArmRefusal("generator_arm_directory_invalid");
  }
}

function validateOwnedDirectory(
  status: BigIntStats,
  effectiveUserId: number,
  effectiveGroupId: number,
): void {
  if (
    !status.isDirectory() ||
    permissionBits(status.mode) !== BigInt(ARM_DIRECTORY_MODE) ||
    status.uid !== BigInt(effectiveUserId) ||
    status.gid !== BigInt(effectiveGroupId)
  ) {
    throw new GeneratorArmRefusal("generator_arm_directory_invalid");
  }
}

function validateArmInventory(
  armDirectory: string,
  effectiveUserId: number,
  effectiveGroupId: number,
): void {
  for (const name of readdirSync(armDirectory)) {
    const expectedMode = ARM_FILE_PATTERN.test(name) || CA_FILE_PATTERN.test(name)
      ? ARM_FILE_MODE
      : BUNDLE_FILE_PATTERN.test(name)
        ? BUNDLE_FILE_MODE
        : null;
    const maximumBytes = ARM_FILE_PATTERN.test(name)
      ? MAX_ARM_MARKER_BYTES
      : CA_FILE_PATTERN.test(name) ? 65_536 : MAX_GENERATOR_BUNDLE_BYTES;
    if (expectedMode === null) {
      throw new GeneratorArmRefusal("generator_arm_directory_invalid");
    }
    const status = lstatSync(join(armDirectory, name), { bigint: true });
    if (
      !status.isFile() ||
      permissionBits(status.mode) !== BigInt(expectedMode) ||
      status.uid !== BigInt(effectiveUserId) ||
      status.gid !== BigInt(effectiveGroupId) ||
      status.nlink !== 1n ||
      status.size < 1n ||
      status.size > BigInt(maximumBytes)
    ) {
      throw new GeneratorArmRefusal("generator_arm_directory_invalid");
    }
  }
}

function validateRootAncestor(path: string): void {
  const status = lstatSync(path, { bigint: true });
  if (
    realpathSync.native(path) !== path ||
    !status.isDirectory() ||
    status.uid !== 0n ||
    status.gid !== 0n ||
    (permissionBits(status.mode) & BigInt(SHARED_WRITE_BITS)) !== 0n
  ) {
    throw new GeneratorArmRefusal("generator_arm_directory_invalid");
  }
}

function writeAll(descriptor: number, body: Uint8Array): void {
  let offset = 0;
  while (offset < body.byteLength) {
    const written = writeSync(
      descriptor,
      body,
      offset,
      body.byteLength - offset,
      offset,
    );
    if (written <= 0) {
      throw new GeneratorArmRefusal("generator_arm_write_failed");
    }
    offset += written;
  }
}

function permissionBits(mode: bigint): bigint {
  return mode & 0o7777n;
}

function requiredOpenFlag(name: "O_DIRECTORY" | "O_NOFOLLOW"): number {
  const value: unknown = Reflect.get(constants, name);
  if (typeof value !== "number" || value === 0) {
    throw new GeneratorArmRefusal("generator_arm_write_failed");
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
    // close失敗は先行する固定失敗codeを置き換えない。
  }
}

function isErrnoCode(error: unknown, code: string): boolean {
  return (
    error !== null &&
    typeof error === "object" &&
    Reflect.get(error, "code") === code
  );
}
