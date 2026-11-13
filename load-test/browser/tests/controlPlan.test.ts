import { readFile } from "node:fs/promises";
import { describe, expect, it } from "vitest";
import { controlManifestSchema, controlSettingsSchema, createControlPlan } from "../src/controlPlan.js";
import { cliVusSchema, durationMs, parseControlCommand } from "../src/controlCommands.js";
import { emptyCounts, workerCountsSchema, workerInputSchema } from "../src/controlIpc.js";
import { MAX_MANUAL_ROUNDS } from "../src/controlRounds.js";

const settings = controlSettingsSchema.parse({run: {
  runId: "control-test", sourceCommit: "0".repeat(40), baseUrl: "https://localhost:8443",
  count: 5, ordinalStart: 1, startAt: "2026-10-02T00:00:00Z", durationSeconds: 180,
  joinSpreadMs: 0, operatorStartDelaySeconds: 30, preScheduleMarginMs: 1000, resultHoldMs: 3000,
  answerDelayMs: 0, scenario: "normal", faultAfterSeconds: 5, faultDurationMs: 100,
  outputDirectory: "/tmp/control-tests", channel: "chromium", headless: true,
}});

describe("fixed control plan", () => {
  it.each([7, 10, 12])("keeps a fixed denominator and command boundary for %i rounds", expectedRounds => {
    const plan = createControlPlan({...settings, expectedRounds}, 150);
    expect(plan).toMatchObject({expectedRounds, totalVus: 150});
    expect(parseControlCommand(`answer --round ${expectedRounds} --when-open`, plan)).toMatchObject({round: expectedRounds});
    expect(() => parseControlCommand(`answer --round ${expectedRounds + 1} --now`, plan)).toThrow("round_outside_plan");
    expect(workerCountsSchema.parse(emptyCounts(expectedRounds)).rounds).toHaveLength(expectedRounds);
  });

  it("bounds planned rounds by the existing command budget", () => {
    expect(controlSettingsSchema.parse({...settings, expectedRounds: MAX_MANUAL_ROUNDS}).expectedRounds).toBe(MAX_MANUAL_ROUNDS);
    for (const expectedRounds of [0, -1, 1.5, MAX_MANUAL_ROUNDS + 1, Infinity])
      expect(controlSettingsSchema.safeParse({...settings, expectedRounds}).success).toBe(false);
  });

  it("shares extended timeout defaults with the example while preserving explicit values", async () => {
    const defaults = {setupTimeoutSeconds: 900, operationTimeoutMs: 30000};
    expect(settings).toMatchObject(defaults);
    const example: unknown = JSON.parse(await readFile(new URL("../control.example.json", import.meta.url), "utf8"));
    expect(controlSettingsSchema.parse(example)).toMatchObject(defaults);
    expect(controlSettingsSchema.parse({...settings, setupTimeoutSeconds: 300, operationTimeoutMs: 10000}))
      .toMatchObject({setupTimeoutSeconds: 300, operationTimeoutMs: 10000});
  });
  it("validates public bounds but keeps isolated small tests possible", () => {
    for (const count of [50, 100, 300, 650]) expect(cliVusSchema.parse(String(count))).toBe(count);
    for (const count of [0, 49, 651, 50.5, "50x", Infinity]) expect(cliVusSchema.safeParse(count).success).toBe(false);
    expect(createControlPlan(settings, 1).totalVus).toBe(1);
    for (const count of [0, 651, Infinity, 3.5]) expect(() => createControlPlan(settings, count)).toThrow();
  });
  it("splits 650 into six hundreds plus fifty with exact contiguous membership", () => {
    const plan = createControlPlan(settings, 650);
    expect(plan.workers.map(worker => worker.count)).toEqual([100, 100, 100, 100, 100, 100, 50]);
    expect(plan.workers.map(worker => worker.ordinalStart)).toEqual([1, 101, 201, 301, 401, 501, 601]);
    expect(plan.expectedRounds * plan.totalVus).toBe(3900);
  });
  it("rejects overlap, missing worker, gap, duplicate identities and total mismatch", () => {
    const plan = createControlPlan(settings, 300);
    for (const patch of [
      {workers: plan.workers.slice(0, 2)}, {totalVus: 299},
      {workers: plan.workers.map((worker, index) => index === 1 ? {...worker, ordinalStart: 100} : worker)},
      {workers: plan.workers.map((worker, index) => index === 1 ? {...worker, ordinalStart: 102} : worker)},
      {workers: plan.workers.map(worker => ({...worker, workerId: "worker-1"}))},
    ]) expect(controlManifestSchema.safeParse({...plan, ...patch}).success).toBe(false);
  });
  it("rejects out of capacity and incompatible automatic controls", () => {
    expect(() => createControlPlan({...settings, run: {...settings.run, ordinalStart: 17500}}, 100)).toThrow();
    expect(controlSettingsSchema.safeParse({...settings, run: {...settings.run, scenario: "reconnect"}}).success).toBe(false);
  });
});

describe("independent AWS control plans", () => {
  const awsSettings = (recordResults: boolean) => controlSettingsSchema.parse({...settings, workerSize: 100,
    run: {...settings.run, count: 1, baseUrl: "https://d123abc.cloudfront.net", durationSeconds: null,
      ordinalStart: 1, aws: {recordResults, sourceDirty: false}}});

  it("uses the same local worker plan regardless of whether results will be retained", () => {
    const saved = createControlPlan(awsSettings(true), 650);
    const temporary = createControlPlan(awsSettings(false), 650);
    expect(saved).toEqual(temporary);
    expect(saved.workers.map(worker => worker.workerId)).toEqual(["worker-1", "worker-2", "worker-3", "worker-4", "worker-5", "worker-6", "worker-7"]);
    expect(saved.workers.at(-1)).toMatchObject({ordinalStart: 601, count: 50});
    expect(saved).not.toHaveProperty("aws");
  });

  it("starts each independent run at one without any counterpart allocation", () => {
    const first = createControlPlan(awsSettings(true), 50);
    const second = createControlPlan(awsSettings(false), 650);
    expect(first.workers).toEqual([{workerId: "worker-1", ordinalStart: 1, count: 50}]);
    expect(second.workers[0]).toEqual({workerId: "worker-1", ordinalStart: 1, count: 100});
    expect(second.workers.at(-1)).toMatchObject({ordinalStart: 601, count: 50});
    const shifted = awsSettings(true);
    expect(() => createControlPlan({...shifted, run: {...shifted.run, ordinalStart: 2}}, 50))
      .toThrow("aws_control_ordinal_start_must_be_one");
  });

  it.each([true, false])("requires a matching deadline mode with recordResults=%s", recordResults => {
    const input = {settings: awsSettings(recordResults), worker: {workerId: "worker-1", ordinalStart: 1, count: 50},
      totalVus: 50, firstOrdinal: 1, deadlineAtMs: null};
    expect(workerInputSchema.parse(input)).toMatchObject({deadlineAtMs: null, settings: {run: {aws: {recordResults}}}});
    expect(workerInputSchema.safeParse({...input, deadlineAtMs: Date.now() + 1000}).success).toBe(false);
    expect(workerInputSchema.safeParse({...input, settings}).success).toBe(false);
    expect(workerInputSchema.safeParse({...input, settings, deadlineAtMs: Date.now() + 1000}).success).toBe(true);
  });
});

describe("console commands", () => {
  const plan = createControlPlan(settings, 650);
  it("binds each command to the immutable whole-run target", () => {
    expect(parseControlCommand("join --spread 30s", plan, 1000)).toMatchObject({kind: "join", runId: plan.runId,
      target: {ordinalStart: 1, count: 650}, issuedAtMs: 1000, spreadMs: 30000});
    expect(parseControlCommand("answer --round 2 --when-open --delay 500ms --spread 2s", plan))
      .toMatchObject({kind: "answer", round: 2, mode: "when-open", delayMs: 500, spreadMs: 2000});
  });
  it("rejects ambiguous modes, invalid rounds and unrecognized arguments", () => {
    for (const line of ["answer --round 2", "answer --round 2 --now --when-open", "answer --round 7 --now",
      "answer --round 1 --now --delay 1", "answer --round 1 --now --round 2", "join --delay 2s",
      "resume --spread 1s", "stop now", "status --now", "answer --round 1 --now --unknown x"])
      expect(() => parseControlCommand(line, plan)).toThrow();
    expect(parseControlCommand("status", plan)).toBe("status");
    for (const kind of ["pause", "resume", "stop"]) expect(parseControlCommand(kind, plan)).toMatchObject({kind});
  });
  it("bounds scheduled spread and delay", () => {
    expect(durationMs("0.5s")).toBe(500);
    for (const text of ["-1s", "0.1ms", "121s", "Infinityms", "1h"]) expect(() => durationMs(text)).toThrow();
  });
});
