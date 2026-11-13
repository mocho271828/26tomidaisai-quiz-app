import { z } from "zod";

import {
  eventStateSchema,
  participantPublicLabelSchema,
  roundStateSchema,
} from "./contracts";

const isoDateTimeSchema = z.iso.datetime({ offset: true });

export const adminRoleSchema = z.enum(["Operator", "Supervisor"]);
export const resultVisibilityTargetSchema = z.enum([
  "distribution",
  "personal_ranking",
  "overall_ranking",
  "rankings",
]);
export const resultVisibilitySchema = z.strictObject({
  distribution: z.boolean(),
  personal_ranking: z.boolean(),
  overall_ranking: z.boolean(),
});
const visibilityControlSchema = z.strictObject({
  mode: z.enum(["manual", "automatic"]),
  initially_visible: z.boolean(),
  changes: z.array(
    z.strictObject({
      after_round: z.number().int().min(1),
      visible: z.boolean(),
    }),
  ),
}).superRefine((control, context) => {
  if ((control.mode === "manual") !== (control.changes.length === 0)) {
    context.addIssue({
      code: "custom",
      message: "自動切替では変更予定が必要で、手動切替では設定できません。",
      path: ["changes"],
    });
  }
  const hasNonIncreasingRounds = control.changes.some((change, index) => {
    const previous = control.changes[index - 1];
    return previous !== undefined && change.after_round <= previous.after_round;
  });
  if (hasNonIncreasingRounds) {
    context.addIssue({
      code: "custom",
      message: "変更予定の終了問は重複のない昇順で指定してください。",
      path: ["changes"],
    });
  }
});
export const visibilityControlsSchema = z.strictObject({
  distribution: visibilityControlSchema,
  personal_ranking: visibilityControlSchema,
  overall_ranking: visibilityControlSchema,
});
export const roundResultPublicationSchema = z.enum(["Withheld", "Published"]);
const automaticResultFailureCodeSchema = z.string().regex(
  /^automatic_aggregate_(?:integrity_failure|domain_failure(?::[a-z][a-z0-9_]{0,79})?)$/,
);

export const operatorLoginRequestSchema = z.strictObject({
  username: z.string().trim().min(1).max(80),
  password: z.string().min(1).max(256),
});

export const operatorIdentitySchema = z.strictObject({
  account_id: z.uuid(),
  username: z.string(),
  role: adminRoleSchema,
});

export const operatorSessionSchema = z.strictObject({
  operator: operatorIdentitySchema,
});

export const operatorOptionSchema = z.strictObject({
  option_id: z.uuid(),
  label: z.string(),
  content: z.string(),
  is_correct: z.boolean(),
});

export const operatorQuestionSchema = z
  .strictObject({
    question_id: z.uuid(),
    version: z.number().int().min(1),
    prompt: z.string(),
    options: z.array(operatorOptionSchema),
  })
  .superRefine((question, context) => {
    const seenOptionIds = new Set<string>();
    question.options.forEach((option, index) => {
      if (seenOptionIds.has(option.option_id)) {
        context.addIssue({
          code: "custom",
          message: "選択肢IDは一意である必要があります。",
          path: ["options", index, "option_id"],
        });
      }
      seenOptionIds.add(option.option_id);
    });
  });

export const operatorQuestionPreviewSchema = operatorQuestionSchema.safeExtend({
  display_order: z.number().int(),
});

export const operatorOptionStatisticsSchema = z
  .strictObject({
    option_id: z.uuid(),
    answer_count: z.number().int().nonnegative(),
    average_response_ms: z.number().nonnegative().nullable(),
  })
  .superRefine((statistics, context) => {
    const hasAnswers = statistics.answer_count > 0;
    const hasAverage = statistics.average_response_ms !== null;
    if (hasAnswers !== hasAverage) {
      context.addIssue({
        code: "custom",
        message: "回答数と平均回答速度の有無が一致していません。",
        path: ["average_response_ms"],
      });
    }
  });

export const operatorRoundStatisticsSchema = z
  .strictObject({
    answer_count: z.number().int().nonnegative(),
    average_response_ms: z.number().nonnegative().nullable(),
    options: z.array(operatorOptionStatisticsSchema),
  })
  .superRefine((statistics, context) => {
    const seenOptionIds = new Set<string>();
    statistics.options.forEach((option, index) => {
      if (seenOptionIds.has(option.option_id)) {
        context.addIssue({
          code: "custom",
          message: "選択肢別統計のIDは一意である必要があります。",
          path: ["options", index, "option_id"],
        });
      }
      seenOptionIds.add(option.option_id);
    });

    const optionAnswerCount = statistics.options.reduce(
      (total, option) => total + option.answer_count,
      0,
    );
    if (statistics.answer_count !== optionAnswerCount) {
      context.addIssue({
        code: "custom",
        message: "全体回答数は選択肢別回答数の合計と一致する必要があります。",
        path: ["answer_count"],
      });
    }

    const hasAnswers = statistics.answer_count > 0;
    const hasAverage = statistics.average_response_ms !== null;
    if (hasAnswers !== hasAverage) {
      context.addIssue({
        code: "custom",
        message: "全体回答数と平均回答速度の有無が一致していません。",
        path: ["average_response_ms"],
      });
    }
  });

export const operatorRankingRowSchema = z.strictObject({
  overall_rank: z.number().int().min(1),
  public_label: participantPublicLabelSchema,
  total_points: z.number().int().nonnegative(),
  correct_count: z.number().int().nonnegative(),
});

export const operatorLatestRankingSchema = z.strictObject({
  snapshot_version: z.number().int().min(1),
  round_id: z.uuid(),
  rows: z.array(operatorRankingRowSchema),
});

export const operatorLatestRankingResponseSchema =
  operatorLatestRankingSchema.nullable();

export const operatorRoundSchema = z
  .strictObject({
    round_id: z.uuid(),
    version: z.number().int().min(1),
    state_version: z.number().int().min(1),
    state: roundStateSchema,
    open_at: isoDateTimeSchema.nullable(),
    standard_close_at: isoDateTimeSchema.nullable(),
    can_publish_results: z.boolean(),
    result_publication: roundResultPublicationSchema,
    result_failure_code: automaticResultFailureCodeSchema.nullable(),
    question: operatorQuestionSchema,
    statistics: operatorRoundStatisticsSchema,
  })
  .superRefine((round, context) => {
    const questionOptionIds = new Set(
      round.question.options.map((option) => option.option_id),
    );
    const statisticsOptionIds = new Set(
      round.statistics.options.map((option) => option.option_id),
    );
    const hasSameOptionIds =
      questionOptionIds.size === statisticsOptionIds.size &&
      [...questionOptionIds].every((optionId) =>
        statisticsOptionIds.has(optionId),
      );
    if (!hasSameOptionIds) {
      context.addIssue({
        code: "custom",
        message: "選択肢と選択肢別統計のIDは1対1で一致する必要があります。",
        path: ["statistics", "options"],
      });
    }
    const hasResultFailure = round.result_failure_code !== null;
    if ((round.state === "VoidCandidate") !== hasResultFailure) {
      context.addIssue({
        code: "custom",
        message:
          "結果失敗コードはVoidCandidateのときだけ必須である必要があります。",
        path: ["result_failure_code"],
      });
    }
  });

export const operatorStateSchema = z.strictObject({
  event_id: z.uuid(),
  projected_at: isoDateTimeSchema,
  event_title: z.string(),
  event_state: eventStateSchema,
  award_ceremony_phase: z.enum(["Waiting", "Announced", "Finished"]).nullable(),
  event_version: z.number().int().min(1),
  start_delay_seconds: z.number().int().min(1),
  result_visibility: resultVisibilitySchema,
  visibility_controls: visibilityControlsSchema,
  participant_count: z.number().int().nonnegative(),
  available_questions: z.array(operatorQuestionPreviewSchema),
  used_question_ids: z.array(z.uuid()),
  current_round: operatorRoundSchema.nullable(),
  latest_ranking_snapshot_version: z.number().int().min(1).nullable(),
}).superRefine((state, context) => {
  if ((state.event_state === "Completed") !== (state.award_ceremony_phase !== null)) {
    context.addIssue({ code: "custom", path: ["award_ceremony_phase"], message: "表彰状態はイベント終了後だけ必須です。" });
  }
});

export const operatorMutationRequestSchema = z.strictObject({
  expected_version: z.number().int().min(1),
  idempotency_key: z.uuid(),
});

export const resultVisibilityMutationRequestSchema =
  operatorMutationRequestSchema.extend({
    target: resultVisibilityTargetSchema,
    visible: z.boolean(),
  });

export const questionSelectionMutationRequestSchema =
  operatorMutationRequestSchema.extend({
    question_id: z.uuid(),
  });

export const advanceRoundMutationRequestSchema =
  operatorMutationRequestSchema.extend({
    question_id: z.uuid(),
  });

export const operatorMutationResponseSchema = z.strictObject({
  operation: z.enum([
    "begin_preparation",
    "select_round_question",
    "schedule_round",
    "retry_round_results",
    "publish_round_results",
    "set_result_visibility",
    "set_final_round",
    "set_award_ceremony",
    "advance_round",
    "complete_event",
  ]),
  event_id: z.uuid(),
  event_version: z.number().int().min(1),
  round_id: z.uuid().nullable(),
  round_version: z.number().int().min(1).nullable(),
  round_state: roundStateSchema.nullable(),
  result_visibility: resultVisibilitySchema,
  open_at: isoDateTimeSchema.nullable(),
  standard_close_at: isoDateTimeSchema.nullable(),
  valid_answer_count: z.number().int().nonnegative().nullable(),
  ranking_row_count: z.number().int().nonnegative().nullable(),
  is_replay: z.boolean(),
});

export type OperatorLoginRequest = z.infer<typeof operatorLoginRequestSchema>;
export type OperatorLatestRanking = z.infer<
  typeof operatorLatestRankingSchema
>;
export type OperatorQuestion = z.infer<typeof operatorQuestionSchema>;
export type OperatorRoundStatistics = z.infer<
  typeof operatorRoundStatisticsSchema
>;
export type OperatorSession = z.infer<typeof operatorSessionSchema>;
export type OperatorState = z.infer<typeof operatorStateSchema>;
export type OperatorMutationRequest = z.infer<
  typeof operatorMutationRequestSchema
>;
export type ResultVisibilityTarget = z.infer<typeof resultVisibilityTargetSchema>;
export type QuestionSelectionMutationRequest = z.infer<
  typeof questionSelectionMutationRequestSchema
>;
export type AdvanceRoundMutationRequest = z.infer<
  typeof advanceRoundMutationRequestSchema
>;
export type OperatorMutationResponse = z.infer<
  typeof operatorMutationResponseSchema
>;
