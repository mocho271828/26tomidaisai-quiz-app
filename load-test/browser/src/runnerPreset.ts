import { randomBytes, randomUUID } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { z } from "zod";
import { configSchema } from "./config.js";
import { cliVusSchema } from "./controlCommands.js";
import { controlSettingsSchema, createControlPlan, type ControlSettings } from "./controlPlan.js";

const integerInput = z.string().regex(/^\d+$/u).transform(Number);
const controlFields = controlSettingsSchema.shape;
const runFields = configSchema.shape;
const optionsSchema = z.object({
  vus: cliVusSchema.default(50),
  expectedRounds: integerInput.optional().pipe(controlFields.expectedRounds),
  baseUrl: runFields.baseUrl.default("https://127.0.0.1:8443"),
  durationSeconds: integerInput.default(900).pipe(runFields.durationSeconds),
  workerSize: integerInput.optional().pipe(controlFields.workerSize),
  measurementLevel: controlFields.measurementLevel,
  detailedVuCount: integerInput.optional().pipe(controlFields.detailedVuCount),
  hostSampleIntervalMs: integerInput.optional().pipe(controlFields.hostSampleIntervalMs),
  setupTimeoutSeconds: integerInput.optional().pipe(controlFields.setupTimeoutSeconds),
  operationTimeoutMs: integerInput.optional().pipe(controlFields.operationTimeoutMs),
  manualParticipantCount: integerInput.optional().pipe(controlFields.manualParticipantCount),
  expectedOperatorConnections: integerInput.optional().pipe(controlFields.expectedOperatorConnections),
  channel: runFields.channel.default("chromium"),
  headless: z.enum(["true", "false"]).default("true").transform(value => value === "true"),
}).strict();
export type RunnerOptions = z.infer<typeof optionsSchema>;

export function parseRunnerEnvironment(environment: NodeJS.ProcessEnv): RunnerOptions {
  return optionsSchema.parse({
    vus: environment.VUS, baseUrl: environment.BASE_URL, durationSeconds: environment.DURATION_SECONDS,
    expectedRounds: environment.EXPECTED_ROUNDS,
    workerSize: environment.WORKER_SIZE, measurementLevel: environment.MEASUREMENT_LEVEL,
    detailedVuCount: environment.DETAILED_VU_COUNT, hostSampleIntervalMs: environment.HOST_SAMPLE_INTERVAL_MS,
    setupTimeoutSeconds: environment.SETUP_TIMEOUT_SECONDS, operationTimeoutMs: environment.OPERATION_TIMEOUT_MS,
    manualParticipantCount: environment.MANUAL_PARTICIPANTS,
    expectedOperatorConnections: environment.EXPECTED_OPERATOR_CONNECTIONS,
    channel: environment.CHANNEL, headless: environment.HEADLESS,
  });
}

export async function createRunnerPreset(root: string, sourceCommit: string, options: RunnerOptions): Promise<ControlSettings> {
  const runId = `browser-${options.vus}-${Date.now()}-${randomUUID().slice(0, 8)}`;
  const parent = resolve(root, ".local/browser-runs");
  const directory = resolve(parent, runId);
  const {vus, baseUrl, durationSeconds, channel, headless, ...control} = options;
  const settings = controlSettingsSchema.parse({
    ...control,
    run: {
      runId, sourceCommit, baseUrl, durationSeconds, channel, headless, count: 1, ordinalStart: 1,
      startAt: new Date().toISOString(), joinSpreadMs: 0, operatorStartDelaySeconds: 45,
      preScheduleMarginMs: 5000, resultHoldMs: 5000, answerDelayMs: 500,
      scenario: "normal", faultAfterSeconds: 50, faultDurationMs: 2000,
      outputDirectory: directory, identityKeyFile: resolve(directory, "identity.key"),
    },
  });
  // 入力・worker計画が成立してから秘密と出力先を作り、途中失敗したrunも再利用しない。
  const plan = createControlPlan(settings, vus);
  await mkdir(parent, {recursive: true, mode: 0o700});
  await mkdir(directory, {mode: 0o700});
  await writeFile(settings.run.identityKeyFile!, randomBytes(32).toString("hex") + "\n", {mode: 0o600, flag: "wx"});
  await writeFile(resolve(directory, "run.json"), JSON.stringify(settings, null, 2) + "\n", {mode: 0o600, flag: "wx"});
  await writeFile(resolve(directory, "plan.json"), JSON.stringify(plan, null, 2) + "\n", {mode: 0o600, flag: "wx"});
  return settings;
}
