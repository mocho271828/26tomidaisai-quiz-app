import { z } from "zod";

export const CANARY_PREFIX = "@@LOAD_TEST_CANARY@@";
export const CANARY_DEADLINE_MILLISECONDS = 30_000;
export const CanaryCompletionSchema = z.strictObject({
  format: z.literal("load-test-generator-canary-v1"),
  run_id: z.uuid(),
  event_id: z.uuid(),
  manifest_sha256: z.string().regex(/^[0-9a-f]{64}$/u),
  vu_id: z.union([z.literal(1), z.literal(2)]),
  join_checked: z.literal(true),
  session_checked: z.literal(true),
  readiness_checked: z.literal(true),
  websocket_checked: z.literal(true),
  observed_at_unix_ms: z.number().int().safe().nonnegative(),
});
export type CanaryCompletion = z.output<typeof CanaryCompletionSchema>;
