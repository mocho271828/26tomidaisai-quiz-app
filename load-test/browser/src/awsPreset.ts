import { randomBytes, randomUUID } from "node:crypto";
import { lstat, mkdir, rm, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { z } from "zod";
import { approvedAwsOrigin, configSchema } from "./config.js";
import { cliVusSchema } from "./controlCommands.js";
import { controlSettingsSchema, createControlPlan, type ControlSettings } from "./controlPlan.js";

const integerInput = z.string().regex(/^\d+$/u).transform(Number);
const fields = controlSettingsSchema.shape;
const optionsSchema = (reviewDomainName: string | undefined) => z.object({
  vus: cliVusSchema.default(300),
  baseUrl: z.string().refine(value => approvedAwsOrigin(value, reviewDomainName),
    "canonical approved AWS HTTPS origin required"),
  expectedRounds: integerInput.optional().pipe(fields.expectedRounds).default(7),
  recordResults: z.enum(["true", "false"]).default("true").transform(value => value === "true"),
  workerSize: integerInput.optional().pipe(fields.workerSize).default(50),
  measurementLevel: z.enum(["minimal", "normal"]).default("minimal"),
  detailedVuCount: integerInput.optional().pipe(fields.detailedVuCount),
  hostSampleIntervalMs: integerInput.optional().pipe(fields.hostSampleIntervalMs),
  setupTimeoutSeconds: integerInput.optional().pipe(fields.setupTimeoutSeconds),
  operationTimeoutMs: integerInput.optional().pipe(fields.operationTimeoutMs),
  manualParticipantCount: integerInput.optional().pipe(fields.manualParticipantCount),
  expectedOperatorConnections: integerInput.optional().pipe(fields.expectedOperatorConnections),
  channel: configSchema.shape.channel.default("chromium"),
  headless: z.enum(["true", "false"]).default("true").transform(value => value === "true"),
}).strict();
export type AwsRunnerOptions = z.infer<ReturnType<typeof optionsSchema>>;
export type AwsSource = {root: string; head: string; dirty: boolean};

export async function removeAwsRun(directory: string): Promise<void> {
  try { await rm(directory, {recursive: true}); }
  catch { throw new Error("aws_browser_cleanup_failed"); }
}

export function parseAwsEnvironment(environment: NodeJS.ProcessEnv): AwsRunnerOptions {
  if (environment.DURATION_SECONDS !== undefined) throw new Error("aws_browser_uses_manual_stop");
  return optionsSchema(environment.AWS_REVIEW_DOMAIN_NAME).parse({
    vus: environment.VUS, baseUrl: environment.BASE_URL,
    expectedRounds: environment.EXPECTED_ROUNDS,
    recordResults: environment.RECORD, workerSize: environment.WORKER_SIZE, measurementLevel: environment.MEASUREMENT_LEVEL,
    detailedVuCount: environment.DETAILED_VU_COUNT, hostSampleIntervalMs: environment.HOST_SAMPLE_INTERVAL_MS,
    setupTimeoutSeconds: environment.SETUP_TIMEOUT_SECONDS, operationTimeoutMs: environment.OPERATION_TIMEOUT_MS,
    manualParticipantCount: environment.MANUAL_PARTICIPANTS, expectedOperatorConnections: environment.EXPECTED_OPERATOR_CONNECTIONS,
    channel: environment.CHANNEL, headless: environment.HEADLESS,
  });
}

async function checkPrivateDirectory(path: string): Promise<void> {
  const directory = resolve(path);
  for (let current = directory; ; current = dirname(current)) {
    const info = await lstat(current);
    const own = info.uid === process.getuid?.();
    if (!info.isDirectory() || info.isSymbolicLink() || (!own && info.uid !== 0) || (info.mode & 0o022) !== 0 ||
      (current === directory && (!own || (info.mode & 0o777) !== 0o700)))
      throw new Error("aws_browser_directory_permissions_invalid");
    if (current === dirname(current)) return;
  }
}

/** 各PCを独立runにし、記録を保存する側だけに試験証跡を残す。 */
export async function createAwsRunnerPreset(source: AwsSource, options: AwsRunnerOptions): Promise<ControlSettings> {
  const runId = `aws-${options.vus}-${Date.now()}-${randomUUID().slice(0, 8)}`;
  const parent = resolve(source.root, ".local/aws-browser-runs");
  const directory = resolve(parent, runId);
  const {vus, baseUrl, recordResults, channel, headless, ...control} = options;
  const settings = controlSettingsSchema.parse({...control, run: {
    runId, sourceCommit: source.head, baseUrl, durationSeconds: null, aws: {recordResults, sourceDirty: source.dirty},
    channel, headless, count: 1, ordinalStart: 1, startAt: new Date().toISOString(),
    joinSpreadMs: 0, operatorStartDelaySeconds: 45, preScheduleMarginMs: 5000, resultHoldMs: 5000, answerDelayMs: 500,
    scenario: "normal", faultAfterSeconds: 50, faultDurationMs: 2000, outputDirectory: directory,
    identityKeyFile: resolve(directory, "identity.key"),
  }});
  const manifest = createControlPlan(settings, vus);
  await mkdir(parent, {recursive: true, mode: 0o700});
  await checkPrivateDirectory(parent);
  await mkdir(directory, {mode: 0o700});
  try {
    const key = randomBytes(32);
    try { await writeFile(settings.run.identityKeyFile!, key.toString("hex") + "\n", {mode: 0o600, flag: "wx"}); }
    finally { key.fill(0); }
    await writeFile(resolve(directory, "run.json"), JSON.stringify(settings, null, 2) + "\n", {mode: 0o600, flag: "wx"});
    await writeFile(resolve(directory, "plan.json"), JSON.stringify(manifest, null, 2) + "\n", {mode: 0o600, flag: "wx"});
  } catch (error: unknown) {
    if (!recordResults) await removeAwsRun(directory);
    throw error;
  }
  return settings;
}
