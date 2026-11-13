import { z } from "zod";

import { operatorMutationRequestSchema } from "./operatorContracts";

export const operatorFinalRoundSchema = z.strictObject({
  event_id: z.uuid(),
  event_version: z.number().int().positive(),
  final_round_id: z.uuid().nullable(),
});

export const operatorFinalRoundMutationSchema = operatorMutationRequestSchema.extend({
  round_id: z.uuid(),
  is_final: z.boolean(),
});

export type OperatorFinalRound = z.infer<typeof operatorFinalRoundSchema>;
