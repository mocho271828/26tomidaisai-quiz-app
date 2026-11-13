import { z } from "zod";

export const SESSION_CONNECTION_PREFIX = "@@LOAD_TEST_SESSION_CONNECTION@@";
export const RAMP_READY_ARTIFACT_NAME = "generator-ramp-ready.json";
export const RAMP_READY_DEADLINE_MILLISECONDS = 660_000;
export const SessionConnectionSchema = z.strictObject({
  format: z.literal("load-test-generator-session-connection-v1"),
  run_id: z.uuid(),
  event_id: z.uuid(),
  manifest_sha256: z.string().regex(/^[0-9a-f]{64}$/u),
  vu_id: z.number().int().min(1).max(600),
  session_checked: z.literal(true),
  websocket_connected: z.boolean(),
  observed_at_unix_ms: z.number().int().safe().nonnegative(),
});
export type SessionConnection = z.output<typeof SessionConnectionSchema>;
