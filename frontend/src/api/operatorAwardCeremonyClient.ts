import { z } from "zod";

import { requestJson } from "./client";
import { csrfHeaders } from "./operatorClient";
import { operatorMutationRequestSchema, operatorMutationResponseSchema, type OperatorMutationRequest, type OperatorMutationResponse } from "./operatorContracts";

export const awardCeremonyMutationSchema = operatorMutationRequestSchema.extend({
  phase: z.enum(["Announced", "Finished"]),
});
export type AwardCeremonyTarget = z.infer<typeof awardCeremonyMutationSchema>["phase"];

export function setAwardCeremony(
  phase: AwardCeremonyTarget,
  mutation: OperatorMutationRequest,
  signal?: AbortSignal,
): Promise<OperatorMutationResponse> {
  const request = awardCeremonyMutationSchema.parse({ ...mutation, phase });
  return requestJson("/operator/award-ceremony", operatorMutationResponseSchema, {
    method: "POST", body: request, headers: csrfHeaders(), signal,
  });
}
