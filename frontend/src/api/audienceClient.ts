import { z } from "zod";

import {
  answerReceiptSchema,
  answerStatusSchema,
  participantJoinRequestSchema,
  participantStateSchema,
  personalResultResponseSchema,
  submitAnswerRequestSchema,
  timeResponseSchema,
  type AnswerReceipt,
  type AnswerStatus,
  type ParticipantState,
  type PersonalResultResponse,
  type SubmitAnswerRequest,
  type TimeResponse,
} from "./contracts";
import { requestJson } from "./client";
import { ApiCancellation, forwardedCancellation } from "./cancellation";

const uuidSchema = z.uuid();
const PARTICIPANT_GET_TIMEOUT_MS = 5_000;

async function boundedParticipantGet<T>(request: (signal: AbortSignal) => Promise<T>, signal?: AbortSignal): Promise<T> {
  const controller = new AbortController();
  let rejectCancellation: (reason: DOMException) => void = () => undefined;
  const cancellation = new Promise<never>((_resolve, reject) => {
    rejectCancellation = reject;
  });
  const stop = (reason: DOMException): void => { controller.abort(reason); rejectCancellation(reason); };
  const cancel = (): void => stop(forwardedCancellation(signal));
  const timer = window.setTimeout(() => stop(new ApiCancellation("deadline")), PARTICIPANT_GET_TIMEOUT_MS);
  signal?.addEventListener("abort", cancel, { once: true });
  if (signal?.aborted) cancel();
  try { return await Promise.race([request(controller.signal), cancellation]); }
  finally {
    window.clearTimeout(timer);
    signal?.removeEventListener("abort", cancel);
  }
}

function checkedUuid(id: string): string {
  return uuidSchema.parse(id);
}

export function getParticipantSession(signal?: AbortSignal): Promise<ParticipantState> {
  return boundedParticipantGet((requestSignal) => requestJson("/participant/session", participantStateSchema,
    { signal: requestSignal, observationOperation: "participant_state" }), signal);
}

export function getParticipantResult(signal?: AbortSignal): Promise<PersonalResultResponse> {
  return boundedParticipantGet((requestSignal) => requestJson("/participant/result", personalResultResponseSchema,
    { signal: requestSignal, observationOperation: "participant_result" }), signal);
}

export function joinParticipant(
  clientJoinToken: string,
): Promise<ParticipantState> {
  const request = participantJoinRequestSchema.parse({
    client_join_token: clientJoinToken,
  });
  return requestJson(
    "/participant/join",
    participantStateSchema,
    { method: "POST", body: request, observationOperation: "participant_join" },
  );
}

export function submitAnswer(
  roundId: string,
  value: SubmitAnswerRequest,
  signal?: AbortSignal,
): Promise<AnswerReceipt> {
  const request = submitAnswerRequestSchema.parse(value);
  return requestJson(
    `/participant/rounds/${checkedUuid(roundId)}/answers`,
    answerReceiptSchema,
    { method: "POST", body: request, signal, observationOperation: "answer_post" },
  );
}

export function getAnswerStatus(clientAnswerId: string, signal?: AbortSignal): Promise<AnswerStatus> {
  return requestJson(
    `/participant/answers/${checkedUuid(clientAnswerId)}/status`,
    answerStatusSchema,
    { signal, observationOperation: "answer_status" },
  );
}

export function getServerTime(signal?: AbortSignal): Promise<TimeResponse> {
  return requestJson("/time", timeResponseSchema, { signal, observationOperation: "server_time" });
}
