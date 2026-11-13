import { useCallback, useEffect, useRef } from "react";

import { getAnswerStatus } from "../../api/audienceClient";
import { ApiCancellation, type CancellationReason } from "../../api/cancellation";
import { ApiResponseError, ApiTransportError, errorMessage } from "../../api/client";
import { observeBrowser } from "../_utils/browserObservation";
import { clearPendingSubmission } from "./pendingSubmission";
import type { ParticipantSubmission } from "./participantUi";

const RECOVERY_LIMIT_MS = 10_000;
const RECOVERY_ATTEMPTS = 6;
const RETRY_DELAYS_MS = [250, 500, 1_000, 2_000, 2_000] as const;

type RecoveryContext = {
  generation: number;
  roundId: string | null;
  roundNumber: number;
  submission: ParticipantSubmission | null;
};

type RecoveryFlight = {
  generation: number;
  target: ParticipantSubmission;
  controller: AbortController;
  promise: Promise<RecoveryOutcome>;
};

type RecoveryOutcome = "accepted" | "unauthorized" | "unresolved";

function hasSameIdentity(left: ParticipantSubmission, right: ParticipantSubmission): boolean {
  return left.clientAnswerId === right.clientAnswerId && left.roundId === right.roundId &&
    left.optionId === right.optionId && left.roundVersion === right.roundVersion && left.clientElapsedMs === right.clientElapsedMs;
}

function waitForRetry(delayMs: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const finish = (): void => {
      window.clearTimeout(timer);
      signal.removeEventListener("abort", finish);
      resolve();
    };
    const timer = window.setTimeout(finish, delayMs);
    signal.addEventListener("abort", finish, { once: true });
    if (signal.aborted) finish();
  });
}

// NotStoredは照会時点の未commitを含む。自動POSTをせず、同一回答の照会だけを有限に続ける。
export function useParticipantSubmissionRecovery(
  getContext: () => RecoveryContext,
  updateSubmission: (next: ParticipantSubmission) => void,
): {
  verifySubmission: (target: ParticipantSubmission) => Promise<RecoveryOutcome>;
  cancelRecovery: (reason?: CancellationReason) => void;
} {
  const activeRef = useRef(true);
  const flightRef = useRef<RecoveryFlight | null>(null);
  const cancelRecovery = useCallback((reason?: CancellationReason): void => {
    const flight = flightRef.current;
    flightRef.current = null;
    // 呼出し側が理由を特定できない取消は、正常な差替えと推定しない。
    flight?.controller.abort(reason === undefined
      ? new DOMException("API request cancelled", "AbortError") : new ApiCancellation(reason));
  }, []);

  useEffect(() => {
    activeRef.current = true;
    return () => {
      activeRef.current = false;
      cancelRecovery("unmount");
    };
  }, [cancelRecovery]);

  const verifySubmission = useCallback((target: ParticipantSubmission): Promise<RecoveryOutcome> => {
    const context = getContext();
    const canRecover = (current: RecoveryContext): boolean => activeRef.current &&
      current.generation === context.generation && current.roundId === target.roundId &&
      current.submission !== null && hasSameIdentity(current.submission, target) &&
      (current.submission.status === "unknown" || current.submission.status === "not-stored");
    if (!canRecover(context)) return Promise.resolve("unresolved");
    const existing = flightRef.current;
    if (existing !== null && !existing.controller.signal.aborted &&
      existing.generation === context.generation && hasSameIdentity(existing.target, target)) {
      return existing.promise;
    }
    cancelRecovery("superseded");
    const controller = new AbortController();
    const deadline = performance.now() + RECOVERY_LIMIT_MS;
    const deadlineTimer = window.setTimeout(() => controller.abort(new ApiCancellation("deadline")), RECOVERY_LIMIT_MS);
    const isCurrent = (): boolean => !controller.signal.aborted && performance.now() < deadline &&
      canRecover(getContext());
    // 即時応答でもfinallyがflightRefへの代入を追い越さないよう、次のmicrotaskで開始する。
    const promise = Promise.resolve().then(async (): Promise<RecoveryOutcome> => {
      try {
        for (let attempt = 0; attempt < RECOVERY_ATTEMPTS; attempt += 1) {
          if (!isCurrent()) return "unresolved";
          try {
            const status = await getAnswerStatus(target.clientAnswerId, controller.signal);
            if (!isCurrent()) return "unresolved";
            const current = getContext().submission;
            if (current === null) return "unresolved";
            const hasIdentityMismatch = status.status === "AcceptedLocked"
              ? status.answer.round_id !== target.roundId || status.answer.option_id !== target.optionId ||
                status.answer.client_answer_id !== target.clientAnswerId || status.answer.client_elapsed_ms !== target.clientElapsedMs
              : status.client_answer_id !== target.clientAnswerId;
            if (hasIdentityMismatch) {
              observeBrowser({ kind: "answer_apply", source: "status", outcome: "identity_mismatch", round_number: context.roundNumber });
              updateSubmission({ ...current, status: "unknown", message: "保存状態の識別子が一致しません。運営へ確認してください。" });
              return "unresolved";
            }
            if (status.status === "AcceptedLocked") {
              clearPendingSubmission();
              observeBrowser({ kind: "answer_apply", source: "status", outcome: "accepted", round_number: context.roundNumber });
              updateSubmission({ ...current, status: "accepted", receivedAt: status.answer.received_at,
                clientElapsedMs: status.answer.client_elapsed_ms, timingBasis: status.answer.timing_basis,
                message: "回答は保存済みです。変更はできません。" });
              return "accepted";
            }
            updateSubmission({ ...current, status: "not-stored", message: "回答の保存をまだ確認できません。保存状態の確認を続けています。" });
            observeBrowser({ kind: "answer_apply", source: "status", outcome: "not_stored", round_number: context.roundNumber });
          } catch (error: unknown) {
            if (!isCurrent()) return "unresolved";
            const current = getContext().submission;
            if (current !== null) {
              observeBrowser({ kind: "answer_apply", source: "status", outcome: "unknown", round_number: context.roundNumber });
              updateSubmission({ ...current, status: "unknown", message: errorMessage(error) });
            }
            if (error instanceof ApiResponseError && error.status === 401) return "unauthorized";
            if (!(error instanceof ApiTransportError || (error instanceof ApiResponseError && error.status >= 500))) {
              return "unresolved";
            }
          }
          if (attempt + 1 < RECOVERY_ATTEMPTS && isCurrent()) {
            const delay = RETRY_DELAYS_MS[attempt]! * (1 + Math.random() * 0.2);
            await waitForRetry(Math.min(delay, deadline - performance.now()), controller.signal);
          }
        }
        return "unresolved";
      } finally {
        window.clearTimeout(deadlineTimer);
        if (flightRef.current?.controller === controller) flightRef.current = null;
      }
    });
    flightRef.current = { generation: context.generation, target, controller, promise };
    return promise;
  }, [cancelRecovery, getContext, updateSubmission]);

  return { verifySubmission, cancelRecovery };
}
