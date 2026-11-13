import { z } from "zod";

import { PARTICIPANT_PUBLIC_LABEL_PATTERN } from "../participant-public-label.js";

// participant generatorはfrontendと同じ公開契約だけを複製し、Node専用APIへ依存しない。
const isoDateTimeSchema = z.iso.datetime({ offset: true });

const eventStateSchema = z.enum([
  "Draft",
  "Checked",
  "LobbyOpen",
  "InProgress",
  "Suspended",
  "DataStoreUnavailable",
  "ServiceLost",
  "Completed",
  "Aborted",
  "Archived",
]);

const eventSchema = z.strictObject({
  event_id: z.uuid(),
  event_title: z.string(),
  event_state: eventStateSchema,
  event_version: z.number().int().min(1),
  start_delay_seconds: z.number().int().positive(),
});

const questionOptionSchema = z.strictObject({
  option_id: z.uuid(),
  label: z.string(),
  content: z.string(),
});

export const publicQuestionSchema = z
  .strictObject({
    question_id: z.uuid(),
    prompt: z.string(),
    options: z.array(questionOptionSchema),
  });

const publishedResultOptionSchema = z.strictObject({
  option_id: z.uuid(),
  label: z.string(),
  content: z.string(),
  is_correct: z.boolean(),
  valid_response_count: z.number().int().nonnegative(),
  response_percentage: z.number().min(0).max(100),
});

const speedCohortSchema = z.enum(["Correct", "AllValid", "Excluded"]);

const scoredParticipantOutcomeSchema = z.strictObject({
  status: z.literal("Scored"),
  is_correct: z.boolean(),
  total_points: z.number().int().nonnegative(),
  scoring_policy_version: z.string().min(1).max(64),
  speed_cohort: speedCohortSchema,
  speed_rank: z.number().int().min(1).nullable(),
  speed_cohort_size: z.number().int().min(1).nullable(),
});

const unansweredParticipantOutcomeSchema = z.strictObject({
  status: z.literal("Unanswered"),
});

const participantOutcomeSchema = z.discriminatedUnion("status", [
  scoredParticipantOutcomeSchema,
  unansweredParticipantOutcomeSchema,
]);

const participantPublishedResultSchema = z.strictObject({
  publication: z.literal("Published"),
  correct_option_id: z.uuid(),
  distribution: z.strictObject({
    valid_answer_count: z.number().int().nonnegative(),
    options: z.array(publishedResultOptionSchema),
  }).nullable(),
  outcome: participantOutcomeSchema,
});


export const roundStateSchema = z.enum([
  "PreparingAssets",
  "ReadyToSchedule",
  "Scheduled",
  "Open",
  "ReadyToReveal",
  "AwaitingValidation",
  "VoidCandidate",
  "Reveal",
  "Voided",
  "RoundBackupPending",
  "RoundBackupValidated",
  "RoundBackupFailed",
  "RoundBackupRetrying",
]);

export const participantRoundSchema = z.strictObject({
  round_id: z.uuid(),
  round_number: z.number().int().positive(),
  version: z.number().int().min(1),
  state_version: z.number().int().min(1),
  state: roundStateSchema,
  open_at: isoDateTimeSchema.nullable(),
  standard_close_at: isoDateTimeSchema.nullable(),
  question: publicQuestionSchema.optional(),
});

const participantIdentitySchema = z.strictObject({
  public_label: z.string().regex(PARTICIPANT_PUBLIC_LABEL_PATTERN),
  is_ranking_eligible: z.boolean(),
});

export const participantAnswerSchema = z.strictObject({
  answer_id: z.uuid(),
  round_id: z.uuid(),
  option_id: z.uuid(),
  client_answer_id: z.uuid(),
  received_at: isoDateTimeSchema,
});

const participantRankingSchema = z.strictObject({
  snapshot_version: z.number().int().min(1),
  overall_rank: z.number().int().min(1),
  total_points: z.number().int().nonnegative(),
  correct_count: z.number().int().nonnegative(),
  top_rows: z.array(z.strictObject({
    public_label: z.string().regex(PARTICIPANT_PUBLIC_LABEL_PATTERN),
    overall_rank: z.number().int().min(1).max(3),
    total_points: z.number().int().nonnegative(),
    correct_count: z.number().int().nonnegative(),
  })).nullable(),
});

export const participantStateSchema = z.strictObject({
  projected_at: isoDateTimeSchema,
  event: eventSchema,
  participant: participantIdentitySchema,
  current_round: participantRoundSchema.nullable(),
  latest_result: participantRoundSchema.extend({
    question: publicQuestionSchema,
    selected_option_id: z.uuid().nullable(),
    result: participantPublishedResultSchema,
  }).nullable(),
  answer: participantAnswerSchema.optional(),
  ranking: participantRankingSchema.optional(),
});

export const participantJoinRequestSchema = z.strictObject({
  client_join_token: z
    .string()
    .length(43)
    .regex(/^[A-Za-z0-9_-]{43}$/u),
  public_label: z.string().regex(PARTICIPANT_PUBLIC_LABEL_PATTERN),
});

export const readinessRequestSchema = z.strictObject({
  round_version: z.number().int().min(1),
  is_ready: z.boolean(),
  has_asset_failure: z.boolean().default(false),
});

export const readinessAcknowledgementSchema = z.strictObject({
  round_id: z.uuid(),
  round_version: z.number().int().min(1),
  round_state: roundStateSchema,
  state_version: z.number().int().min(1),
});

export const submitAnswerRequestSchema = z.strictObject({
  round_version: z.number().int().min(1),
  option_id: z.uuid(),
  client_answer_id: z.uuid(),
});

export const answerReceiptSchema = z.strictObject({
  ...participantAnswerSchema.shape,
  is_replay: z.boolean(),
});

const acceptedAnswerStatusSchema = z.strictObject({
  status: z.literal("AcceptedLocked"),
  answer: participantAnswerSchema,
});

const unsavedAnswerStatusSchema = z.strictObject({
  status: z.literal("NotStored"),
  client_answer_id: z.uuid(),
});

export const answerStatusSchema = z.discriminatedUnion("status", [
  acceptedAnswerStatusSchema,
  unsavedAnswerStatusSchema,
]);

export const timeResponseSchema = z.strictObject({
  server_time: isoDateTimeSchema,
});

const stateInvalidatedMessageSchema = z.strictObject({
  type: z.literal("state.invalidated"),
  audience: z.enum(["participant", "operator"]),
  event_id: z.uuid(),
});

const pollingRequiredMessageSchema = z.strictObject({
  type: z.literal("polling.required"),
  reason: z.literal("realtime_unavailable"),
  poll_after_ms: z.literal(1_000),
});

export const realtimeServerMessageSchema = z.discriminatedUnion("type", [
  stateInvalidatedMessageSchema,
  pollingRequiredMessageSchema,
]);

export type ParticipantState = z.infer<typeof participantStateSchema>;
export type ParticipantRound = z.infer<typeof participantRoundSchema>;
export type ParticipantAnswer = z.infer<typeof participantAnswerSchema>;
export type ParticipantJoinRequest = z.infer<typeof participantJoinRequestSchema>;
export type RoundState = z.infer<typeof roundStateSchema>;
export type ReadinessRequest = z.infer<typeof readinessRequestSchema>;
export type ReadinessAcknowledgement = z.infer<
  typeof readinessAcknowledgementSchema
>;
export type SubmitAnswerRequest = z.infer<typeof submitAnswerRequestSchema>;
export type AnswerReceipt = z.infer<typeof answerReceiptSchema>;
export type AnswerStatus = z.infer<typeof answerStatusSchema>;
export type TimeResponse = z.infer<typeof timeResponseSchema>;
export type RealtimeServerMessage = z.infer<typeof realtimeServerMessageSchema>;

export type GeneratorPayloadRefusalCode =
  | "generator_payload_contract_invalid"
  | "generator_payload_json_invalid";

/** 応答本文や識別子を保持せず、固定codeだけで外部payload拒否を通知する。 */
export class GeneratorPayloadRefusalError extends Error {
  readonly code: GeneratorPayloadRefusalCode;

  constructor(code: GeneratorPayloadRefusalCode) {
    super(code);
    this.name = "GeneratorPayloadRefusalError";
    this.code = code;
  }
}

/** JSONと公開契約を境界で検証し、失敗時は機密値を含まない固定codeだけを返す。 */
export function decodeApiJson<Schema extends z.ZodType>(
  schema: Schema,
  body: string,
): z.output<Schema> {
  let decoded: unknown;
  try {
    decoded = JSON.parse(body);
  } catch {
    throw new GeneratorPayloadRefusalError("generator_payload_json_invalid");
  }

  const result = schema.safeParse(decoded);
  if (!result.success) {
    throw new GeneratorPayloadRefusalError("generator_payload_contract_invalid");
  }
  return result.data;
}
