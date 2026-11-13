import { z } from "zod";
import { manualCommandSchema } from "./manualProtocol.js";
import { controlSettingsSchema, workerPlanSchema } from "./controlPlan.js";
import { expectedRoundsSchema, MAX_MANUAL_ROUNDS } from "./controlRounds.js";

export const workerInputSchema = z.object({settings: controlSettingsSchema, worker: workerPlanSchema,
  totalVus: z.number().int().min(1).max(650), firstOrdinal: z.number().int().positive(),
  deadlineAtMs: z.number().finite().positive().nullable()}).strict().superRefine((value, context) => {
  const run = value.settings.run;
  if ((run.durationSeconds === null) !== (value.deadlineAtMs === null))
    context.addIssue({code: "custom", message: "worker deadline must match run duration mode"});
});
export const parentMessageSchema = z.object({type: z.literal("command"), command: manualCommandSchema}).strict();
const count = z.number().int().nonnegative();
export const workerCountsSchema = z.object({ready: count, joined: count, connected: count, failed: count,
  completed: count, paused: count, failures: count, timeouts: count, disconnects: count, reconnects: count,
  commandReceipts: count, commandRejections: count,
  rounds: z.array(z.object({round: expectedRoundsSchema, clicked: count, accepted: count,
    results: count}).strict()).min(1).max(MAX_MANUAL_ROUNDS),
}).strict().superRefine((counts, context) => {
  if (counts.rounds.some((row, index) => row.round !== index + 1))
    context.addIssue({code: "custom", message: "contiguous_unique_rounds_required"});
});
export type WorkerCounts = z.infer<typeof workerCountsSchema>;
export const preparationFailureSchema = z.object({serial: z.number().int().min(1).max(17576),
  stage: z.enum(["navigation", "join_button", "fonts"])}).strict();
export const workerMessageSchema = z.discriminatedUnion("type", [
  z.object({type: z.literal("status"), workerId: z.string(), counts: workerCountsSchema}).strict(),
  z.object({type: z.literal("finished"), workerId: z.string(), clean: z.boolean()}).strict(),
  z.object({type: z.literal("failed"), workerId: z.string(),
    preparationFailure: preparationFailureSchema.optional()}).strict(),
]);
export function emptyCounts(expectedRounds = 6): WorkerCounts {
  expectedRoundsSchema.parse(expectedRounds);
  return {ready: 0, joined: 0, connected: 0, failed: 0, completed: 0, paused: 0,
    failures: 0, timeouts: 0, disconnects: 0, reconnects: 0, commandReceipts: 0, commandRejections: 0,
    rounds: Array.from({length: expectedRounds}, (_, index) => ({round: index + 1, clicked: 0, accepted: 0, results: 0}))};
}
