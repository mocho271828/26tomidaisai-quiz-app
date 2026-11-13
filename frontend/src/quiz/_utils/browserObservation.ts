import type { RoundState } from "../../api/contracts";
import type { CancellationReason } from "../../api/cancellation";

export type ApiObservationOperation = "answer_post" | "answer_status" | "participant_state" | "participant_result" | "participant_join" | "server_time";
export type ApiObservationOutcome = "validated" | "http_error" | "contract_error" | "transport_error" | "aborted" | "timeout";
export const API_OBSERVATION_CODES = [
  "state_conflict", "resource_not_found", "participant_authentication_required", "request_validation_failed",
  "domain_temporarily_unavailable", "database_temporarily_unavailable", "audience_service_unavailable",
  "audience_control_error", "domain_error",
] as const;
export type SafeApiCode = typeof API_OBSERVATION_CODES[number] | "other";
export const SERVER_TIMING_METRICS = [
  "api_total", "pre_handler", "participant_metadata", "recovery_wait", "common_state_wait", "thread_wait", "dispatch_wait", "handler", "db_acquire", "db_lock_query",
  "answer_transaction", "state_projection", "status_projection", "redis_publish", "response_build",
] as const;
export type SafeServerTiming = Partial<Record<typeof SERVER_TIMING_METRICS[number], number>>;

type ObservationDetail =
  | { kind: "clock_sample"; rtt_ms: number; offset_ms: number }
  | { kind: "clock_sync"; outcome: "synchronized" | "retry"; retry_ms: number }
  | { kind: "ws"; audience: "participant" | "operator"; phase: "connecting" | "open" | "closed" | "retry" | "stable"; retry_ms?: number; attempt?: number }
  | { kind: "projection_received"; round_number: number; state: RoundState; has_question: boolean }
  | { kind: "participant_display"; round_number: number; phase: "completed" | "answering" | "result" | "collecting" | "scheduled" | "waiting"; synchronized: boolean; has_question: boolean; submission_status?: "submitting" | "accepted" | "unknown" | "not-stored" | "rejected"; answer_dom_present: boolean; answer_dom_visible: boolean; answer_notice_visible?: boolean; result_dom_present: boolean; answer_deadline_monotonic_ms?: number }
  | { kind: "answer"; phase: "sending" | "accepted" | "unknown" | "rejected"; round_number: number }
  | { kind: "answer_apply"; round_number: number; source: "post" | "status" | "projection"; outcome: "accepted" | "not_stored" | "unknown" | "rejected" | "stale" | "identity_mismatch" }
  | { kind: "answer_timeout"; round_number: number; timeout_ms: number; timer_lag_ms: number }
  | { kind: "api_request"; operation: ApiObservationOperation; request_sequence: number; phase: "started" | "settled"; elapsed_ms: number; outcome?: ApiObservationOutcome; cancellation_reason?: CancellationReason; status?: number; headers_elapsed_ms?: number; body_elapsed_ms?: number; validation_ms?: number; response_end_elapsed_ms?: number; delivery_lag_ms?: number; server_timing?: SafeServerTiming; server_request_id?: string; answer_trace?: string; api_code?: SafeApiCode };

export type BrowserObservation = ObservationDetail & {
  monotonic_ms: number;
  wall_time_ms: number;
};

// 生の識別子・payload・URL・例外本文は持たせず、API相関は検証済みの乱数/HMACと固定codeに限る。
export function observeBrowser(detail: ObservationDetail): void {
  window.dispatchEvent(new CustomEvent<BrowserObservation>("quiz:observation", {
    detail: { ...detail, monotonic_ms: performance.now(), wall_time_ms: Date.now() },
  }));
}
