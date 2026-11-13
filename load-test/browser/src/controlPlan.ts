import { readFile } from "node:fs/promises";
import { z } from "zod";
import { controlRunConfigSchema } from "./config.js";
import { expectedRoundsSchema } from "./controlRounds.js";

export const controlSettingsSchema = z.object({
  run: controlRunConfigSchema,
  expectedRounds: expectedRoundsSchema.default(6),
  workerSize: z.number().int().min(1).max(100).default(100),
  measurementLevel: z.enum(["minimal", "normal"]).default("normal"),
  detailedVuCount: z.number().int().min(0).max(10).default(3),
  hostSampleIntervalMs: z.number().int().min(2000).max(30000).default(3000),
  setupTimeoutSeconds: z.number().int().min(10).max(900).default(900),
  operationTimeoutMs: z.number().int().min(100).max(30000).default(30000),
  manualParticipantCount: z.number().int().min(0).max(100).default(0),
  expectedOperatorConnections: z.number().int().min(0).max(10).default(1),
}).strict().superRefine((value, context) => {
  if (value.run.scenario !== "normal" || value.run.rampStages.length || value.run.coordination)
    context.addIssue({code: "custom", message: "control requires normal scenario without ramp or filesystem coordination"});
});
export type ControlSettings = z.infer<typeof controlSettingsSchema>;
export const workerPlanSchema = z.object({
  workerId: z.string().regex(/^worker-[1-9]\d*$/),
  ordinalStart: z.number().int().min(1).max(17576),
  count: z.number().int().min(1).max(100),
}).strict();
export const controlManifestSchema = z.object({
  format: z.literal("browser-control-manifest-v1"),
  runId: z.string().regex(/^[a-z0-9][a-z0-9-]{0,63}$/),
  sourceCommit: z.string().regex(/^[a-f0-9]{40}$/),
  totalVus: z.number().int().min(1).max(650),
  expectedRounds: expectedRoundsSchema,
  manualParticipantCount: z.number().int().min(0).max(100),
  workers: z.array(workerPlanSchema).min(1).max(650),
}).strict().superRefine((plan, context) => {
  const workers = [...plan.workers].sort((a, b) => a.ordinalStart - b.ordinalStart);
  if (new Set(workers.map(worker => worker.workerId)).size !== workers.length ||
      workers.reduce((sum, worker) => sum + worker.count, 0) !== plan.totalVus ||
      workers.some((worker, index) => worker.ordinalStart + worker.count - 1 > 17576 ||
        (index > 0 && worker.ordinalStart !== workers[index - 1]!.ordinalStart + workers[index - 1]!.count)))
    context.addIssue({code: "custom", message: "worker totals, unique identities and contiguous ordinals required"});
});
export type ControlManifest = z.infer<typeof controlManifestSchema>;
export type WorkerPlan = z.infer<typeof workerPlanSchema>;

export function createControlPlan(settings: ControlSettings, totalVus: number): ControlManifest {
  z.number().int().min(1).max(650).parse(totalVus);
  if (settings.run.durationSeconds === null && settings.run.ordinalStart !== 1)
    throw new Error("aws_control_ordinal_start_must_be_one");
  const workers: WorkerPlan[] = [];
  for (let offset = 0; offset < totalVus; offset += settings.workerSize)
    workers.push({workerId: `worker-${workers.length + 1}`, ordinalStart: settings.run.ordinalStart + offset,
      count: Math.min(settings.workerSize, totalVus - offset)});
  return controlManifestSchema.parse({format: "browser-control-manifest-v1", runId: settings.run.runId,
    sourceCommit: settings.run.sourceCommit, totalVus, expectedRounds: settings.expectedRounds,
    manualParticipantCount: settings.manualParticipantCount, workers});
}
export async function readControlSettings(path: string): Promise<ControlSettings> {
  const input: unknown = JSON.parse(await readFile(path, "utf8"));
  return controlSettingsSchema.parse(input);
}
