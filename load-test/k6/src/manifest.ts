import {
  closeSync,
  constants,
  existsSync,
  fstatSync,
  lstatSync,
  openSync,
  readSync,
  realpathSync,
} from "node:fs";
import type { Stats } from "node:fs";
import {
  basename,
  dirname,
  isAbsolute,
  join,
  parse,
  relative,
  resolve,
  sep,
} from "node:path";
import { fileURLToPath } from "node:url";

import { canonicalSha256 } from "./canonical-json.js";
import {
  LoadTestManifestSchema,
  stringHasLoneSurrogate,
  type LoadTestManifest,
} from "./contracts.js";

const MAX_MANIFEST_BYTES = 1_000_000;
const MANIFEST_FILE_MODE = 0o600;
const SHARED_WRITE_BITS = 0o022;

export type ManifestRefusalCode =
  | "manifest_path_invalid"
  | "manifest_parent_invalid"
  | "manifest_file_invalid"
  | "manifest_json_invalid"
  | "manifest_contract_invalid";

export class ManifestRefusal extends Error {
  public readonly code: ManifestRefusalCode;

  public constructor(code: ManifestRefusalCode) {
    super(code);
    this.name = "ManifestRefusal";
    this.code = code;
  }
}

export interface ManifestHashes {
  readonly manifestSha256: string;
  readonly scenarioSha256: string;
}

export interface LoadedManifest {
  readonly manifest: LoadTestManifest;
  readonly hashes: ManifestHashes;
}

const MODULE_SENTINELS = [
  join("load-test", "k6", "src", "manifest.ts"),
  join("load-test", "k6", "dist", "launcher.js"),
  join("load-test", "k6", "dist", "generator-cli.js"),
];

const ROOT_SENTINELS = [
  "compose.yaml",
  join("backend", "pyproject.toml"),
  join("load-test", "k6", "package.json"),
];

function isWithin(path: string, parent: string): boolean {
  const difference = relative(parent, path);
  return difference === "" || (!difference.startsWith(`..${sep}`) && difference !== "..");
}

/** caller入力を使わず、実行moduleとtracked sentinelからrepository rootを導出する。 */
export function runtimeRepositoryRoot(): string {
  const modulePath = realpathSync(fileURLToPath(import.meta.url));
  for (const moduleFile of MODULE_SENTINELS) {
    let candidate = dirname(modulePath);
    while (true) {
      const moduleSentinel = join(candidate, moduleFile);
      if (
        isRegularFile(moduleSentinel) &&
        realpathSync(moduleSentinel) === modulePath &&
        ROOT_SENTINELS.every((sentinel) => isRegularFile(join(candidate, sentinel)))
      ) {
        return realpathSync(candidate);
      }
      const parent = dirname(candidate);
      if (parent === candidate) {
        break;
      }
      candidate = parent;
    }
  }
  throw new ManifestRefusal("manifest_path_invalid");
}

function isRegularFile(path: string): boolean {
  if (!existsSync(path)) {
    return false;
  }
  return lstatSync(path).isFile();
}

function validateLexicalAncestors(path: string): void {
  const parsed = parse(path);
  let current = parsed.root;
  const segments = path.slice(parsed.root.length).split(sep).filter(Boolean);
  for (const segment of segments) {
    current = join(current, segment);
    const status = lstatSync(current);
    if (status.isSymbolicLink()) {
      throw new ManifestRefusal("manifest_path_invalid");
    }
  }
}

function openValidatedParent(manifestPath: string): number {
  const parent = dirname(manifestPath);
  validateLexicalAncestors(parent);
  const parentStatus = lstatSync(parent, { bigint: true });
  const effectiveUser = effectiveUserId();
  const effectiveGroup = effectiveGroupId();
  if (
    !parentStatus.isDirectory() ||
    parentStatus.uid !== effectiveUser ||
    parentStatus.gid !== effectiveGroup ||
    (parentStatus.mode & BigInt(SHARED_WRITE_BITS)) !== 0n
  ) {
    throw new ManifestRefusal("manifest_parent_invalid");
  }
  const resolvedParent = realpathSync(parent);
  const resolvedRepository = runtimeRepositoryRoot();
  if (isWithin(resolvedParent, resolvedRepository)) {
    throw new ManifestRefusal("manifest_parent_invalid");
  }

  let descriptor = -1;
  try {
    descriptor = openSync(
      resolvedParent,
      constants.O_RDONLY |
        requiredOpenFlag("O_DIRECTORY") |
        requiredOpenFlag("O_NOFOLLOW"),
    );
    const opened = fstatSync(descriptor, { bigint: true });
    if (
      !opened.isDirectory() ||
      opened.dev !== parentStatus.dev ||
      opened.ino !== parentStatus.ino ||
      opened.mode !== parentStatus.mode ||
      opened.uid !== effectiveUser ||
      opened.gid !== effectiveGroup ||
      (opened.mode & BigInt(SHARED_WRITE_BITS)) !== 0n
    ) {
      throw new ManifestRefusal("manifest_parent_invalid");
    }
    return descriptor;
  } catch (error: unknown) {
    closeDescriptor(descriptor);
    if (error instanceof ManifestRefusal) {
      throw error;
    }
    throw new ManifestRefusal("manifest_parent_invalid");
  }
}

function validateFileStatus(status: Stats): void {
  const effectiveUser = process.geteuid?.();
  const effectiveGroup = process.getegid?.();
  if (
    !status.isFile() ||
    effectiveUser === undefined ||
    effectiveGroup === undefined ||
    status.uid !== effectiveUser ||
    status.gid !== effectiveGroup ||
    status.nlink !== 1 ||
    (status.mode & 0o7777) !== MANIFEST_FILE_MODE ||
    status.size < 1 ||
    status.size > MAX_MANIFEST_BYTES
  ) {
    throw new ManifestRefusal("manifest_file_invalid");
  }
}

function readValidatedFile(manifestPath: string): Uint8Array {
  const before = lstatSync(manifestPath);
  const beforeIdentity = lstatSync(manifestPath, { bigint: true });
  validateFileStatus(before);
  const descriptor = openSync(
    manifestPath,
    constants.O_RDONLY | requiredOpenFlag("O_NOFOLLOW"),
  );
  try {
    const opened = fstatSync(descriptor);
    const openedIdentity = fstatSync(descriptor, { bigint: true });
    validateFileStatus(opened);
    if (
      beforeIdentity.dev !== openedIdentity.dev ||
      beforeIdentity.ino !== openedIdentity.ino ||
      beforeIdentity.mode !== openedIdentity.mode ||
      beforeIdentity.uid !== openedIdentity.uid ||
      beforeIdentity.gid !== openedIdentity.gid ||
      beforeIdentity.nlink !== openedIdentity.nlink ||
      beforeIdentity.size !== openedIdentity.size ||
      beforeIdentity.mtimeNs !== openedIdentity.mtimeNs ||
      beforeIdentity.ctimeNs !== openedIdentity.ctimeNs
    ) {
      throw new ManifestRefusal("manifest_file_invalid");
    }
    const body = Buffer.alloc(opened.size);
    let offset = 0;
    while (offset < body.length) {
      const count = readSync(descriptor, body, offset, body.length - offset, offset);
      if (count === 0) {
        throw new ManifestRefusal("manifest_file_invalid");
      }
      offset += count;
    }
    const after = fstatSync(descriptor);
    const afterIdentity = fstatSync(descriptor, { bigint: true });
    validateFileStatus(after);
    if (
      afterIdentity.dev !== openedIdentity.dev ||
      afterIdentity.ino !== openedIdentity.ino ||
      afterIdentity.mode !== openedIdentity.mode ||
      afterIdentity.uid !== openedIdentity.uid ||
      afterIdentity.gid !== openedIdentity.gid ||
      afterIdentity.size !== openedIdentity.size ||
      afterIdentity.mtimeNs !== openedIdentity.mtimeNs ||
      afterIdentity.ctimeNs !== openedIdentity.ctimeNs ||
      afterIdentity.nlink !== openedIdentity.nlink
    ) {
      throw new ManifestRefusal("manifest_file_invalid");
    }
    return body;
  } finally {
    closeSync(descriptor);
  }
}

function decodeManifest(body: Uint8Array): unknown {
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(body);
  } catch {
    throw new ManifestRefusal("manifest_json_invalid");
  }
  if (text.startsWith("\ufeff") || text.includes("\0")) {
    throw new ManifestRefusal("manifest_json_invalid");
  }
  if (hasNonIntegerNumberToken(text)) {
    throw new ManifestRefusal("manifest_contract_invalid");
  }
  try {
    const parsed: unknown = JSON.parse(text);
    if (containsLoneSurrogate(parsed)) {
      throw new ManifestRefusal("manifest_json_invalid");
    }
    return parsed;
  } catch {
    throw new ManifestRefusal("manifest_json_invalid");
  }
}

function deepFreeze<T>(value: T): T {
  if (value === null || typeof value !== "object" || Object.isFrozen(value)) {
    return value;
  }
  for (const nested of Object.values(value)) {
    deepFreeze(nested);
  }
  return Object.freeze(value);
}

function containsLoneSurrogate(value: unknown): boolean {
  if (typeof value === "string") {
    return stringHasLoneSurrogate(value);
  }
  if (Array.isArray(value)) {
    return value.some(containsLoneSurrogate);
  }
  if (value === null || typeof value !== "object") {
    return false;
  }
  return Object.entries(value).some(
    ([key, item]) => stringHasLoneSurrogate(key) || containsLoneSurrogate(item),
  );
}

/** JSON.parseが失う数値字句を先に検査し、Python StrictIntとの差を作らない。 */
function hasNonIntegerNumberToken(text: string): boolean {
  let isInsideString = false;
  let isEscaped = false;
  for (let index = 0; index < text.length; index += 1) {
    const character = text[index];
    if (isInsideString) {
      if (isEscaped) {
        isEscaped = false;
      } else if (character === "\\") {
        isEscaped = true;
      } else if (character === '"') {
        isInsideString = false;
      }
      continue;
    }
    if (character === '"') {
      isInsideString = true;
      continue;
    }
    if (character !== "-" && (character === undefined || !/[0-9]/.test(character))) {
      continue;
    }

    let cursor = character === "-" ? index + 1 : index;
    while (cursor < text.length && /[0-9]/.test(text[cursor] ?? "")) {
      cursor += 1;
    }
    const suffix = text[cursor];
    if (suffix === "." || suffix === "e" || suffix === "E") {
      return true;
    }
    index = cursor - 1;
  }
  return false;
}

/** repository外の所有済み0600通常fileだけをstrict manifestとして読む。 */
export function loadManifest(path: string): LoadedManifest {
  if (!isAbsolute(path)) {
    throw new ManifestRefusal("manifest_path_invalid");
  }
  const manifestPath = resolve(path);
  let parentDescriptor = -1;
  try {
    parentDescriptor = openValidatedParent(manifestPath);
    validateLexicalAncestors(manifestPath);
    const anchoredPath =
      process.platform === "linux"
        ? `/proc/self/fd/${parentDescriptor}/${basename(manifestPath)}`
        : manifestPath;
    const raw = decodeManifest(readValidatedFile(anchoredPath));
    const result = LoadTestManifestSchema.safeParse(raw);
    if (!result.success) {
      throw new ManifestRefusal("manifest_contract_invalid");
    }
    const manifest = deepFreeze(result.data);
    return Object.freeze({
      manifest,
      hashes: Object.freeze({
        manifestSha256: canonicalSha256(manifest),
        scenarioSha256: canonicalSha256(manifest.scenario),
      }),
    });
  } catch (error: unknown) {
    if (error instanceof ManifestRefusal) {
      throw error;
    }
    throw new ManifestRefusal("manifest_file_invalid");
  } finally {
    closeDescriptor(parentDescriptor);
  }
}

function requiredOpenFlag(name: "O_DIRECTORY" | "O_NOFOLLOW"): number {
  const value: unknown = Reflect.get(constants, name);
  if (typeof value !== "number" || value === 0) {
    throw new ManifestRefusal("manifest_file_invalid");
  }
  return value;
}

function effectiveUserId(): bigint {
  if (typeof process.geteuid !== "function") {
    throw new ManifestRefusal("manifest_parent_invalid");
  }
  return BigInt(process.geteuid());
}

function effectiveGroupId(): bigint {
  if (typeof process.getegid !== "function") {
    throw new ManifestRefusal("manifest_parent_invalid");
  }
  return BigInt(process.getegid());
}

function closeDescriptor(descriptor: number): void {
  if (descriptor < 0) {
    return;
  }
  try {
    closeSync(descriptor);
  } catch {
    // close error本文はpath情報を含み得るため外へ出さない。
  }
}
