import { execFile } from "node:child_process";
import { copyFile, mkdir, mkdtemp, readFile, readdir, rm, stat, writeFile } from "node:fs/promises";
import { delimiter, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { afterEach, describe, expect, it, vi } from "vitest";
import { z } from "zod";
import { createAwsRunnerPreset, parseAwsEnvironment } from "../src/awsPreset.js";
import { awsFailureCode, readAwsSource, runAwsRunner, validateAwsRuntime } from "../src/aws.js";
import { controlManifestSchema, type ControlSettings } from "../src/controlPlan.js";

const roots: string[] = [];
const execute = promisify(execFile);
const repository = fileURLToPath(new URL("../../../", import.meta.url));
const sourceCommit = "a".repeat(40);
const input = {BASE_URL: "https://d123test.cloudfront.net"};
const failure = vi.hoisted(() => ({runJson: false, cleanup: false}));
vi.mock("node:fs/promises", async importOriginal => {
  const original = await importOriginal<typeof import("node:fs/promises")>();
  return {...original,
    writeFile: async (...args: Parameters<typeof original.writeFile>) => {
      if (failure.runJson && String(args[0]).endsWith("/run.json")) throw new Error("disk_full");
      return original.writeFile(...args);
    },
    rm: async (...args: Parameters<typeof original.rm>) => {
      if (failure.cleanup) throw new Error("delete_failed");
      return original.rm(...args);
    }};
});

async function fixture(): Promise<string> {
  const parent = resolve(repository, ".local/aws-runner-tests");
  await mkdir(parent, {recursive: true, mode: 0o700});
  const root = await mkdtemp(resolve(parent, "case-"));
  roots.push(root);
  return root;
}

afterEach(async () => {
  failure.runJson = false;
  failure.cleanup = false;
  vi.restoreAllMocks();
  await Promise.all(roots.splice(0).map(root => rm(root, {recursive: true, force: true})));
});

describe("independent AWS browser runs", () => {
  it.each([250, 300, 650])("plans %i VUs, each run with its own private key and directory", async vus => {
    const root = await fixture();
    const options = parseAwsEnvironment({...input, VUS: String(vus)});
    const source = {root, head: sourceCommit, dirty: false};
    const first = await createAwsRunnerPreset(source, options);
    const second = await createAwsRunnerPreset(source, options);
    expect(first.run).toMatchObject({sourceCommit, durationSeconds: null, ordinalStart: 1, aws: {recordResults: true, sourceDirty: false}});
    expect(second.run.runId).not.toBe(first.run.runId);
    const keys = await Promise.all([first, second].map(settings => readFile(settings.run.identityKeyFile!, "utf8")));
    expect(keys[0]).not.toBe(keys[1]);
    const manifest: unknown = JSON.parse(await readFile(resolve(first.run.outputDirectory, "plan.json"), "utf8"));
    const plan = controlManifestSchema.parse(manifest);
    expect(plan.totalVus).toBe(vus);
    expect(plan.workers.reduce((sum, worker) => sum + worker.count, 0)).toBe(vus);
    expect(plan.workers[0]).toMatchObject({workerId: "worker-1", ordinalStart: 1});
    expect((await stat(first.run.outputDirectory)).mode & 0o777).toBe(0o700);
    for (const filename of ["run.json", "plan.json", "identity.key"])
      expect((await stat(resolve(first.run.outputDirectory, filename))).mode & 0o777).toBe(0o600);
    expect(await readFile(resolve(first.run.outputDirectory, "run.json"), "utf8")).not.toContain(keys[0]!.trim());
  });

  it("supports every command setting and rejects malformed or local-only inputs", () => {
    expect(parseAwsEnvironment(input)).toMatchObject({vus: 300, workerSize: 50, measurementLevel: "minimal", recordResults: true});
    expect(parseAwsEnvironment({BASE_URL: "https://quiz.example.org", AWS_REVIEW_DOMAIN_NAME: "quiz.example.org", VUS: "150", MANUAL_PARTICIPANTS: "90"}))
      .toMatchObject({baseUrl: "https://quiz.example.org", vus: 150, manualParticipantCount: 90, expectedRounds: 7});
    expect(parseAwsEnvironment({...input, EXPECTED_ROUNDS: "10"})).toMatchObject({expectedRounds: 10});
    expect(parseAwsEnvironment({...input, VUS: "250", RECORD: "false", WORKER_SIZE: "25", MEASUREMENT_LEVEL: "normal",
      DETAILED_VU_COUNT: "2", HOST_SAMPLE_INTERVAL_MS: "4000", SETUP_TIMEOUT_SECONDS: "300", OPERATION_TIMEOUT_MS: "10000",
      MANUAL_PARTICIPANTS: "2", EXPECTED_OPERATOR_CONNECTIONS: "0", CHANNEL: "chrome", HEADLESS: "false"}))
      .toMatchObject({vus: 250, recordResults: false, workerSize: 25, measurementLevel: "normal", detailedVuCount: 2,
        hostSampleIntervalMs: 4000, setupTimeoutSeconds: 300, operationTimeoutMs: 10000, manualParticipantCount: 2,
        expectedOperatorConnections: 0, channel: "chrome", headless: false});
    for (const override of [{VUS: "49"}, {VUS: "651"}, {VUS: "500.5"}, {RECORD: "1"},
      {DURATION_SECONDS: "3600"}, {EXPECTED_ROUNDS: "0"}, {EXPECTED_ROUNDS: "4096"}, {EXPECTED_ROUNDS: "7.5"},
      {BASE_URL: "https://quiz.example.org"}, {BASE_URL: "https://example.com"}, {BASE_URL: "https://127.0.0.1"},
      {BASE_URL: "https://d123test.cloudfront.net/path"}, {HEADLESS: "yes"}, {WORKER_SIZE: "0"}])
      expect(() => parseAwsEnvironment({...input, ...override})).toThrow();
  });

  it("records the local revision and dirty state without a deployment commit argument", async () => {
    const root = await fixture();
    await execute("git", ["init", "--quiet"], {cwd: root});
    await execute("git", ["-c", "user.name=Runner Test", "-c", "user.email=runner@example.invalid",
      "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "--allow-empty", "-m", "fixture"], {cwd: root});
    const clean = await readAwsSource(root);
    expect(clean).toMatchObject({root, dirty: false});
    expect(clean.head).toMatch(/^[a-f0-9]{40}$/u);
    await writeFile(resolve(root, "uncommitted.txt"), "local edit");
    const dirty = await readAwsSource(root);
    expect(dirty).toEqual({...clean, dirty: true});
    const settings = await createAwsRunnerPreset(dirty, parseAwsEnvironment(input));
    expect(settings.run).toMatchObject({sourceCommit: clean.head, aws: {sourceDirty: true}});
    expect(JSON.parse(await readFile(resolve(settings.run.outputDirectory, "run.json"), "utf8")))
      .toMatchObject({run: {sourceCommit: clean.head, aws: {sourceDirty: true}}});
  });

  it("removes a transient key if writing settings fails after private directory creation", async () => {
    const root = await fixture();
    failure.runJson = true;
    await expect(createAwsRunnerPreset({root, head: sourceCommit, dirty: false}, parseAwsEnvironment({...input, RECORD: "false"}))).rejects.toThrow("disk_full");
    expect(await readdir(resolve(root, ".local/aws-browser-runs"))).toEqual([]);
  });

  it("reports cleanup failure without claiming the records were removed", async () => {
    const root = await fixture();
    const source = vi.fn().mockResolvedValue({root, head: sourceCommit, dirty: false});
    const supervise = vi.fn().mockResolvedValue("/result");
    const completion = vi.fn();
    const stdout = vi.spyOn(process.stdout, "write").mockReturnValue(true);
    failure.cleanup = true;
    await expect(runAwsRunner({...input, RECORD: "false"}, {source, supervise, completion})).rejects.toThrow("aws_browser_cleanup_failed");
    expect(await readdir(resolve(root, ".local/aws-browser-runs"))).toHaveLength(1);
    expect(stdout.mock.calls.flat().join("")).not.toContain("削除しました");
  });

  it.each([true, false])("RECORD=%s prints completion then keeps only requested results", async record => {
    const root = await fixture();
    const retained = await createAwsRunnerPreset({root, head: sourceCommit, dirty: false}, parseAwsEnvironment(input));
    const source = vi.fn().mockResolvedValue({root, head: sourceCommit, dirty: true});
    let observed: ControlSettings | undefined;
    const supervise = vi.fn(async (settings: ControlSettings): Promise<string> => {
      observed = settings;
      await writeFile(resolve(settings.run.outputDirectory, "report-marker"), "done", {mode: 0o600});
      return settings.run.outputDirectory;
    });
    const completion = vi.fn(async (directory: string, retain = true): Promise<void> => {
      expect(retain).toBe(record);
      expect(await readFile(resolve(directory, "report-marker"), "utf8")).toBe("done");
    });
    vi.spyOn(process.stdout, "write").mockReturnValue(true);
    await runAwsRunner({...input, RECORD: String(record)}, {source, supervise, completion});
    expect(supervise).toHaveBeenCalledWith(expect.objectContaining({run: expect.objectContaining({
      sourceCommit, durationSeconds: null, aws: {recordResults: record, sourceDirty: true},
    })}), 300);
    expect(completion).toHaveBeenCalledOnce();
    expect(await readdir(dirname(retained.run.outputDirectory))).toHaveLength(record ? 2 : 1);
    if (!record) await expect(stat(observed!.run.outputDirectory)).rejects.toMatchObject({code: "ENOENT"});
    expect(await readFile(retained.run.identityKeyFile!, "utf8")).toMatch(/^[a-f0-9]{64}\n$/u);
  });

  it("removes only the transient run after failure and never masks it as success", async () => {
    const root = await fixture();
    const source = vi.fn().mockResolvedValue({root, head: sourceCommit, dirty: false});
    const supervise = vi.fn().mockRejectedValue(new Error("worker_failed"));
    const completion = vi.fn();
    vi.spyOn(process.stdout, "write").mockReturnValue(true);
    await expect(runAwsRunner({...input, RECORD: "false"}, {source, supervise, completion})).rejects.toThrow("worker_failed");
    expect(completion).not.toHaveBeenCalled();
    expect(await readdir(resolve(root, ".local/aws-browser-runs"))).toEqual([]);
    expect(awsFailureCode(new Error("password=private"))).toBe("aws_browser_configuration_or_runtime_failed");
    expect(() => validateAwsRuntime({NODE_TLS_REJECT_UNAUTHORIZED: "0"})).toThrow("tls_environment_rejected");
    expect(() => validateAwsRuntime({NODE_OPTIONS: "--require /untrusted"})).toThrow("tls_environment_rejected");
  });
});

describe("AWS Makefile command", () => {
  it("passes settings as environment values without evaluating shell metacharacters", async () => {
    const root = await fixture();
    const bin = resolve(root, "bin");
    const record = resolve(root, "calls.jsonl");
    await mkdir(bin);
    await copyFile(resolve(repository, "Makefile"), resolve(root, "Makefile"));
    const names = ["BASE_URL", "SOURCE_COMMIT", "VUS", "RECORD", "WORKER_SIZE", "MEASUREMENT_LEVEL"];
    await writeFile(resolve(bin, "pnpm"), `#!/usr/bin/env node
const {appendFileSync}=require("node:fs");
appendFileSync(process.env.AWS_RUNNER_TEST_RECORD,JSON.stringify({args:process.argv.slice(2),values:Object.fromEntries(${JSON.stringify(names)}.map(n=>[n,process.env[n]]))})+"\\n");
`, {mode: 0o700});
    const environment: NodeJS.ProcessEnv = {...process.env, PATH: [bin, dirname(process.execPath), process.env.PATH].join(delimiter), AWS_RUNNER_TEST_RECORD: record};
    for (const name of ["MAKEFLAGS", "MFLAGS", "MAKELEVEL", ...names]) delete environment[name];
    const marker = resolve(root, "unexpected");
    const injected = `https://d123test.cloudfront.net; touch ${marker}; \`touch ${marker}\``;
    await execute("make", ["aws-browser-runner", `BASE_URL=${injected}`, "VUS=250", "RECORD=false"], {cwd: root, env: environment});
    const schema = z.object({args: z.array(z.string()), values: z.record(z.string(), z.string())});
    const calls = (await readFile(record, "utf8")).trim().split("\n").map(line => schema.parse(JSON.parse(line) as unknown));
    expect(calls.map(call => call.args)).toEqual([["--dir", "load-test/browser", "build"], ["--dir", "load-test/browser", "aws:run"]]);
    expect(calls[1]!.values).toEqual({BASE_URL: injected, VUS: "250", RECORD: "false", WORKER_SIZE: "50", MEASUREMENT_LEVEL: "minimal"});
    expect(await readdir(root)).not.toContain("unexpected");
  });
});
