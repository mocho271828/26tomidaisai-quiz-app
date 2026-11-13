import { requestJson } from "./client";
import { csrfHeaders } from "./operatorClient";
import { operatorMutationResponseSchema, type OperatorMutationRequest, type OperatorMutationResponse } from "./operatorContracts";
import { operatorFinalRoundMutationSchema, operatorFinalRoundSchema, type OperatorFinalRound } from "./operatorFinalRoundContracts";

export type { OperatorFinalRound } from "./operatorFinalRoundContracts";

export function getOperatorFinalRound(signal?: AbortSignal): Promise<OperatorFinalRound> {
  return requestJson("/operator/final-round", operatorFinalRoundSchema, { signal });
}

export function setOperatorFinalRound(
  roundId: string,
  isFinal: boolean,
  mutation: OperatorMutationRequest,
  signal?: AbortSignal,
): Promise<OperatorMutationResponse> {
  const request = operatorFinalRoundMutationSchema.parse({ ...mutation, round_id: roundId, is_final: isFinal });
  return requestJson("/operator/final-round", operatorMutationResponseSchema, {
    method: "POST", body: request, headers: csrfHeaders(), signal,
  });
}
