import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { configSchema, type RunConfig } from "../src/config.js";
import { inspectLease, LeaseMonitor, ParticipantCoordinator } from "../src/coordinator.js";
import { runOperator } from "../src/operator.js";
import { RunLifecycle } from "../src/lifecycle.js";
import type { Browser } from "playwright";
import type { Evidence } from "../src/evidence.js";

const config: RunConfig = configSchema.parse({
  runId: "coordinator-test", sourceCommit: "a".repeat(40), baseUrl: "https://localhost:8443",
  count: 50, ordinalStart: 1, startAt: "2026-10-02T15:00:00+09:00", durationSeconds: 300,
  joinSpreadMs: 0, operatorStartDelaySeconds: 60, preScheduleMarginMs: 5000, resultHoldMs: 5000,
  answerDelayMs: 500, scenario: "normal", faultAfterSeconds: 30, faultDurationMs: 1000,
  outputDirectory: "/tmp/coordinator-evidence", channel: "chromium", headless: true,
});
const cohort = {ordinalStart: 1, count: 50};
const lease = {version: 1, runId: config.runId, sourceCommit: config.sourceCommit, startAt: config.startAt,
  ...cohort, phase: "measurement", ready: true, sequence: 0};
const directories: string[] = [];
afterEach(async () => { await Promise.all(directories.splice(0).map(path => rm(path, {recursive: true, force: true}))); });

describe("coordinator lease boundaries", () => {
  it("rejects other runs, source versions, cohorts and aborted participants", () => {
    expect(inspectLease(lease, config, cohort)).toBe("ready");
    for (const changed of [{runId: "other"}, {sourceCommit: "b".repeat(40)}, {count: 49},
      {ordinalStart: 51}, {startAt: "2026-10-03T15:00:00+09:00"}, {phase: "stopped"}])
      expect(inspectLease({...lease, ...changed}, config, cohort)).toBe("unavailable");
  });

  it("requires a progressing heartbeat and expires using only observer monotonic time", () => {
    const monitor = new LeaseMonitor();
    expect(monitor.observe(lease, config, cohort, 100)).toBe("waiting");
    expect(monitor.observe(lease, config, cohort, 200)).toBe("waiting");
    expect(monitor.observe({...lease, sequence: 1}, config, cohort, 1000)).toBe("ready");
    expect(monitor.observe({...lease, sequence: 1}, config, cohort, 11001)).toBe("unavailable");
  });

  it("keeps a confirmed normal completion available during operator result hold", () => {
    const monitor = new LeaseMonitor();
    monitor.observe(lease, config, cohort, 100);
    expect(monitor.observe({...lease, sequence: 1}, config, cohort, 1000)).toBe("ready");
    expect(monitor.observe({...lease, sequence: 2, phase: "completed"}, config, cohort, 2000)).toBe("ready");
    expect(monitor.observe({...lease, sequence: 2, phase: "completed"}, config, cohort, 130000)).toBe("ready");
    expect(monitor.observe({...lease, sequence: 1}, config, cohort, 130001)).toBe("unavailable");
  });

  it("requires nonoverlapping distributed cohort ranges", () => {
    expect(configSchema.safeParse({...config, coordination: {directory: "/shared/run",
      cohorts: [{ordinalStart: 1, count: 50}, {ordinalStart: 51, count: 50}]}}).success).toBe(true);
    expect(configSchema.safeParse({...config, coordination: {directory: "/shared/run",
      cohorts: [{ordinalStart: 1, count: 50}, {ordinalStart: 50, count: 50}]}}).success).toBe(false);
  });

  it("does not overwrite an earlier owner on duplicate startup or failed cleanup", async () => {
    const directory = await mkdtemp(join(tmpdir(), "quiz-coordinator-"));
    directories.push(directory);
    const localConfig = {...config, coordination: {directory, cohorts: [cohort]}};
    const first = new ParticipantCoordinator(localConfig, vi.fn());
    const duplicate = new ParticipantCoordinator(localConfig, vi.fn());
    try {
      await first.start();
      const path = join(directory, config.runId, "participants-1.json");
      const original = await readFile(path, "utf8");
      await expect(duplicate.start()).rejects.toThrow();
      await duplicate.stop();
      expect(await readFile(path, "utf8")).toBe(original);
      await first.stop();
      expect(JSON.parse(await readFile(path, "utf8"))).toMatchObject({phase: "stopped", ready: false});
    } finally { await first.stop(); }
  });

  it("stops operator progression before prepare when the participant runner has stopped", async () => {
    const directory = await mkdtemp(join(tmpdir(), "quiz-coordinator-operator-"));
    directories.push(directory);
    const localConfig = {...config, startAt: new Date(Date.now() - 120_000).toISOString(),
      coordination: {directory, cohorts: [cohort]}};
    const participants = new ParticipantCoordinator(localConfig, vi.fn());
    await participants.start();
    await participants.stop();
    const credentialPath = join(directory, "fixture-credentials.json");
    await writeFile(credentialPath, JSON.stringify({username: "fixture", password: "fixture"}), {mode: 0o600});
    const clicks: (string | RegExp)[] = [];
    const page = {
      on: vi.fn(), off: vi.fn(), setDefaultTimeout: vi.fn(), goto: vi.fn(async () => undefined),
      getByLabel: () => ({fill: vi.fn(async () => undefined)}),
      getByRole: (_role: string, options: {name: string | RegExp}) => ({
        click: async () => { clicks.push(options.name); }, waitFor: vi.fn(async () => undefined),
      }),
      locator: () => ({waitFor: vi.fn(async () => undefined)}),
    };
    const close = vi.fn(async () => undefined);
    const browser = {newContext: async () => ({newPage: async () => page, close})} as unknown as Browser;
    const evidence = {event: vi.fn()} as unknown as Evidence;
    const lifecycle = new RunLifecycle(evidence, "operator");
    await expect(runOperator(browser, localConfig, evidence, credentialPath, lifecycle))
      .rejects.toThrow("participant_cohort_unavailable");
    expect(clicks).toEqual(["ログイン"]);
    expect(close).toHaveBeenCalledOnce();
  });

  it("publishes each round explicitly before observing results and advancing", async () => {
    const directory = await mkdtemp(join(tmpdir(), "quiz-operator-publication-"));
    directories.push(directory);
    const credentialPath = join(directory, "fixture-credentials.json");
    await writeFile(credentialPath, JSON.stringify({username: "fixture", password: "fixture"}), {mode: 0o600});
    const localConfig = {...config, startAt: new Date(Date.now() - 120_000).toISOString(),
      preScheduleMarginMs: 0, resultHoldMs: 0};
    const actions: string[] = [];
    const page = {
      on: vi.fn(), off: vi.fn(), setDefaultTimeout: vi.fn(), goto: vi.fn(async () => undefined),
      getByLabel: () => ({fill: vi.fn(async () => undefined)}),
      getByRole: (role: string, options: {name: string | RegExp}) => ({
        click: async () => { actions.push(`click:${String(options.name)}`); },
        waitFor: async () => { actions.push(`wait:${role}:${String(options.name)}`); },
      }),
    };
    const close = vi.fn(async () => undefined);
    const browser = {newContext: async () => ({newPage: async () => page, close})} as unknown as Browser;
    const evidence = {event: vi.fn()} as unknown as Evidence;
    await runOperator(browser, localConfig, evidence, credentialPath, new RunLifecycle(evidence, "operator"));
    expect(actions.slice(0, 2)).toEqual(["click:ログイン", "wait:region:現在の問題と進行"]);
    const rounds = actions.slice(2);
    for (let round = 0; round < 6; round++) {
      const offset = round * 6;
      expect(rounds.slice(offset, offset + 4)).toEqual([
        "click:問題を確定して配信", "click:3秒後に開始予約", "click:正解・結果を公開", "wait:heading:公開済み",
      ]);
      if (round < 5) expect(rounds.slice(offset + 4, offset + 6)).toEqual([
        "click:/^backupを検証して登録問題 \\d+へ進む$/", "wait:button:問題を確定して配信",
      ]);
    }
    expect(rounds).toHaveLength(34);
    expect(close).toHaveBeenCalledOnce();
  });
});
