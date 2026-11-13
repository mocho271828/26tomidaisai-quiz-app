import { z } from "zod";
import { expectedRoundsSchema, MAX_MANUAL_ROUNDS, roundNumberSchema } from "./controlRounds.js";

const ordinal = z.number().int().min(1).max(17576);
const envelope = {
  runId: z.string().regex(/^[a-z0-9][a-z0-9-]{0,63}$/u),
  commandId: z.string().regex(/^[a-zA-Z0-9-]{1,80}$/u),
  issuedAtMs: z.number().finite().nonnegative(),
  target: z.object({ordinalStart: ordinal, count: z.number().int().min(1).max(650)}).strict(),
};
const spreadMs = z.number().int().min(0).max(120000);
export const manualCommandSchema = z.discriminatedUnion("kind", [
  z.object({...envelope, kind: z.literal("join"), spreadMs}).strict(),
  z.object({...envelope, kind: z.literal("answer"), round: expectedRoundsSchema,
    mode: z.enum(["now", "when-open"]), delayMs: z.number().int().min(0).max(120000), spreadMs}).strict(),
  z.object({...envelope, kind: z.literal("pause")}).strict(),
  z.object({...envelope, kind: z.literal("resume")}).strict(),
  z.object({...envelope, kind: z.literal("stop")}).strict(),
]).superRefine((command, context) => {
  if (command.target.ordinalStart + command.target.count - 1 > 17576)
    context.addIssue({code: "custom", path: ["target"], message: "target_range_invalid"});
});
export type ManualCommand = z.infer<typeof manualCommandSchema>;
export type ManualAction = Extract<ManualCommand, {kind: "join" | "answer"}>;

export function spreadOffset(command: ManualAction, serial: number): number {
  if (serial < command.target.ordinalStart || serial >= command.target.ordinalStart + command.target.count)
    throw new Error("command_target_mismatch");
  return command.target.count === 1 ? 0 :
    command.spreadMs * (serial - command.target.ordinalStart) / (command.target.count - 1);
}

const roundList = z.array(roundNumberSchema).max(MAX_MANUAL_ROUNDS);
export const manualParticipantStatusSchema = z.object({
  serial: ordinal, ready: z.boolean(), joined: z.boolean(), connected: z.boolean(),
  paused: z.boolean(), stopped: z.boolean(), complete: z.boolean(), failed: z.boolean(),
  currentRound: z.number().int().min(0).max(MAX_MANUAL_ROUNDS),
  clicked: roundList, accepted: roundList, results: roundList, answeredResults: roundList, unansweredResults: roundList,
  failures: z.number().int().nonnegative(), timeouts: z.number().int().nonnegative(),
  disconnects: z.number().int().nonnegative(), reconnects: z.number().int().nonnegative(),
  pending: z.number().int().min(0).max(MAX_MANUAL_ROUNDS + 1),
}).strict();
export type ManualParticipantStatus = z.infer<typeof manualParticipantStatusSchema>;
