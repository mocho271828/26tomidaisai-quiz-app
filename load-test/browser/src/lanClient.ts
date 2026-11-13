import { randomBytes, randomUUID, X509Certificate } from "node:crypto";
import { execFile } from "node:child_process";
import { constants } from "node:fs";
import { lstat, mkdir, open, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { promisify, parseArgs } from "node:util";
import { z } from "zod";
import { controlSettingsSchema, type ControlSettings } from "./controlPlan.js";
import { superviseControl } from "./controlSupervisor.js";
import { checkLanBrowser, checkLanReadiness } from "./lanChecks.js";

const execute = promisify(execFile);
export const LAN_BASE_URL = "https://10.0.1.2:8443";
export const lanServerSchema = z.object({
  sourceCommit: z.string().regex(/^[a-f0-9]{40}$/u),
  baseUrl: z.literal(LAN_BASE_URL),
  caSha256: z.string().regex(/^[a-fA-F0-9]{64}$/u).transform(value => value.toLowerCase()),
}).strict();
export type LanServer = z.infer<typeof lanServerSchema>;
const integerInput = z.string().regex(/^\d+$/u).transform(Number).pipe(z.number().int());
const lanOptionsSchema = z.object({
  durationSeconds: integerInput.pipe(z.number().min(180).max(3600)),
  manualParticipantCount: integerInput.pipe(z.number().min(0).max(100)),
}).strict();
export type LanOptions = z.infer<typeof lanOptionsSchema>;
export type LanAction = {kind: "check"} | {kind: "run"; vus: 50 | 600};
export type LanDependencies = {
  repositoryRoot: () => Promise<string>;
  gitHead: (root: string) => Promise<string>;
  readiness: (ca: string) => Promise<void>;
  browser: () => Promise<void>;
  supervise: (settings: ControlSettings, vus: number) => Promise<string>;
};

export const lanDependencies: LanDependencies = {
  repositoryRoot: async () => (await execute("git", ["rev-parse", "--show-toplevel"], {timeout: 30000, maxBuffer: 4096})).stdout.trim(),
  gitHead: async root => {
    const options = {cwd: root, timeout: 30000, maxBuffer: 4096};
    const state = await execute("git", ["status", "--porcelain=v1", "--untracked-files=normal"], options);
    if (state.stdout.trim() !== "") throw new Error("lan_source_dirty");
    return (await execute("git", ["rev-parse", "HEAD"], options)).stdout.trim();
  },
  readiness: checkLanReadiness, browser: checkLanBrowser, supervise: superviseControl,
};

export function parseLanAction(args: string[]): LanAction {
  const {values, positionals} = parseArgs({args, allowPositionals: true, options: {vus: {type: "string"}}});
  if (positionals.length !== 1) throw new Error("lan_usage_invalid");
  if (positionals[0] === "check" && values.vus === undefined) return {kind: "check"};
  if (positionals[0] === "run" && (values.vus === "50" || values.vus === "600"))
    return {kind: "run", vus: values.vus === "50" ? 50 : 600};
  throw new Error("lan_usage_invalid");
}

export function lanFailureCode(error: unknown): string {
  const codes = new Set(["lan_usage_invalid", "lan_platform_unsupported", "lan_tls_environment_rejected", "lan_source_dirty", "lan_input_not_regular",
    "lan_input_too_large", "lan_source_commit_mismatch", "lan_ca_invalid", "lan_ca_fingerprint_mismatch",
    "lan_readiness_unavailable", "lan_readiness_failed", "lan_readiness_timeout", "lan_readiness_tls_or_network_failed",
    "lan_browser_tls_or_page_failed"]);
  return error instanceof Error && codes.has(error.message) ? error.message : "lan_configuration_or_runtime_failed";
}

export function validateLanEnvironment(platform: NodeJS.Platform, environment: NodeJS.ProcessEnv): LanOptions {
  if (platform !== "darwin" && platform !== "linux") throw new Error("lan_platform_unsupported");
  // 起動時preloadによるTLS差替えも許可せず、Chromiumの証明書検証設定を固定する。
  if ((environment.NODE_TLS_REJECT_UNAUTHORIZED !== undefined && environment.NODE_TLS_REJECT_UNAUTHORIZED !== "1") ||
    (environment.NODE_OPTIONS?.trim() ?? "") !== "") throw new Error("lan_tls_environment_rejected");
  return lanOptionsSchema.parse({durationSeconds: environment.LAN_DURATION_SECONDS ?? "900",
    manualParticipantCount: environment.LAN_MANUAL_PARTICIPANTS ?? "0"});
}

export async function readLanServer(root: string, head: string): Promise<{server: LanServer; ca: string}> {
  const directory = resolve(root, ".local/lan-client");
  const serverText = await readBoundedInput(resolve(directory, "server.json"), 16384);
  const certificateText = await readBoundedInput(resolve(directory, "ca.crt"), 65536);
  const server = lanServerSchema.parse(JSON.parse(serverText) as unknown);
  if (server.sourceCommit !== head) throw new Error("lan_source_commit_mismatch");
  // fingerprintで確認した証明書以外をca bundleへ紛れ込ませない。
  if (!/^\s*-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----\s*$/u.test(certificateText))
    throw new Error("lan_ca_invalid");
  const certificate = new X509Certificate(certificateText);
  if (!certificate.ca || certificate.fingerprint256.replaceAll(":", "").toLowerCase() !== server.caSha256)
    throw new Error("lan_ca_fingerprint_mismatch");
  return {server, ca: certificate.toString()};
}

async function readBoundedInput(path: string, limit: number): Promise<string> {
  const initial = await lstat(path);
  if (!initial.isFile()) throw new Error("lan_input_not_regular");
  if (initial.size > limit) throw new Error("lan_input_too_large");
  // stat後のsymlink/FIFO差替えでも待機せず、読取り自体も上限で止める。
  const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const metadata = await file.stat();
    if (!metadata.isFile()) throw new Error("lan_input_not_regular");
    if (metadata.size > limit) throw new Error("lan_input_too_large");
    const buffer = Buffer.alloc(limit + 1);
    let length = 0;
    while (length < buffer.length) {
      const next = await file.read(buffer, length, buffer.length - length, length);
      if (next.bytesRead === 0) break;
      length += next.bytesRead;
    }
    if (length > limit) throw new Error("lan_input_too_large");
    return buffer.toString("utf8", 0, length);
  } finally { await file.close(); }
}

export async function createLanRun(root: string, server: LanServer, vus: 50 | 600, options: LanOptions): Promise<ControlSettings> {
  const runId = `lan-${String(vus).padStart(3, "0")}-${Date.now()}-${randomUUID()}`;
  const directory = resolve(root, ".local/lan-runs", runId);
  await mkdir(resolve(root, ".local/lan-runs"), {recursive: true, mode: 0o700});
  await mkdir(directory, {mode: 0o700});
  const identityKeyFile = resolve(directory, "identity.key");
  await writeFile(identityKeyFile, randomBytes(32).toString("hex") + "\n", {mode: 0o600, flag: "wx"});
  const settings = controlSettingsSchema.parse({run: {
    runId, sourceCommit: server.sourceCommit, baseUrl: server.baseUrl, count: 1, ordinalStart: 1,
    startAt: new Date().toISOString(), durationSeconds: options.durationSeconds, joinSpreadMs: 0,
    operatorStartDelaySeconds: 45, preScheduleMarginMs: 5000, resultHoldMs: 5000, answerDelayMs: 500,
    scenario: "normal", faultAfterSeconds: 50, faultDurationMs: 2000, outputDirectory: directory,
    channel: "chromium", headless: true, identityKeyFile,
  }, workerSize: 100, measurementLevel: "normal", detailedVuCount: 3,
    hostSampleIntervalMs: 3000, setupTimeoutSeconds: 900, operationTimeoutMs: 30000,
    manualParticipantCount: options.manualParticipantCount, expectedOperatorConnections: 1});
  await writeFile(resolve(directory, "run.json"), JSON.stringify(settings, null, 2) + "\n", {mode: 0o600, flag: "wx"});
  await writeFile(resolve(directory, "server.json"), JSON.stringify(server, null, 2) + "\n", {mode: 0o600, flag: "wx"});
  return settings;
}

export async function executeLan(action: LanAction, platform: NodeJS.Platform, environment: NodeJS.ProcessEnv,
  dependencies: LanDependencies = lanDependencies): Promise<{kind: "checked"} | {kind: "finished"; directory: string}> {
  const options = validateLanEnvironment(platform, environment);
  const root = await dependencies.repositoryRoot();
  const {server, ca} = await readLanServer(root, await dependencies.gitHead(root));
  await dependencies.readiness(ca);
  await dependencies.browser();
  if (action.kind === "check") return {kind: "checked"};
  const settings = await createLanRun(root, server, action.vus, options);
  return {kind: "finished", directory: await dependencies.supervise(settings, action.vus)};
}
