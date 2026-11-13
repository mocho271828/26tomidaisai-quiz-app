import { setTimeout as delay } from "node:timers/promises";
import type { Evidence } from "./evidence.js";

export type RunPhase = "setup" | "measurement" | "shutdown";
export type StopReason = "completed" | "run_failed" | "run_deadline" | "signal" | "participant_unavailable";

export class RunLifecycle {
  readonly controller = new AbortController();
  phase: RunPhase = "setup";
  private finished = false;

  constructor(private readonly evidence: Evidence, private readonly role: "participants" | "operator") {}

  get signal(): AbortSignal { return this.controller.signal; }

  startMeasurement(): void {
    this.signal.throwIfAborted();
    this.phase = "measurement";
    this.evidence.event({event: this.role === "participants" ? "participant_measurement_started" : "operator_measurement_started",
      phase: this.phase, lifecycleVersion: 2});
  }

  finish(reason: StopReason): void {
    if (this.finished) return;
    this.finished = true;
    this.evidence.event({event: this.role === "participants" ? "participant_run_finished" : "operator_run_finished",
      phase: this.phase, lifecycleVersion: 2, reason});
    this.phase = "shutdown";
    this.controller.abort(reason);
  }

  shutdownFinished(reason?: StopReason): void {
    this.evidence.event({event: this.role === "participants" ? "participant_shutdown_finished" : "operator_shutdown_finished",
      phase: "shutdown", lifecycleVersion: 2, reason});
  }
}

export async function cancellableDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  signal.throwIfAborted();
  await delay(Math.max(0, milliseconds), undefined, {signal});
}

export function fixedFailureReason(error: unknown, signal?: AbortSignal): string {
  if (signal?.aborted) return "runner_cancelled";
  if (error instanceof Error && error.name === "TimeoutError") return "timeout";
  if (error instanceof Error && error.message === "round_superseded") return "round_superseded";
  if (error instanceof Error && error.message === "secure_session_missing") return "secure_session_missing";
  if (error instanceof Error && error.message === "unanswered_result") return "unanswered_result";
  return "unclassified_failure";
}
