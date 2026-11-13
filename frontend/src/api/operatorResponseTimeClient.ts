import { requestJson } from "./client";
import {
  operatorResponseTimeQuerySchema,
  operatorResponseTimesSchema,
  type OperatorResponseTimes,
} from "./operatorResponseTimeContracts";

export function getOperatorResponseTimes(
  roundId: string | null,
  signal?: AbortSignal,
): Promise<OperatorResponseTimes> {
  const query = operatorResponseTimeQuerySchema.parse({ round_id: roundId });
  const parameters = new URLSearchParams();
  if (query.round_id !== null) parameters.set("round_id", query.round_id);
  const queryString = parameters.size === 0 ? "" : `?${parameters}`;
  return requestJson(`/operator/response-times${queryString}`, operatorResponseTimesSchema, {
    signal: signal === undefined ? AbortSignal.timeout(5000) : AbortSignal.any([signal, AbortSignal.timeout(5000)]),
  });
}
