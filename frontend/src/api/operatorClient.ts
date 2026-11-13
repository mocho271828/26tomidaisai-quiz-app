import { z } from "zod";

import { requestEmpty, requestJson } from "./client";
import {
  advanceRoundMutationRequestSchema,
  operatorLoginRequestSchema,
  operatorLatestRankingResponseSchema,
  operatorMutationRequestSchema,
  operatorMutationResponseSchema,
  questionSelectionMutationRequestSchema,
  operatorSessionSchema,
  operatorStateSchema,
  resultVisibilityMutationRequestSchema,
  type OperatorLoginRequest,
  type OperatorLatestRanking,
  type OperatorMutationRequest,
  type OperatorMutationResponse,
  type OperatorSession,
  type OperatorState,
  type ResultVisibilityTarget,
} from "./operatorContracts";

const uuidSchema = z.uuid();
const csrfTokenSchema = z.string().min(1).max(512);
const CSRF_COOKIE_NAME = "quiz_operator_csrf";

function checkedUuid(id: string): string {
  return uuidSchema.parse(id);
}

export function csrfHeaders(): HeadersInit {
  const token = readCookie(CSRF_COOKIE_NAME);
  if (token === null) {
    throw new Error("運営操作用のCSRF tokenが見つかりません。");
  }
  return { "X-CSRF-Token": csrfTokenSchema.parse(token) };
}

export function readCookie(name: string): string | null {
  const prefix = `${name}=`;
  const match = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(prefix));
  if (match === undefined) {
    return null;
  }
  try {
    return decodeURIComponent(match.slice(prefix.length));
  } catch (error: unknown) {
    if (error instanceof URIError) {
      return null;
    }
    throw error;
  }
}

export function loginOperator(value: OperatorLoginRequest): Promise<OperatorSession> {
  const request = operatorLoginRequestSchema.parse(value);
  return requestJson("/operator/auth/login", operatorSessionSchema, {
    method: "POST",
    body: request,
  });
}

export function getOperatorSession(signal?: AbortSignal): Promise<OperatorSession> {
  return requestJson("/operator/auth/session", operatorSessionSchema, { signal });
}

export function logoutOperator(): Promise<void> {
  return requestEmpty("/operator/auth/logout", {
    method: "POST",
    headers: csrfHeaders(),
  });
}

export function getOperatorState(
  signal?: AbortSignal,
): Promise<OperatorState> {
  return requestJson("/operator/state", operatorStateSchema, { signal });
}

export function getOperatorRanking(
  signal?: AbortSignal,
): Promise<OperatorLatestRanking | null> {
  return requestJson("/operator/ranking", operatorLatestRankingResponseSchema, {
    signal,
  });
}

type RoundOperation = "prepare" | "schedule" | "publish-results";

export function mutateRound(
  roundId: string,
  operation: RoundOperation,
  value: OperatorMutationRequest,
): Promise<OperatorMutationResponse> {
  const request = operatorMutationRequestSchema.parse(value);
  return requestJson(
    `/operator/rounds/${checkedUuid(roundId)}/${operation}`,
    operatorMutationResponseSchema,
    { method: "POST", body: request, headers: csrfHeaders() },
  );
}

export function retryRoundResults(
  roundId: string,
  value: OperatorMutationRequest,
): Promise<OperatorMutationResponse> {
  const request = operatorMutationRequestSchema.parse(value);
  return requestJson(
    `/operator/rounds/${checkedUuid(roundId)}/retry-results`,
    operatorMutationResponseSchema,
    { method: "POST", body: request, headers: csrfHeaders() },
  );
}

export function selectRoundQuestion(
  roundId: string,
  questionId: string,
  value: OperatorMutationRequest,
): Promise<OperatorMutationResponse> {
  const request = questionSelectionMutationRequestSchema.parse({
    ...value,
    question_id: questionId,
  });
  return requestJson(
    `/operator/rounds/${checkedUuid(roundId)}/question`,
    operatorMutationResponseSchema,
    { method: "POST", body: request, headers: csrfHeaders() },
  );
}

export function advanceRound(
  roundId: string,
  questionId: string,
  value: OperatorMutationRequest,
): Promise<OperatorMutationResponse> {
  const request = advanceRoundMutationRequestSchema.parse({
    ...value,
    question_id: questionId,
  });
  return requestJson(
    `/operator/rounds/${checkedUuid(roundId)}/advance`,
    operatorMutationResponseSchema,
    { method: "POST", body: request, headers: csrfHeaders() },
  );
}

export function completeEvent(
  roundId: string,
  value: OperatorMutationRequest,
): Promise<OperatorMutationResponse> {
  const request = operatorMutationRequestSchema.parse(value);
  return requestJson(
    `/operator/rounds/${checkedUuid(roundId)}/complete-event`,
    operatorMutationResponseSchema,
    { method: "POST", body: request, headers: csrfHeaders() },
  );
}

export function setResultVisibility(
  target: ResultVisibilityTarget,
  visible: boolean,
  value: OperatorMutationRequest,
): Promise<OperatorMutationResponse> {
  const request = resultVisibilityMutationRequestSchema.parse({
    ...value,
    target,
    visible,
  });
  return requestJson(
    "/operator/result-visibility",
    operatorMutationResponseSchema,
    { method: "POST", body: request, headers: csrfHeaders() },
  );
}
