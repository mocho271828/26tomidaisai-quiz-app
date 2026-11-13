import { X509Certificate } from "node:crypto";
import { execFile } from "node:child_process";
import { mkdtemp, mkdir, readFile, readdir, rm, stat, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import { afterEach, describe, expect, it, vi } from "vitest";
import { executeLan, LAN_BASE_URL, lanDependencies, lanFailureCode, lanServerSchema, parseLanAction, readLanServer,
  validateLanEnvironment, type LanDependencies } from "../src/lanClient.js";
import type { ControlSettings } from "../src/controlPlan.js";

const roots: string[] = [];
const execute = promisify(execFile);
const ca = await readFile(new URL("./fixtures/lan-ca.pem", import.meta.url), "utf8");
const server = {sourceCommit: "a".repeat(40), baseUrl: LAN_BASE_URL,
  caSha256: new X509Certificate(ca).fingerprint256.replaceAll(":", "").toLowerCase()};

async function fixture() {
  const root = await mkdtemp(join(tmpdir(), "lan-client-unit-"));
  roots.push(root);
  const directory = join(root, ".local/lan-client");
  await mkdir(directory, {recursive: true});
  await writeFile(join(directory, "server.json"), JSON.stringify(server));
  await writeFile(join(directory, "ca.crt"), ca);
  const dependencies: LanDependencies = {
    repositoryRoot: vi.fn().mockResolvedValue(root), gitHead: vi.fn().mockResolvedValue(server.sourceCommit),
    readiness: vi.fn().mockResolvedValue(undefined), browser: vi.fn().mockResolvedValue(undefined),
    supervise: vi.fn().mockResolvedValue("fixture-evidence"),
  };
  return {root, directory, dependencies};
}
afterEach(async () => { await Promise.all(roots.splice(0).map(root => rm(root, {recursive: true}))); });

describe("LAN runner preset", () => {
  it("accepts only fixed server metadata and exact supported commands", () => {
    expect(lanServerSchema.parse(server)).toEqual(server);
    for (const changed of [{...server, baseUrl: "https://other.invalid"}, {...server, sourceCommit: "short"},
      {...server, caSha256: "short"}, {...server, ignoreHTTPSErrors: true}])
      expect(lanServerSchema.safeParse(changed).success).toBe(false);
    expect(parseLanAction(["check"])).toEqual({kind: "check"});
    expect(parseLanAction(["run", "--vus", "600"])).toEqual({kind: "run", vus: 600});
    for (const args of [["check", "--vus", "50"], ["run", "--vus", "650"], ["run", "--vus", "50.0"],
      ["run"], ["run", "--vus", "50", "--ignore-https-errors"], ["check", "extra"]])
      expect(() => parseLanAction(args)).toThrow();
  });

  it("rejects Windows native, TLS bypass/preload and invalid environment numbers before I/O", () => {
    expect(validateLanEnvironment("darwin", {})).toEqual({durationSeconds: 900, manualParticipantCount: 0});
    expect(validateLanEnvironment("linux", {LAN_DURATION_SECONDS: "180", LAN_MANUAL_PARTICIPANTS: "2"}))
      .toEqual({durationSeconds: 180, manualParticipantCount: 2});
    expect(() => validateLanEnvironment("win32", {})).toThrow("lan_platform_unsupported");
    for (const environment of [{NODE_TLS_REJECT_UNAUTHORIZED: "0"}, {NODE_OPTIONS: "--require /private/preload.js"},
      {LAN_DURATION_SECONDS: "179"}, {LAN_DURATION_SECONDS: "900.1"}, {LAN_MANUAL_PARTICIPANTS: "-1"}])
      expect(() => validateLanEnvironment("darwin", environment)).toThrow();
    expect(lanFailureCode(new Error("secret-value-must-not-be-logged"))).toBe("lan_configuration_or_runtime_failed");
  });

  it("refuses wrong commits, fingerprint mismatches and appended trust roots", async () => {
    const {root, directory} = await fixture();
    await expect(readLanServer(root, "b".repeat(40))).rejects.toThrow("lan_source_commit_mismatch");
    await writeFile(join(directory, "server.json"), JSON.stringify({...server, caSha256: "0".repeat(64)}));
    await expect(readLanServer(root, server.sourceCommit)).rejects.toThrow("lan_ca_fingerprint_mismatch");
    await writeFile(join(directory, "server.json"), JSON.stringify(server));
    await writeFile(join(directory, "ca.crt"), ca + ca);
    await expect(readLanServer(root, server.sourceCommit)).rejects.toThrow("lan_ca_invalid");
  });

  it("checks tracked and untracked changes while allowing ignored local run outputs", async () => {
    const {root} = await fixture();
    const options = {cwd: root, timeout: 10000};
    await execute("git", ["init", "--quiet"], options);
    await writeFile(join(root, ".gitignore"), ".local/\n");
    await writeFile(join(root, "runner.txt"), "committed runner\n");
    await execute("git", ["add", ".gitignore", "runner.txt"], options);
    await execute("git", ["-c", "user.name=LAN Runner Unit", "-c", "user.email=lan-fixture@example.invalid",
      "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "fixture"], options);
    const head = await lanDependencies.gitHead(root);
    expect(head).toMatch(/^[a-f0-9]{40}$/u);
    await writeFile(join(root, "runner.txt"), "changed runner\n");
    await expect(lanDependencies.gitHead(root)).rejects.toThrow("lan_source_dirty");
    await writeFile(join(root, "runner.txt"), "committed runner\n");
    await writeFile(join(root, "untracked.txt"), "new runner code\n");
    await expect(lanDependencies.gitHead(root)).rejects.toThrow("lan_source_dirty");
    await rm(join(root, "untracked.txt"));
    await writeFile(join(root, ".local/run-output.json"), "{}");
    expect(await lanDependencies.gitHead(root)).toBe(head);
  });

  it("rejects oversized metadata and CA before reading and accepts the exact metadata limit", async () => {
    const {root, directory} = await fixture();
    const metadata = JSON.stringify(server).padEnd(16384, " ");
    await writeFile(join(directory, "server.json"), metadata);
    expect((await readLanServer(root, server.sourceCommit)).server).toEqual(server);
    await writeFile(join(directory, "server.json"), metadata + " ");
    await expect(readLanServer(root, server.sourceCommit)).rejects.toThrow("lan_input_too_large");
    await writeFile(join(directory, "server.json"), JSON.stringify(server));
    await writeFile(join(directory, "ca.crt"), "x".repeat(65537));
    await expect(readLanServer(root, server.sourceCommit)).rejects.toThrow("lan_input_too_large");
  });

  it("refuses symlinks, directories and FIFO input without waiting for a writer", async () => {
    const {root, directory} = await fixture();
    const path = join(directory, "server.json");
    await rm(path);
    await symlink("ca.crt", path);
    await expect(readLanServer(root, server.sourceCommit)).rejects.toThrow("lan_input_not_regular");
    await rm(path);
    await mkdir(path);
    await expect(readLanServer(root, server.sourceCommit)).rejects.toThrow("lan_input_not_regular");
    await rm(path, {recursive: true});
    await execute("mkfifo", [path], {timeout: 1000});
    await expect(readLanServer(root, server.sourceCommit)).rejects.toThrow("lan_input_not_regular");
  });

  it("check uses both verified probes and never starts VUs or creates run secrets", async () => {
    const {root, dependencies} = await fixture();
    expect(await executeLan({kind: "check"}, "darwin", {}, dependencies)).toEqual({kind: "checked"});
    expect(dependencies.readiness).toHaveBeenCalledWith(new X509Certificate(ca).toString());
    expect(dependencies.browser).toHaveBeenCalledOnce();
    expect(dependencies.supervise).not.toHaveBeenCalled();
    expect(await readdir(join(root, ".local"))).toEqual(["lan-client"]);
  });

  it("does not create run files or launch VUs after a failed preflight", async () => {
    const {root, dependencies} = await fixture();
    vi.mocked(dependencies.readiness).mockRejectedValue(new Error("lan_readiness_unavailable"));
    await expect(executeLan({kind: "run", vus: 50}, "darwin", {}, dependencies)).rejects.toThrow("lan_readiness_unavailable");
    expect(dependencies.browser).not.toHaveBeenCalled();
    expect(dependencies.supervise).not.toHaveBeenCalled();
    expect(await readdir(join(root, ".local"))).toEqual(["lan-client"]);
  });

  it("makes new private keys/settings per run and delegates 50 and 600 VUs to the existing controller", async () => {
    const {dependencies} = await fixture();
    const settings: ControlSettings[] = [];
    vi.mocked(dependencies.supervise).mockImplementation(async value => { settings.push(value); return value.run.outputDirectory; });
    await executeLan({kind: "run", vus: 50}, "darwin", {}, dependencies);
    await executeLan({kind: "run", vus: 600}, "darwin", {LAN_MANUAL_PARTICIPANTS: "1", LAN_DURATION_SECONDS: "1200"}, dependencies);
    expect(settings[0]!.run.runId).not.toBe(settings[1]!.run.runId);
    expect(dependencies.supervise).toHaveBeenNthCalledWith(1, settings[0], 50);
    expect(dependencies.supervise).toHaveBeenNthCalledWith(2, settings[1], 600);
    expect(settings[1]).toMatchObject({manualParticipantCount: 1, expectedOperatorConnections: 1, workerSize: 100,
      run: {baseUrl: LAN_BASE_URL, sourceCommit: server.sourceCommit, count: 1, channel: "chromium", durationSeconds: 1200}});
    const keys: string[] = [];
    for (const setting of settings) {
      expect(setting).toMatchObject({setupTimeoutSeconds: 900, operationTimeoutMs: 30000});
      keys.push(await readFile(setting.run.identityKeyFile!, "utf8"));
      expect(keys.at(-1)).toMatch(/^[a-f0-9]{64}\n$/u);
      expect((await stat(setting.run.identityKeyFile!)).mode & 0o777).toBe(0o600);
      expect((await stat(setting.run.outputDirectory)).mode & 0o777).toBe(0o700);
      const saved = await readFile(join(setting.run.outputDirectory, "run.json"), "utf8");
      expect(JSON.parse(saved)).toEqual(setting);
      expect(saved).not.toContain(keys.at(-1)!.trim());
    }
    expect(keys[0]).not.toBe(keys[1]);
  });
});
