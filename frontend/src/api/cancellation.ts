export const CANCELLATION_REASONS = [
  "superseded", "hidden", "unmount", "deadline", "result_received", "revision_changed", "auth_invalidated",
] as const;
export type CancellationReason = typeof CANCELLATION_REASONS[number];

// 自由文のAbortSignal.reasonを証跡へ渡さず、呼出し元の意図だけを固定分類する。
export class ApiCancellation extends DOMException {
  readonly cancellationReason: CancellationReason;

  constructor(reason: CancellationReason) {
    super("API request cancelled", reason === "deadline" ? "TimeoutError" : "AbortError");
    this.cancellationReason = reason;
  }
}

export function cancellationReason(signal?: AbortSignal | null): CancellationReason | undefined {
  if (signal?.aborted !== true) return undefined;
  if (signal.reason instanceof ApiCancellation) return signal.reason.cancellationReason;
  if (signal.reason instanceof DOMException && signal.reason.name === "TimeoutError") return "deadline";
  return undefined;
}

export function forwardedCancellation(signal: AbortSignal | undefined): DOMException {
  const reason = cancellationReason(signal);
  return reason === undefined ? new DOMException("API request cancelled", "AbortError") : new ApiCancellation(reason);
}
