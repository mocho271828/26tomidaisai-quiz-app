import { z } from "zod";

import { participantPublicLabelSchema, timingBasisSchema } from "./contracts";

export const operatorResponseTimeQuerySchema = z.strictObject({
  round_id: z.uuid().nullable(),
});

const responseTimeRowSchema = z.strictObject({
  rank: z.number().int().positive(),
  public_label: participantPublicLabelSchema,
  response_ms: z.number().int().nonnegative(),
  is_correct: z.boolean(),
});

const responseTimeGroupSchema = z.strictObject({
  answer_count: z.number().int().nonnegative(),
  average_response_ms: z.number().nonnegative().nullable(),
  median_response_ms: z.number().nonnegative().nullable(),
  fastest_response_ms: z.number().int().nonnegative().nullable(),
  slowest_response_ms: z.number().int().nonnegative().nullable(),
  rows: z.array(responseTimeRowSchema),
}).superRefine((group, context) => {
  const measurements = [group.average_response_ms, group.median_response_ms,
    group.fastest_response_ms, group.slowest_response_ms];
  if (group.answer_count !== group.rows.length ||
      measurements.some((value) => (value === null) !== (group.answer_count === 0))) {
    context.addIssue({ code: "custom", message: "回答数と回答時間の集計が一致していません。" });
  }
  if (new Set(group.rows.map((row) => row.public_label)).size !== group.rows.length) {
    context.addIssue({ code: "custom", message: "参加者IDが重複しています。" });
  }
});

export const operatorResponseTimesSchema = z.strictObject({
  timing_basis: timingBasisSchema,
  rounds: z.array(z.strictObject({
    round_id: z.uuid(),
    round_number: z.number().int().positive(),
    is_practice: z.boolean(),
    prompt: z.string(),
  })),
  selected_round_id: z.uuid().nullable(),
  all_answers: responseTimeGroupSchema,
  correct_answers: responseTimeGroupSchema,
}).superRefine((data, context) => {
  const ids = data.rounds.map((round) => round.round_id);
  if (new Set(ids).size !== ids.length ||
      (data.selected_round_id === null ? ids.length !== 0 : !ids.includes(data.selected_round_id))) {
    context.addIssue({ code: "custom", message: "集計対象の問題が一致していません。" });
  }
  if (data.selected_round_id === null && data.all_answers.answer_count !== 0) {
    context.addIssue({ code: "custom", message: "未確定の問題に回答時間が含まれています。" });
  }
  const allRows = new Map(data.all_answers.rows.map((row) => [row.public_label, row]));
  if (data.correct_answers.answer_count !== data.all_answers.rows.filter((row) => row.is_correct).length ||
      data.correct_answers.rows.some((row) => {
        const source = allRows.get(row.public_label);
        return !row.is_correct || source?.is_correct !== true ||
          source.response_ms !== row.response_ms;
      })) {
    context.addIssue({ code: "custom", message: "正答者の集計が全回答者と一致していません。" });
  }
});

export type OperatorResponseTimes = z.infer<typeof operatorResponseTimesSchema>;
