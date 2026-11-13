import { readFile } from "node:fs/promises";
import { isIP } from "node:net";
import { z } from "zod";

const cohortSchema = z.object({
  ordinalStart: z.number().int().min(1).max(17576),
  count: z.number().int().min(1).max(100),
}).strict();

export function localHttpsOrigin(value: string): boolean {
  const url = URL.canParse(value) ? new URL(value) : null;
  if (!url || url.protocol !== "https:" || url.username || url.password ||
      url.pathname !== "/" || url.search || url.hash) return false;
  const host = url.hostname;
  if (host === "localhost" || host === "127.0.0.1") return true;
  if (isIP(host) !== 4) return false;
  const octets = host.split(".").map(Number);
  return octets[0] === 10 || (octets[0] === 192 && octets[1] === 168) ||
    (octets[0] === 172 && octets[1] !== undefined && octets[1] >= 16 && octets[1] <= 31);
}

export const configSchema = z.object({
  workerId: z.string().regex(/^worker-[1-9]\d*$/).optional(),
  runId: z.string().regex(/^[a-z0-9][a-z0-9-]{0,63}$/),
  sourceCommit: z.string().regex(/^[a-f0-9]{40}$/),
  baseUrl: z.string().refine(localHttpsOrigin, "local HTTPS origin required"),
  count: z.number().int().min(1).max(100),
  ordinalStart: z.number().int().min(1).max(17576),
  startAt: z.iso.datetime({ offset: true }),
  durationSeconds: z.number().int().min(180).max(3600),
  joinSpreadMs: z.number().int().min(0).max(120000),
  rampStages: z.array(z.number().int().min(1).max(100)).default([]),
  rampHoldMs: z.number().int().min(1000).max(30000).default(5000),
  operatorStartDelaySeconds: z.number().int().min(10).max(600),
  preScheduleMarginMs: z.number().int().min(1000).max(120000),
  resultHoldMs: z.number().int().min(3000).max(120000),
  answerDelayMs: z.number().int().min(0).max(8000),
  scenario: z.enum(["normal", "reconnect", "clock-delay"]),
  faultAfterSeconds: z.number().int().min(5).max(600),
  faultDurationMs: z.number().int().min(100).max(10000),
  outputDirectory: z.string().min(1),
  channel: z.enum(["chromium", "chrome"]),
  headless: z.boolean(),
  identityKeyFile: z.string().min(1).optional(),
  coordination: z.object({
    directory: z.string().min(1),
    cohorts: z.array(cohortSchema).min(1).max(100),
  }).strict().optional(),
}).strict().superRefine((value, context) => {
  if (value.ordinalStart + value.count - 1 > 17576)
    context.addIssue({code: "custom", message: "ordinal range exceeds local label capacity"});
  if (value.rampStages.some((count, index) => count > value.count ||
      (index > 0 && count <= value.rampStages[index - 1]!)))
    context.addIssue({code: "custom", path: ["rampStages"], message: "ramp stages must increase strictly within count"});
  if (value.rampStages.length > 0 && value.rampStages.at(-1) !== value.count)
    context.addIssue({code: "custom", path: ["rampStages"], message: "last ramp stage must equal count"});
  if (value.coordination !== undefined) {
    const cohorts = [...value.coordination.cohorts].sort((left, right) => left.ordinalStart - right.ordinalStart);
    if (cohorts.some((cohort, index) => cohort.ordinalStart + cohort.count - 1 > 17576 ||
      (index > 0 && cohort.ordinalStart < cohorts[index - 1]!.ordinalStart + cohorts[index - 1]!.count)))
      context.addIssue({code: "custom", path: ["coordination", "cohorts"], message: "cohorts must have non-overlapping valid ranges"});
  }
});
export type RunConfig = z.infer<typeof configSchema>;

const cloudfrontOrigin = /^https:\/\/d[a-z0-9]{1,62}\.cloudfront\.net$/u;
const domainName = /^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/u;

export function approvedAwsOrigin(value: string, reviewDomainName: string | undefined): boolean {
  return cloudfrontOrigin.test(value) || (
    reviewDomainName !== undefined && reviewDomainName.length <= 253 &&
    domainName.test(reviewDomainName) && value === `https://${reviewDomainName}`
  );
}

export const awsRunOptionsSchema = z.object({recordResults: z.boolean(), sourceDirty: z.boolean()}).strict();

// 自動回帰とLANの期限・接続先制約を保ち、AWS手動運営だけを別の入力契約にする。
export const awsRunConfigSchema = z.object({
  ...configSchema.shape,
  baseUrl: z.string().refine(value => approvedAwsOrigin(value, process.env.AWS_REVIEW_DOMAIN_NAME),
    "canonical approved AWS HTTPS origin required"),
  durationSeconds: z.null(),
  aws: awsRunOptionsSchema,
}).strict().superRefine((value, context) => {
  if (value.ordinalStart + value.count - 1 > 650)
    context.addIssue({code: "custom", message: "AWS worker ordinal range must remain within 650 VUs"});
  if (value.scenario !== "normal" || value.rampStages.length > 0 || value.coordination !== undefined)
    context.addIssue({code: "custom", message: "AWS control requires normal scenario without ramp or filesystem coordination"});
});
export const controlRunConfigSchema = z.union([configSchema, awsRunConfigSchema]);
export type BrowserRunConfig = z.infer<typeof controlRunConfigSchema>;

export async function loadConfig(path: string): Promise<RunConfig> {
  const input: unknown = JSON.parse(await readFile(path, "utf8"));
  return configSchema.parse(input);
}
