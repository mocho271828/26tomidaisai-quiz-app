import { submitAnswer } from "../../api/audienceClient";
import { ApiCancellation } from "../../api/cancellation";
import { ApiTransportError } from "../../api/client";
import type { AnswerReceipt } from "../../api/contracts";
import { observeBrowser } from "../_utils/browserObservation";
import type { PendingSubmission } from "./pendingSubmission";

// HTTP中断はDB側の取消を保証しない。同じID・時間だけで1回再送し、その後はGET照会へ渡す。
export async function submitTimedAnswer(
  pending: PendingSubmission,
  roundNumber: number,
  canRetry: () => boolean,
): Promise<AnswerReceipt> {
  for (let attempt = 0; ; attempt += 1) {
    const controller = new AbortController();
    const timeoutMs = 1_000;
    const timeoutAt = performance.now() + timeoutMs;
    let rejectDeadline: (reason: ApiCancellation) => void = () => undefined;
    const deadline = new Promise<never>((_resolve, reject) => { rejectDeadline = reject; });
    const timeout = window.setTimeout(() => {
      observeBrowser({ kind: "answer_timeout", round_number: roundNumber,
        timeout_ms: timeoutMs, timer_lag_ms: Math.max(0, performance.now() - timeoutAt) });
      const reason = new ApiCancellation("deadline");
      controller.abort(reason);
      rejectDeadline(reason);
    }, timeoutMs);
    try {
      return await Promise.race([submitAnswer(pending.round_id, {
        round_version: pending.round_version,
        option_id: pending.option_id,
        client_answer_id: pending.client_answer_id,
        client_elapsed_ms: pending.client_elapsed_ms,
      }, controller.signal), deadline]);
    } catch (error: unknown) {
      const isRetryable = error instanceof ApiTransportError ||
        (error instanceof ApiCancellation && error.cancellationReason === "deadline");
      if (attempt >= 1 || !isRetryable || !canRetry()) throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  }
}
