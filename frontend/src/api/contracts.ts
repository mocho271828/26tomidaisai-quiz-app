import { z } from "zod";

const isoDateTimeSchema = z.iso.datetime({ offset: true });

export const apiErrorSchema = z.strictObject({
  code: z.string(),
  message: z.string(),
});

export const participantPublicLabelSchema = z
  .string()
  .regex(/^[A-Z]{3}$/u);

export const eventStateSchema = z.enum([
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

export const timingBasisSchema = z.enum(["server_received_v1", "client_monotonic_v1"]);

export const eventSchema = z.strictObject({
  event_id: z.uuid(),
  event_title: z.string(),
  event_state: eventStateSchema,
  event_version: z.number().int().min(1),
  start_delay_seconds: z.number().int().min(1),
  timing_basis: timingBasisSchema,
  arrival_grace_ms: z.number().int().nonnegative(),
  clock_tolerance_ms: z.number().int().nonnegative(),
  client_ahead_tolerance_ms: z.number().int().nonnegative(),
});

export const questionOptionSchema = z.strictObject({
  option_id: z.uuid(),
  label: z.string(),
  content: z.string(),
});

export const questionSchema = z.strictObject({
  question_id: z.uuid(),
  prompt: z.string(),
  options: z.array(questionOptionSchema),
});

export const withheldResultSchema = z.strictObject({
  publication: z.literal("Withheld"),
});

export const publishedResultOptionSchema = z.strictObject({
  option_id: z.uuid(),
  label: z.string(),
  content: z.string(),
  is_correct: z.boolean(),
  valid_response_count: z.number().int().nonnegative(),
  response_percentage: z.number().min(0).max(100),
});

export const resultDistributionSchema = z.strictObject({
  valid_answer_count: z.number().int().nonnegative(),
  options: z.array(publishedResultOptionSchema),
});

export const publishedResultSchema = z.strictObject({
  publication: z.literal("Published"),
  correct_option_id: z.uuid(),
  distribution: resultDistributionSchema.nullable(),
});

export const speedCohortSchema = z.enum([
  "Correct",
  "AllValid",
  "Excluded",
]);

export const scoredParticipantOutcomeSchema = z.strictObject({
  status: z.literal("Scored"),
  is_correct: z.boolean(),
  total_points: z.number().int().nonnegative(),
  base_points: z.number().int().nonnegative(),
  speed_points: z.number().int().nonnegative(),
  elapsed_ms: z.number().int().nonnegative().nullable(),
  timing_basis: timingBasisSchema,
  scoring_policy_version: z.string().min(1).max(64),
  speed_cohort: speedCohortSchema,
  speed_rank: z.number().int().min(1).nullable(),
  speed_cohort_size: z.number().int().min(1).nullable(),
}).refine((outcome) => outcome.total_points === outcome.base_points + outcome.speed_points, {
  message: "得点の内訳と合計が一致していません。",
}).refine((outcome) => outcome.timing_basis !== "client_monotonic_v1" || outcome.elapsed_ms !== null,
  { message: "端末計測の正式回答時間がありません。" });

export const unansweredParticipantOutcomeSchema = z.strictObject({
  status: z.literal("Unanswered"),
});

export const participantOutcomeSchema = z.discriminatedUnion("status", [
  scoredParticipantOutcomeSchema,
  unansweredParticipantOutcomeSchema,
]);

export const participantPublishedResultSchema = z.strictObject({
  ...publishedResultSchema.shape,
  outcome: participantOutcomeSchema,
});

export const participantResultSchema = z.discriminatedUnion("publication", [
  withheldResultSchema,
  participantPublishedResultSchema,
]);

export const rankingRowSchema = z.strictObject({
  public_label: participantPublicLabelSchema,
  overall_rank: z.number().int().min(1),
  total_points: z.number().int().nonnegative(),
  correct_count: z.number().int().nonnegative(),
});

export const awardCeremonySchema = z.discriminatedUnion("phase", [
  z.strictObject({ phase: z.literal("Waiting"), top_rows: z.null() }),
  z.strictObject({
    phase: z.literal("Announced"),
    top_rows: z.array(z.strictObject({
      public_label: participantPublicLabelSchema,
      overall_rank: z.number().int().min(1).max(5),
    })),
  }),
  z.strictObject({ phase: z.literal("Finished"), top_rows: z.null() }),
]);

export const participantRankingSchema = z.strictObject({
  snapshot_version: z.number().int().min(1),
  overall_rank: z.number().int().min(1),
  total_points: z.number().int().nonnegative(),
  correct_count: z.number().int().nonnegative(),
  top_rows: z.array(rankingRowSchema.extend({
    overall_rank: z.number().int().min(1).max(3),
  })).nullable(),
});

export const roundStateSchema = z.enum([
  "PreparingAssets",
  "ReadyToSchedule",
  "Scheduled",
  "Open",
  "AwaitingValidation",
  "ReadyToReveal",
  "VoidCandidate",
  "Reveal",
  "Voided",
  "RoundBackupPending",
  "RoundBackupValidated",
  "RoundBackupFailed",
  "RoundBackupRetrying",
]);

const roundTimelineShape = {
  round_id: z.uuid(),
  round_number: z.number().int().min(1),
  version: z.number().int().min(1),
  state_version: z.number().int().min(1),
  state: roundStateSchema,
  open_at: isoDateTimeSchema.nullable(),
  standard_close_at: isoDateTimeSchema.nullable(),
} as const;

export const participantCurrentRoundSchema = z.strictObject({
  ...roundTimelineShape,
  question: questionSchema.optional(),
});

export const participantLatestResultSchema = z.strictObject({
  ...roundTimelineShape,
  question: questionSchema,
  selected_option_id: z.uuid().nullable(),
  result: participantPublishedResultSchema,
});

export const participantCommonLatestResultSchema = participantLatestResultSchema
  .omit({ selected_option_id: true, result: true }).extend({ result: publishedResultSchema });

export const participantResultVisibilitySchema = z.strictObject({
  distribution: z.boolean(), personal_ranking: z.boolean(), overall_ranking: z.boolean(),
});

export const personalResultRevisionSchema = z.strictObject({
  event_id: z.uuid(),
  event_version: z.number().int().min(1),
  result_round_id: z.uuid().nullable(),
  result_round_version: z.number().int().min(1).nullable(),
  ranking_snapshot_version: z.number().int().min(1).nullable(),
  result_visibility: participantResultVisibilitySchema,
}).refine((revision) => (revision.result_round_id === null) === (revision.result_round_version === null), {
  message: "結果Round IDと版の有無が一致していません。",
});

export const personalResultDeliverySchema = z.strictObject({
  status: z.enum(["none", "pending", "ready", "unavailable"]),
  revision: personalResultRevisionSchema.nullable(),
}).refine((delivery) => delivery.status === "none" ? delivery.revision === null
  : (delivery.status !== "pending" && delivery.status !== "ready") || delivery.revision !== null, {
  message: "配信対象の版がありません。",
});

export const personalResultResponseSchema = z.strictObject({
  delivery: personalResultDeliverySchema,
  result: z.strictObject({
    selected_option_id: z.uuid().nullable(),
    outcome: participantOutcomeSchema.nullable(),
    ranking: participantRankingSchema.nullable(),
  }).nullable(),
}).superRefine((response, context) => {
  const revision = response.delivery.revision;
  const result = response.result;
  if ((response.delivery.status === "ready") !== (result !== null)) {
    context.addIssue({ code: "custom", message: "本人結果と配信状態が一致していません。" });
  }
  if (result === null || revision === null) return;
  if ((revision.result_round_id !== null) !== (result.outcome !== null) ||
    (result.outcome === null && result.selected_option_id !== null)) {
    context.addIssue({ code: "custom", message: "本人結果と対象Roundが一致していません。" });
  }
  if ((!revision.result_visibility.personal_ranking && (result.ranking !== null ||
    (result.outcome?.status === "Scored" && (result.outcome.speed_rank !== null || result.outcome.speed_cohort_size !== null)))) ||
    (!revision.result_visibility.overall_ranking && result.ranking?.top_rows != null) ||
    (result.ranking !== null && result.ranking.snapshot_version !== revision.ranking_snapshot_version)) {
    context.addIssue({ code: "custom", message: "本人結果が公開設定または順位版と一致していません。" });
  }
});

export const participantIdentitySchema = z.strictObject({
  public_label: participantPublicLabelSchema,
  is_ranking_eligible: z.boolean(),
});

const participantAnswerObjectSchema = z.strictObject({
  answer_id: z.uuid(),
  round_id: z.uuid(),
  option_id: z.uuid(),
  client_answer_id: z.uuid(),
  received_at: isoDateTimeSchema,
  client_elapsed_ms: z.number().int().nonnegative().nullable(),
  timing_basis: timingBasisSchema,
});

export const participantAnswerSchema = participantAnswerObjectSchema.refine(
  (answer) => answer.timing_basis !== "client_monotonic_v1" || answer.client_elapsed_ms !== null,
  { message: "端末計測の正式回答時間がありません。" },
);

export const acceptedAnswerStatusSchema = z.strictObject({
  status: z.literal("AcceptedLocked"),
  answer: participantAnswerSchema,
});

export const unsavedAnswerStatusSchema = z.strictObject({
  status: z.literal("NotStored"),
  client_answer_id: z.uuid(),
});

export const answerStatusSchema = z.discriminatedUnion("status", [
  acceptedAnswerStatusSchema,
  unsavedAnswerStatusSchema,
]);

export const participantStateSchema = z.strictObject({
  projected_at: isoDateTimeSchema,
  event: eventSchema,
  participant: participantIdentitySchema,
  current_round: participantCurrentRoundSchema.nullable(),
  latest_result: participantCommonLatestResultSchema.nullable(),
  answer: participantAnswerSchema.optional(),
  result_visibility: participantResultVisibilitySchema,
  latest_ranking_snapshot_version: z.number().int().min(1).nullable(),
  personal_result_delivery: personalResultDeliverySchema,
  award_ceremony: awardCeremonySchema.nullable(),
}).refine((state) => (state.event.event_state === "Completed") === (state.award_ceremony !== null), {
  message: "表彰の表示段階と開催状態が一致していません。",
});

export const participantJoinResponseSchema = participantStateSchema;
export const participantSessionResponseSchema = participantStateSchema;

export const participantJoinRequestSchema = z.strictObject({
  client_join_token: z
    .string()
    .length(43)
    .regex(/^[A-Za-z0-9_-]{43}$/u),
});

export const submitAnswerRequestSchema = z.strictObject({
  client_elapsed_ms: z.number().int().min(0).max(9999),
  round_version: z.number().int().min(1),
  option_id: z.uuid(),
  client_answer_id: z.uuid(),
});

export const answerReceiptSchema = z.strictObject({
  ...participantAnswerObjectSchema.shape,
  is_replay: z.boolean(),
}).refine((answer) => answer.timing_basis !== "client_monotonic_v1" || answer.client_elapsed_ms !== null,
  { message: "端末計測の正式回答時間がありません。" });

export const timeResponseSchema = z.strictObject({
  server_time: isoDateTimeSchema,
  shared_sample_age_ms: z.number().finite().nonnegative(),
});

export type EventState = z.infer<typeof eventStateSchema>;
export type ApiError = z.infer<typeof apiErrorSchema>;
export type Event = z.infer<typeof eventSchema>;
export type Question = z.infer<typeof questionSchema>;
export type PublishedResult = z.infer<typeof publishedResultSchema>;
export type ParticipantOutcome = z.infer<typeof participantOutcomeSchema>;
export type ParticipantResult = z.infer<typeof participantResultSchema>;
export type ParticipantRanking = z.infer<typeof participantRankingSchema>;
export type AwardCeremony = z.infer<typeof awardCeremonySchema>;
export type RoundState = z.infer<typeof roundStateSchema>;
export type ParticipantCurrentRound = z.infer<
  typeof participantCurrentRoundSchema
>;
export type ParticipantLatestResult = z.infer<
  typeof participantLatestResultSchema
>;
export type ParticipantAnswer = z.infer<typeof participantAnswerSchema>;
export type AnswerStatus = z.infer<typeof answerStatusSchema>;
export type ParticipantState = z.infer<typeof participantStateSchema>;
export type PersonalResultRevision = z.infer<typeof personalResultRevisionSchema>;
export type PersonalResultDelivery = z.infer<typeof personalResultDeliverySchema>;
export type PersonalResultResponse = z.infer<typeof personalResultResponseSchema>;
export type ParticipantJoinResponse = ParticipantState;
export type ParticipantSessionResponse = ParticipantState;
export type SubmitAnswerRequest = z.infer<typeof submitAnswerRequestSchema>;
export type AnswerReceipt = z.infer<typeof answerReceiptSchema>;
export type TimeResponse = z.infer<typeof timeResponseSchema>;
