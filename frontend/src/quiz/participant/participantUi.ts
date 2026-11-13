import type { ParticipantAnswer } from "../../api/contracts";

export function formatEventStartCountdown(
  eventStartAt: string | null,
  now: number,
): string | null {
  if (eventStartAt === null) {
    return null;
  }
  const startAt = Date.parse(eventStartAt);
  if (!Number.isFinite(startAt) || !Number.isFinite(now)) {
    return null;
  }
  const remainingMilliseconds = startAt - now;
  if (remainingMilliseconds <= 0) {
    return "まもなく開始";
  }
  const remainingMinutes = Math.max(
    1,
    Math.ceil(remainingMilliseconds / 60_000),
  );
  return `開始まで残り${remainingMinutes}分`;
}

export function createParticipantJoinToken(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  const binary = String.fromCharCode(...bytes);
  return btoa(binary)
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/u, "");
}

export type ParticipantSubmissionStatus =
  | "submitting"
  | "unknown"
  | "not-stored"
  | "accepted"
  | "rejected";

export type ParticipantSubmission = {
  clientAnswerId: string;
  optionId: string;
  roundId: string;
  roundVersion: number;
  clientElapsedMs: number | null;
  timingBasis: ParticipantAnswer["timing_basis"];
  receivedAt: string | null;
  status: ParticipantSubmissionStatus;
  message: string;
};

export type ParticipantInFlightRequest = {
  clientAnswerId: string;
  generation: number;
};

type SubmissionIdentity = {
  client_answer_id: string;
  option_id: string;
  round_id: string;
  round_version: number;
  client_elapsed_ms: number | null;
  timing_basis?: ParticipantAnswer["timing_basis"];
};

export function createParticipantSubmission(
  identity: SubmissionIdentity,
  status: ParticipantSubmissionStatus,
  message: string,
): ParticipantSubmission {
  return {
    clientAnswerId: identity.client_answer_id,
    optionId: identity.option_id,
    roundId: identity.round_id,
    roundVersion: identity.round_version,
    clientElapsedMs: identity.client_elapsed_ms,
    timingBasis: identity.timing_basis ?? "client_monotonic_v1",
    receivedAt: null,
    status,
    message,
  };
}

export function createAcceptedParticipantSubmission(
  answer: ParticipantAnswer,
  roundVersion: number,
): ParticipantSubmission {
  const submission = createParticipantSubmission(
    {
      client_answer_id: answer.client_answer_id,
      option_id: answer.option_id,
      round_id: answer.round_id,
      round_version: roundVersion,
      client_elapsed_ms: answer.client_elapsed_ms,
      timing_basis: answer.timing_basis,
    },
    "accepted",
    "回答は保存済みです。変更はできません。",
  );
  return {
    ...submission,
    receivedAt: answer.received_at,
  };
}

export function isSameSubmission(
  submission: ParticipantSubmission,
  identity: SubmissionIdentity,
): boolean {
  return (
    submission.clientAnswerId === identity.client_answer_id &&
    submission.optionId === identity.option_id &&
    submission.roundId === identity.round_id &&
    submission.roundVersion === identity.round_version &&
    submission.clientElapsedMs === identity.client_elapsed_ms
  );
}
