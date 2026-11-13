import { execFile } from "node:child_process";
import { copyFile, mkdir, mkdtemp, readFile, readdir, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { delimiter, dirname, join } from "node:path";
import { promisify } from "node:util";
import { afterEach, describe, expect, it } from "vitest";
import { z } from "zod";
import { controlManifestSchema, controlSettingsSchema } from "../src/controlPlan.js";
import { createRunnerPreset, parseRunnerEnvironment } from "../src/runnerPreset.js";

const roots: string[] = [];
const execute = promisify(execFile);
const sourceCommit = "a".repeat(40);
const defaults = {
  VUS: "50", EXPECTED_ROUNDS: "6", BASE_URL: "https://127.0.0.1:8443", DURATION_SECONDS: "900", WORKER_SIZE: "100",
  MEASUREMENT_LEVEL: "normal", DETAILED_VU_COUNT: "3", HOST_SAMPLE_INTERVAL_MS: "3000",
  SETUP_TIMEOUT_SECONDS: "900", OPERATION_TIMEOUT_MS: "30000", MANUAL_PARTICIPANTS: "0",
  EXPECTED_OPERATOR_CONNECTIONS: "1", CHANNEL: "chromium", HEADLESS: "true",
};
const pnpmCallSchema = z.object({args: z.array(z.string()), environment: z.record(z.string(), z.string())});

async function fixture(): Promise<string> {
  const root = await mkdtemp(join(tmpdir(), "browser-runner-preset-"));
  roots.push(root);
  return root;
}

async function makeFixture(): Promise<{root: string; record: string; environment: NodeJS.ProcessEnv}> {
  const root = await fixture();
  const bin = join(root, "bin");
  const record = join(root, "calls.jsonl");
  await mkdir(bin);
  await copyFile(new URL("../../../Makefile", import.meta.url), join(root, "Makefile"));
  // 実 Runner を起動せず、make が渡す引数と環境を subprocess の境界で記録する。
  await writeFile(join(bin, "pnpm"), `#!/usr/bin/env node
const {appendFileSync} = require("node:fs");
const names = ${JSON.stringify(Object.keys(defaults))};
appendFileSync(process.env.RUNNER_PRESET_RECORD, JSON.stringify({args: process.argv.slice(2),
  environment: Object.fromEntries(names.map(name => [name, process.env[name]]))}) + "\\n");
`, {mode: 0o700});
  const environment: NodeJS.ProcessEnv = {...process.env, PATH: [bin, dirname(process.execPath), process.env.PATH].join(delimiter),
    RUNNER_PRESET_RECORD: record};
  for (const name of [...Object.keys(defaults), "MAKEFLAGS", "MFLAGS", "MAKELEVEL", "GNUMAKEFLAGS", "MAKEFILES"])
    delete environment[name];
  return {root, record, environment};
}

async function readPnpmCalls(record: string): Promise<z.infer<typeof pnpmCallSchema>[]> {
  return (await readFile(record, "utf8")).trim().split("\n").map(line => {
    const input: unknown = JSON.parse(line);
    return pnpmCallSchema.parse(input);
  });
}

afterEach(async () => { await Promise.all(roots.splice(0).map(root => rm(root, {recursive: true, force: true}))); });

describe("configurable browser Runner preset", () => {
  it("parses the defaults and every supported environment override", () => {
    expect(parseRunnerEnvironment({})).toEqual({vus: 50, expectedRounds: 6, baseUrl: "https://127.0.0.1:8443", durationSeconds: 900,
      workerSize: 100, measurementLevel: "normal", detailedVuCount: 3, hostSampleIntervalMs: 3000,
      setupTimeoutSeconds: 900, operationTimeoutMs: 30000, manualParticipantCount: 0,
      expectedOperatorConnections: 1, channel: "chromium", headless: true});
    expect(parseRunnerEnvironment({VUS: "650", EXPECTED_ROUNDS: "10", BASE_URL: "https://10.0.1.2:8443", DURATION_SECONDS: "180",
      WORKER_SIZE: "25", MEASUREMENT_LEVEL: "minimal", DETAILED_VU_COUNT: "0", HOST_SAMPLE_INTERVAL_MS: "2000",
      SETUP_TIMEOUT_SECONDS: "10", OPERATION_TIMEOUT_MS: "100", MANUAL_PARTICIPANTS: "2",
      EXPECTED_OPERATOR_CONNECTIONS: "0", CHANNEL: "chrome", HEADLESS: "false"}))
      .toEqual({vus: 650, expectedRounds: 10, baseUrl: "https://10.0.1.2:8443", durationSeconds: 180, workerSize: 25,
        measurementLevel: "minimal", detailedVuCount: 0, hostSampleIntervalMs: 2000, setupTimeoutSeconds: 10,
        operationTimeoutMs: 100, manualParticipantCount: 2, expectedOperatorConnections: 0, channel: "chrome", headless: false});
  });

  it("rejects invalid bounds, unsupported settings and nonlocal or non-HTTPS origins", () => {
    const invalidInputs: Record<string, string[]> = {
      VUS: ["49", "651", "50.5", "oops"], DURATION_SECONDS: ["179", "3601", "180.0"],
      WORKER_SIZE: ["0", "101"], DETAILED_VU_COUNT: ["-1", "11"], HOST_SAMPLE_INTERVAL_MS: ["1999", "30001"],
      SETUP_TIMEOUT_SECONDS: ["9", "901"], OPERATION_TIMEOUT_MS: ["99", "30001"], MANUAL_PARTICIPANTS: ["-1", "101"],
      EXPECTED_OPERATOR_CONNECTIONS: ["-1", "11"], CHANNEL: ["firefox"], HEADLESS: ["1", "yes"],
      MEASUREMENT_LEVEL: ["full"], BASE_URL: ["http://localhost:8443", "https://example.com", "https://localhost:8443/path"],
    };
    for (const [name, values] of Object.entries(invalidInputs))
      for (const value of values) expect(() => parseRunnerEnvironment({[name]: value}), `${name}=${value}`).toThrow();
  });

  it.each([50, 650])("saves the exact %i VU worker plan and private settings", async vus => {
    const root = await fixture();
    const settings = await createRunnerPreset(root, sourceCommit, parseRunnerEnvironment({VUS: String(vus)}));
    expect(settings).toMatchObject({setupTimeoutSeconds: 900, operationTimeoutMs: 30000});
    const runInput: unknown = JSON.parse(await readFile(join(settings.run.outputDirectory, "run.json"), "utf8"));
    expect(controlSettingsSchema.parse(runInput)).toEqual(settings);
    const planInput: unknown = JSON.parse(await readFile(join(settings.run.outputDirectory, "plan.json"), "utf8"));
    const plan = controlManifestSchema.parse(planInput);
    expect(plan).toMatchObject({runId: settings.run.runId, sourceCommit, totalVus: vus, expectedRounds: 6});
    expect(plan.workers.map(worker => worker.count)).toEqual(vus === 50 ? [50] : [100, 100, 100, 100, 100, 100, 50]);
    expect(plan.workers.map(worker => worker.ordinalStart)).toEqual(vus === 50 ? [1] : [1, 101, 201, 301, 401, 501, 601]);
    for (const directory of [join(root, ".local/browser-runs"), settings.run.outputDirectory])
      expect((await stat(directory)).mode & 0o777).toBe(0o700);
    for (const filename of ["identity.key", "run.json", "plan.json"])
      expect((await stat(join(settings.run.outputDirectory, filename))).mode & 0o777).toBe(0o600);
  });

  it("creates distinct run IDs and keys and honors a custom worker size", async () => {
    const root = await fixture();
    const options = parseRunnerEnvironment({VUS: "50", WORKER_SIZE: "20", MANUAL_PARTICIPANTS: "2"});
    const first = await createRunnerPreset(root, sourceCommit, options);
    const second = await createRunnerPreset(root, sourceCommit, options);
    expect(first.run.runId).not.toBe(second.run.runId);
    expect(first.run.outputDirectory).not.toBe(second.run.outputDirectory);
    const keys = await Promise.all([first, second].map(settings => readFile(settings.run.identityKeyFile!, "utf8")));
    expect(keys[0]).not.toBe(keys[1]);
    for (const key of keys) expect(key).toMatch(/^[a-f0-9]{64}\n$/u);
    const serializedPlan = await readFile(join(first.run.outputDirectory, "plan.json"), "utf8");
    const planInput: unknown = JSON.parse(serializedPlan);
    const plan = controlManifestSchema.parse(planInput);
    expect(plan.workers.map(worker => worker.count)).toEqual([20, 20, 10]);
    expect(plan.manualParticipantCount).toBe(2);
    const serializedSettings = await readFile(join(first.run.outputDirectory, "run.json"), "utf8");
    expect(serializedSettings).not.toContain(keys[0]!.trim());
    expect(serializedPlan).not.toContain(keys[0]!.trim());
  });

  it("validates settings, commit and plan before writing any files", async () => {
    const root = await fixture();
    const options = parseRunnerEnvironment({});
    await expect(createRunnerPreset(root, "invalid", options)).rejects.toThrow();
    await expect(createRunnerPreset(root, sourceCommit, {...options, workerSize: 0})).rejects.toThrow();
    await expect(createRunnerPreset(root, sourceCommit, {...options, vus: 651})).rejects.toThrow();
    expect(await readdir(root)).toEqual([]);
  });
});

describe("Makefile browser Runner commands", () => {
  it.each(["browser-runner-new", "browser-runner"])("%s builds then selects creation or immediate start", async target => {
    const {root, record, environment} = await makeFixture();
    await execute("make", ["--no-print-directory", target], {cwd: root, env: environment, timeout: 10000});
    const calls = await readPnpmCalls(record);
    expect(calls.map(call => call.args)).toEqual([
      ["--dir", "load-test/browser", "build"],
      ["--dir", "load-test/browser", "runner:new", ...(target === "browser-runner" ? ["--start"] : [])],
    ]);
    expect(calls.at(-1)?.environment).toEqual(defaults);
  });

  it("passes all make overrides as environment values without evaluating shell metacharacters", async () => {
    const {root, record, environment} = await makeFixture();
    const marker = join(root, "shell-command-ran");
    const overrides = {
      VUS: "125", EXPECTED_ROUNDS: "10", BASE_URL: `https://localhost:8443; touch ${marker}; \`touch ${marker}\` ' "`,
      DURATION_SECONDS: "180", WORKER_SIZE: "25", MEASUREMENT_LEVEL: "minimal", DETAILED_VU_COUNT: "0",
      HOST_SAMPLE_INTERVAL_MS: "2000", SETUP_TIMEOUT_SECONDS: "10", OPERATION_TIMEOUT_MS: "100",
      MANUAL_PARTICIPANTS: "2", EXPECTED_OPERATOR_CONNECTIONS: "0", CHANNEL: "chrome", HEADLESS: "false",
    };
    await execute("make", ["--no-print-directory", "browser-runner-new",
      ...Object.entries(overrides).map(([name, value]) => `${name}=${value}`)], {cwd: root, env: environment, timeout: 10000});
    const calls = await readPnpmCalls(record);
    expect(calls.at(-1)?.environment).toEqual(overrides);
    expect(await readdir(root)).not.toContain("shell-command-ran");
  });
});
