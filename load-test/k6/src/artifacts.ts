import { createHash } from "node:crypto";
import {
  closeSync,
  constants,
  fchmodSync,
  fstatSync,
  fsyncSync,
  lstatSync,
  openSync,
  readSync,
  realpathSync,
  unlinkSync,
  writeSync,
} from "node:fs";
import { dirname, isAbsolute, join, normalize, relative, sep } from "node:path";

import { runtimeRepositoryRoot } from "./manifest.js";

export const MAX_INPUT_ARTIFACT_BYTES = 64 * 1024 * 1024;
export const MAX_OUTPUT_ARTIFACT_BYTES = 16 * 1024 * 1024;

const ARTIFACT_NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,199}(?![\s\S])/u;

export type ArtifactRefusalCode =
  | "artifact_collision"
  | "artifact_directory_invalid"
  | "artifact_directory_must_be_external"
  | "artifact_file_invalid"
  | "artifact_input_changed"
  | "artifact_input_invalid"
  | "artifact_name_invalid"
  | "artifact_path_invalid"
  | "artifact_read_failed"
  | "artifact_runtime_root_invalid"
  | "artifact_size_invalid"
  | "artifact_write_failed";

export class ArtifactRefusal extends Error {
  readonly code: ArtifactRefusalCode;

  constructor(code: ArtifactRefusalCode) {
    super(code);
    this.name = "ArtifactRefusal";
    this.code = code;
  }
}

export interface ArtifactIdentity {
  readonly name: string;
  readonly sha256: string;
  readonly size_bytes: number;
}

export interface LoadedArtifact {
  readonly body: Uint8Array;
  readonly artifact: ArtifactIdentity;
}

export interface ArtifactBoundary {
  readonly maximumBytes?: number;
}

export interface SealArtifactOptions extends ArtifactBoundary {
  readonly outputDirectory: string;
  readonly name: string;
  readonly body: Uint8Array;
}

/** launcher起動前にoutput境界だけを検証する。 */
export function validateSecureExternalDirectory(
  directory: string,
): void {
  const descriptor = openSecureExternalDirectory(directory);
  closeDescriptor(descriptor);
}

/** repository外のowner限定directoryからsingle-link 0600通常fileだけを読む。 */
export function readSecureExternalArtifact(
  filePath: string,
  boundary: ArtifactBoundary,
): LoadedArtifact {
  const maximumBytes = boundary.maximumBytes ?? MAX_INPUT_ARTIFACT_BYTES;
  requireBoundedPositiveSafeInteger(
    maximumBytes,
    MAX_INPUT_ARTIFACT_BYTES,
    "artifact_size_invalid",
  );
  if (
    !isAbsolute(filePath) ||
    hasParentSegment(filePath) ||
    !ARTIFACT_NAME_PATTERN.test(fileName(filePath))
  ) {
    throw new ArtifactRefusal("artifact_path_invalid");
  }

  const directory = dirname(filePath);
  let directoryDescriptor = -1;
  let descriptor = -1;
  try {
    directoryDescriptor = openSecureExternalDirectory(directory);
    const anchoredPath = directoryEntryPath(
      directoryDescriptor,
      fileName(filePath),
      filePath,
    );
    const before = lstatSync(anchoredPath, { bigint: true });
    descriptor = openSync(
      anchoredPath,
      constants.O_RDONLY | requiredOpenFlag("O_NOFOLLOW", "artifact_input_invalid"),
    );
    const opened = fstatSync(descriptor, { bigint: true });
    if (
      before.dev !== opened.dev ||
      before.ino !== opened.ino ||
      !opened.isFile() ||
      permissionBits(opened.mode) !== 0o600n ||
      opened.uid !== effectiveUserId() ||
      opened.gid !== effectiveGroupId() ||
      opened.nlink !== 1n ||
      opened.size < 1n ||
      opened.size > BigInt(maximumBytes)
    ) {
      throw new ArtifactRefusal("artifact_input_invalid");
    }

    const size = Number(opened.size);
    const body = Buffer.allocUnsafe(size);
    let offset = 0;
    while (offset < size) {
      const bytesRead = readSync(descriptor, body, offset, size - offset, offset);
      if (bytesRead <= 0) {
        throw new ArtifactRefusal("artifact_input_changed");
      }
      offset += bytesRead;
    }
    const trailingByte = Buffer.allocUnsafe(1);
    if (readSync(descriptor, trailingByte, 0, 1, offset) !== 0) {
      throw new ArtifactRefusal("artifact_input_changed");
    }

    const after = fstatSync(descriptor, { bigint: true });
    if (
      after.size !== opened.size ||
      after.mtimeNs !== opened.mtimeNs ||
      after.ctimeNs !== opened.ctimeNs ||
      after.nlink !== opened.nlink
    ) {
      throw new ArtifactRefusal("artifact_input_changed");
    }
    return {
      body,
      artifact: identityFor(fileName(filePath), body),
    };
  } catch (error: unknown) {
    if (error instanceof ArtifactRefusal) {
      throw error;
    }
    throw new ArtifactRefusal("artifact_read_failed");
  } finally {
    closeDescriptor(descriptor);
    closeDescriptor(directoryDescriptor);
  }
}

/** O_EXCL/0600/file fsync/directory fsyncでartifactを非上書きsealする。 */
export function sealArtifact(options: SealArtifactOptions): ArtifactIdentity {
  if (!ARTIFACT_NAME_PATTERN.test(options.name)) {
    throw new ArtifactRefusal("artifact_name_invalid");
  }
  const maximumBytes = options.maximumBytes ?? MAX_OUTPUT_ARTIFACT_BYTES;
  requireBoundedPositiveSafeInteger(
    maximumBytes,
    MAX_OUTPUT_ARTIFACT_BYTES,
    "artifact_size_invalid",
  );
  if (!(options.body instanceof Uint8Array)) {
    throw new ArtifactRefusal("artifact_size_invalid");
  }
  const body = Buffer.from(options.body);
  if (body.byteLength < 1 || body.byteLength > maximumBytes) {
    throw new ArtifactRefusal("artifact_size_invalid");
  }

  let directoryDescriptor = -1;
  let descriptor = -1;
  let created = false;
  let target = join(options.outputDirectory, options.name);
  try {
    directoryDescriptor = openSecureExternalDirectory(options.outputDirectory);
    target = directoryEntryPath(directoryDescriptor, options.name, target);
    descriptor = openSync(
      target,
      constants.O_WRONLY |
        constants.O_CREAT |
        constants.O_EXCL |
        requiredOpenFlag("O_NOFOLLOW", "artifact_write_failed"),
      0o600,
    );
    created = true;
    fchmodSync(descriptor, 0o600);

    let offset = 0;
    while (offset < body.byteLength) {
      const bytesWritten = writeSync(
        descriptor,
        body,
        offset,
        body.byteLength - offset,
        offset,
      );
      if (bytesWritten <= 0) {
        throw new ArtifactRefusal("artifact_write_failed");
      }
      offset += bytesWritten;
    }
    fsyncSync(descriptor);

    const written = fstatSync(descriptor, { bigint: true });
    if (
      !written.isFile() ||
      permissionBits(written.mode) !== 0o600n ||
      written.uid !== effectiveUserId() ||
      written.gid !== effectiveGroupId() ||
      written.nlink !== 1n ||
      written.size !== BigInt(body.byteLength)
    ) {
      throw new ArtifactRefusal("artifact_file_invalid");
    }
    closeSync(descriptor);
    descriptor = -1;
    fsyncSync(directoryDescriptor);
    return identityFor(options.name, body);
  } catch (error: unknown) {
    if (created && directoryDescriptor >= 0) {
      removeCreatedArtifact(target, directoryDescriptor);
    }
    if (isErrnoCode(error, "EEXIST")) {
      throw new ArtifactRefusal("artifact_collision");
    }
    if (error instanceof ArtifactRefusal) {
      throw error;
    }
    throw new ArtifactRefusal("artifact_write_failed");
  } finally {
    closeDescriptor(descriptor);
    closeDescriptor(directoryDescriptor);
  }
}

function openSecureExternalDirectory(directory: string): number {
  if (!isAbsolute(directory) || hasParentSegment(directory)) {
    throw new ArtifactRefusal("artifact_directory_invalid");
  }
  requireNoSymlinkAncestors(directory);

  let resolvedDirectory: string;
  let resolvedRepository: string;
  let directoryStatus;
  try {
    directoryStatus = lstatSync(directory, { bigint: true });
    resolvedDirectory = realpathSync.native(directory);
    resolvedRepository = runtimeRepositoryRoot();
  } catch {
    throw new ArtifactRefusal("artifact_directory_invalid");
  }
  if (isWithin(resolvedRepository, resolvedDirectory)) {
    throw new ArtifactRefusal("artifact_directory_must_be_external");
  }

  let descriptor = -1;
  try {
    descriptor = openSync(
      resolvedDirectory,
      constants.O_RDONLY |
        requiredOpenFlag("O_DIRECTORY", "artifact_directory_invalid") |
        requiredOpenFlag("O_NOFOLLOW", "artifact_directory_invalid"),
    );
    const status = fstatSync(descriptor, { bigint: true });
    if (
      !status.isDirectory() ||
      status.dev !== directoryStatus.dev ||
      status.ino !== directoryStatus.ino ||
      status.mode !== directoryStatus.mode ||
      permissionBits(status.mode) !== 0o700n ||
      status.uid !== effectiveUserId() ||
      status.gid !== effectiveGroupId() ||
      status.uid !== directoryStatus.uid ||
      status.gid !== directoryStatus.gid
    ) {
      throw new ArtifactRefusal("artifact_directory_invalid");
    }
    validateOpenedDirectoryLocation(descriptor, resolvedRepository);
    return descriptor;
  } catch (error: unknown) {
    closeDescriptor(descriptor);
    if (error instanceof ArtifactRefusal) {
      throw error;
    }
    throw new ArtifactRefusal("artifact_directory_invalid");
  }
}

function validateOpenedDirectoryLocation(
  descriptor: number,
  resolvedRepository: string,
): void {
  if (process.platform !== "linux") {
    return;
  }
  let openedDirectory: string;
  try {
    openedDirectory = realpathSync.native(`/proc/self/fd/${descriptor}`);
  } catch {
    throw new ArtifactRefusal("artifact_directory_invalid");
  }
  if (isWithin(resolvedRepository, openedDirectory)) {
    throw new ArtifactRefusal("artifact_directory_must_be_external");
  }
}

function requireNoSymlinkAncestors(directory: string): void {
  let current = normalize(directory);
  while (true) {
    let status;
    try {
      status = lstatSync(current);
    } catch {
      throw new ArtifactRefusal("artifact_directory_invalid");
    }
    if (status.isSymbolicLink() || !status.isDirectory()) {
      throw new ArtifactRefusal("artifact_directory_invalid");
    }
    const parent = dirname(current);
    if (parent === current) {
      return;
    }
    current = parent;
  }
}

function identityFor(name: string, body: Uint8Array): ArtifactIdentity {
  return {
    name,
    sha256: createHash("sha256").update(body).digest("hex"),
    size_bytes: body.byteLength,
  };
}

function directoryEntryPath(
  directoryDescriptor: number,
  name: string,
  fallback: string,
): string {
  // 正式runtimeはLinuxのため、rename後も検証済みdirectory descriptorへ固定する。
  return process.platform === "linux"
    ? `/proc/self/fd/${directoryDescriptor}/${name}`
    : fallback;
}

function removeCreatedArtifact(target: string, directoryDescriptor: number): void {
  try {
    unlinkSync(target);
    fsyncSync(directoryDescriptor);
  } catch {
    // cleanup失敗時も元の固定codeだけを外へ返す。
  }
}

function closeDescriptor(descriptor: number): void {
  if (descriptor < 0) {
    return;
  }
  try {
    closeSync(descriptor);
  } catch {
    // close失敗で秘密を含むOS error本文を外へ出さない。
  }
}

function fileName(filePath: string): string {
  const segments = filePath.split(sep);
  return segments.at(-1) ?? "";
}

function hasParentSegment(value: string): boolean {
  return value.split(/[\\/]/u).includes("..");
}

function isWithin(parent: string, candidate: string): boolean {
  const difference = relative(parent, candidate);
  return difference === "" || (!difference.startsWith(`..${sep}`) && difference !== "..");
}

function permissionBits(mode: bigint): bigint {
  return mode & 0o7777n;
}

function effectiveUserId(): bigint {
  if (typeof process.geteuid !== "function") {
    throw new ArtifactRefusal("artifact_directory_invalid");
  }
  return BigInt(process.geteuid());
}

function effectiveGroupId(): bigint {
  if (typeof process.getegid !== "function") {
    throw new ArtifactRefusal("artifact_directory_invalid");
  }
  return BigInt(process.getegid());
}

function requiredOpenFlag(
  name: "O_DIRECTORY" | "O_NOFOLLOW",
  code: ArtifactRefusalCode,
): number {
  const flag = constants[name];
  if (typeof flag !== "number" || flag === 0) {
    throw new ArtifactRefusal(code);
  }
  return flag;
}

function isErrnoCode(error: unknown, code: string): boolean {
  return (
    error instanceof Error &&
    "code" in error &&
    typeof error.code === "string" &&
    error.code === code
  );
}

function requireBoundedPositiveSafeInteger(
  value: number,
  maximum: number,
  code: ArtifactRefusalCode,
): void {
  if (!Number.isSafeInteger(value) || value < 1 || value > maximum) {
    throw new ArtifactRefusal(code);
  }
}
