import { z } from "zod";

import {
  eventSchema,
  awardCeremonySchema,
  participantCurrentRoundSchema,
  participantCommonLatestResultSchema,
  personalResultDeliverySchema,
  personalResultResponseSchema,
} from "./contracts";
import { operatorStateSchema, resultVisibilitySchema } from "./operatorContracts";

export const realtimeAudienceSchema = z.enum(["participant", "operator"]);

export const participantCommonStateSchema = z.strictObject({
  projected_at: z.iso.datetime({ offset: true }),
  event: eventSchema,
  current_round: participantCurrentRoundSchema.nullable(),
  latest_result: participantCommonLatestResultSchema.nullable(),
  result_visibility: resultVisibilitySchema,
  latest_ranking_snapshot_version: z.number().int().min(1).nullable(),
  personal_result_delivery: personalResultDeliverySchema,
  award_ceremony: awardCeremonySchema.nullable(),
}).refine((state) => (state.event.event_state === "Completed") === (state.award_ceremony !== null), {
  message: "表彰の表示段階と開催状態が一致していません。",
});

const streamShape = {
  event_id: z.uuid(),
  stream_id: z.uuidv4(),
  sequence: z.number().int().min(1),
} as const;

export const participantStateMessageSchema = z.strictObject({
  type: z.literal("participant.state"),
  audience: z.literal("participant"),
  ...streamShape,
  state: participantCommonStateSchema,
}).refine((message) => message.event_id === message.state.event.event_id, {
  message: "開催IDが状態と一致していません。",
});

export const operatorStateMessageSchema = z.strictObject({
  type: z.literal("operator.state"),
  audience: z.literal("operator"),
  ...streamShape,
  state: operatorStateSchema,
}).refine((message) => message.event_id === message.state.event_id, {
  message: "開催IDが状態と一致していません。",
});

export const heartbeatMessageSchema = z.strictObject({
  type: z.literal("heartbeat"),
  audience: realtimeAudienceSchema,
  ...streamShape,
  personal_result_delivery: personalResultDeliverySchema.optional(),
}).refine((message) => message.audience === "participant" || message.personal_result_delivery === undefined, {
  message: "運営heartbeatに本人結果の配信状態を含められません。",
}).refine((message) => message.personal_result_delivery?.revision == null ||
  message.personal_result_delivery.revision.event_id === message.event_id, {
  message: "本人結果の配信状態と開催IDが一致していません。",
});

export const participantResultMessageSchema = z.strictObject({
  type: z.literal("participant.result"),
  audience: z.literal("participant"),
  ...streamShape,
  result: personalResultResponseSchema,
}).refine((message) => message.result.delivery.revision === null ||
  message.result.delivery.revision.event_id === message.event_id, {
  message: "本人結果と開催IDが一致していません。",
});

export const sessionChangedMessageSchema = z.strictObject({
  type: z.literal("session.changed"),
  audience: realtimeAudienceSchema,
  ...streamShape,
});

export const pollingRequiredMessageSchema = z.strictObject({
  type: z.literal("polling.required"),
  reason: z.literal("realtime_unavailable"),
  poll_after_ms: z.literal(5_000),
});

export const realtimeServerMessageSchema = z.discriminatedUnion("type", [
  participantStateMessageSchema,
  participantResultMessageSchema,
  operatorStateMessageSchema,
  heartbeatMessageSchema,
  sessionChangedMessageSchema,
  pollingRequiredMessageSchema,
]);

export type ParticipantCommonState = z.infer<typeof participantCommonStateSchema>;
export type RealtimeAudience = z.infer<typeof realtimeAudienceSchema>;
export type RealtimeServerMessage = z.infer<typeof realtimeServerMessageSchema>;
export type RealtimeStateMessage = Extract<RealtimeServerMessage, { type: "participant.state" | "operator.state" }>;
