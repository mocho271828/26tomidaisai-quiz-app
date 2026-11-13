import { z } from "zod";

import {
  LoadTestManifestSchema,
  OptionLabelSchema,
  uuid5,
} from "../contracts.js";
import {
  GENERATOR_ROUND_COUNT,
  GENERATOR_VIRTUAL_USER_COUNT,
  planGeneratorVirtualUser,
} from "./planner-core.js";

const VIRTUAL_USER_COUNT = GENERATOR_VIRTUAL_USER_COUNT;
const ROUND_COUNT = GENERATOR_ROUND_COUNT;

const safeIntegerSchema = z.number().refine(Number.isSafeInteger, "safe integer required");
const nonNegativeIntegerSchema = safeIntegerSchema.refine(
  (value) => value >= 0,
  "non-negative integer required",
);
const unixMillisecondsSchema = nonNegativeIntegerSchema;
const vuIdSchema = safeIntegerSchema.refine(
  (value) => value >= 1 && value <= VIRTUAL_USER_COUNT,
  "VU ID out of range",
);
const roundNumberSchema = safeIntegerSchema.refine(
  (value) => value >= 1 && value <= ROUND_COUNT,
  "round number out of range",
);
const uuid5Schema = z.string().regex(
  /^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}(?![\s\S])/,
);

export const GeneratorVuRoleSchema = z.enum(["answerer", "observer"]);
export type GeneratorVuRole = z.infer<typeof GeneratorVuRoleSchema>;

const disabledStatusDiscardSchema = z.strictObject({
  enabled: z.literal(false),
  discard_only_when_status: z.null(),
  query_delay_ms: z.null(),
  retry_only_when_status: z.null(),
});

const enabledStatusDiscardSchema = z.strictObject({
  enabled: z.literal(true),
  discard_only_when_status: z.literal("Stored"),
  query_delay_ms: nonNegativeIntegerSchema,
  retry_only_when_status: z.literal("NotStored"),
});

export const GeneratorStatusDiscardPlanSchema = z.discriminatedUnion("enabled", [
  disabledStatusDiscardSchema,
  enabledStatusDiscardSchema,
]);
export type GeneratorStatusDiscardPlan = z.infer<
  typeof GeneratorStatusDiscardPlanSchema
>;

export const GeneratorRoundScheduleSchema = z.strictObject({
  round_start_offset_ms: nonNegativeIntegerSchema,
  round_start_at_unix_ms: unixMillisecondsSchema,
  answer_send_offset_ms: z.union([nonNegativeIntegerSchema, z.null()]),
  answer_send_at_unix_ms: z.union([unixMillisecondsSchema, z.null()]),
  replay_send_offset_ms: z.union([nonNegativeIntegerSchema, z.null()]),
  replay_send_at_unix_ms: z.union([unixMillisecondsSchema, z.null()]),
  status_query_offset_ms: z.union([nonNegativeIntegerSchema, z.null()]),
  status_query_at_unix_ms: z.union([unixMillisecondsSchema, z.null()]),
});
export type GeneratorRoundSchedule = z.infer<
  typeof GeneratorRoundScheduleSchema
>;

export const GeneratorVuRoundPlanSchema = z
  .strictObject({
    round_number: roundNumberSchema,
    question_id: z.uuid(),
    role: GeneratorVuRoleSchema,
    selected_option: z.union([OptionLabelSchema, z.null()]),
    client_answer_id: uuid5Schema,
    replay_required: z.boolean(),
    status_discard: GeneratorStatusDiscardPlanSchema,
    schedule: GeneratorRoundScheduleSchema,
  })
  .superRefine((round, context) => {
    const isAnswerer = round.role === "answerer";
    const hasAnswerSchedule =
      round.schedule.answer_send_offset_ms !== null &&
      round.schedule.answer_send_at_unix_ms !== null;
    const hasReplaySchedule =
      round.schedule.replay_send_offset_ms !== null &&
      round.schedule.replay_send_at_unix_ms !== null;
    const hasStatusSchedule =
      round.schedule.status_query_offset_ms !== null &&
      round.schedule.status_query_at_unix_ms !== null;
    if (isAnswerer !== (round.selected_option !== null) || isAnswerer !== hasAnswerSchedule) {
      context.addIssue({ code: "custom", message: "answerer fields are inconsistent" });
    }
    if (round.replay_required !== hasReplaySchedule || (!isAnswerer && round.replay_required)) {
      context.addIssue({ code: "custom", message: "replay fields are inconsistent" });
    }
    if (round.status_discard.enabled !== hasStatusSchedule || (!isAnswerer && hasStatusSchedule)) {
      context.addIssue({ code: "custom", message: "status fields are inconsistent" });
    }
  });
export type GeneratorVuRoundPlan = z.infer<typeof GeneratorVuRoundPlanSchema>;

export const GeneratorVuPlanSchema = z.strictObject({
  vu_id: vuIdSchema,
  session_start_offset_ms: nonNegativeIntegerSchema,
  session_stop_offset_ms: nonNegativeIntegerSchema,
  rounds: z.array(GeneratorVuRoundPlanSchema).length(ROUND_COUNT),
});
export type GeneratorVuPlan = z.infer<typeof GeneratorVuPlanSchema>;

export const GeneratorExecutionPlanSchema = z.strictObject({
  format: z.literal("load-test-generator-execution-plan-v1"),
  run_id: z.uuid(),
  scheduled_start_at: z.string(),
  virtual_user_count: z.literal(VIRTUAL_USER_COUNT),
  round_count: z.literal(ROUND_COUNT),
  virtual_users: z.array(GeneratorVuPlanSchema).length(VIRTUAL_USER_COUNT),
});
export type GeneratorExecutionPlan = z.infer<typeof GeneratorExecutionPlanSchema>;

export type GeneratorPlannerRefusalCode =
  | "generator_plan_failed"
  | "generator_plan_manifest_invalid";

/** manifest内容をmessageへ転記せず、固定codeだけでplanner失敗を通知する。 */
export class GeneratorPlannerRefusal extends Error {
  readonly code: GeneratorPlannerRefusalCode;

  constructor(code: GeneratorPlannerRefusalCode) {
    super(code);
    this.name = "GeneratorPlannerRefusal";
    this.code = code;
  }
}

/**
 * 検証済みmanifestだけから600 VU×10 Roundの実行計画を生成する。
 * participant I/Oや現在時刻に依存しないため、同じmanifestは常に同じ計画になる。
 */
export function buildGeneratorExecutionPlan(
  manifestValue: unknown,
): GeneratorExecutionPlan {
  const manifestResult = LoadTestManifestSchema.safeParse(manifestValue);
  if (!manifestResult.success) {
    throw new GeneratorPlannerRefusal("generator_plan_manifest_invalid");
  }
  const manifest = manifestResult.data;
  const scheduledStartMilliseconds = Date.parse(manifest.scheduled_start_at);
  if (!Number.isSafeInteger(scheduledStartMilliseconds) || scheduledStartMilliseconds < 0) {
    throw new GeneratorPlannerRefusal("generator_plan_manifest_invalid");
  }

  const planValue = {
    format: "load-test-generator-execution-plan-v1" as const,
    run_id: manifest.run_id,
    scheduled_start_at: manifest.scheduled_start_at,
    virtual_user_count: VIRTUAL_USER_COUNT,
    round_count: ROUND_COUNT,
    virtual_users: Array.from({ length: VIRTUAL_USER_COUNT }, (_, index) =>
      planGeneratorVirtualUser(manifest, index + 1, (runId, questionId, vuId) =>
        uuid5(runId, `${questionId}:${vuId}`),
      ),
    ),
  };
  const planResult = GeneratorExecutionPlanSchema.safeParse(planValue);
  if (!planResult.success) {
    throw new GeneratorPlannerRefusal("generator_plan_failed");
  }
  return planResult.data;
}
