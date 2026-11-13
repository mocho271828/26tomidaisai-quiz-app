import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { cpus, totalmem, platform } from "node:os";
import { appendFile } from "node:fs/promises";
import { z } from "zod";
import { BufferedEventWriter, type WriterHealth, type WriterOptions } from "./bufferedWriter.js";
import { browserProbeSchema, serverTimingSchema } from "./browserDiagnostics.js";
import type { BrowserRunConfig } from "./config.js";
import { MAX_MANUAL_ROUNDS, roundNumberSchema } from "./controlRounds.js";

export { startEventLoopSampling, startResourceSampling, startWorkerSampling } from "./resourceSampling.js";
const clockFields = {monotonic_ms: z.number().finite(), wall_time_ms: z.number().finite()};
const roundNumber = roundNumberSchema;
const roundState = z.enum(["PreparingAssets", "ReadyToSchedule", "Scheduled", "Open",
  "ReadyToReveal", "AwaitingValidation", "VoidCandidate", "Reveal", "Voided",
  "RoundBackupPending", "RoundBackupValidated", "RoundBackupFailed", "RoundBackupRetrying"]);
export const telemetrySchema = z.discriminatedUnion("kind", [
  z.object({kind: z.literal("clock_sample"), rtt_ms: z.number().finite(), offset_ms: z.number().finite(), ...clockFields}).strict(),
  z.object({kind: z.literal("clock_sync"), outcome: z.enum(["synchronized", "retry"]), retry_ms: z.number().finite(), ...clockFields}).strict(),
  z.object({kind: z.literal("ws"), audience: z.enum(["participant", "operator"]),
    phase: z.enum(["connecting", "open", "closed", "retry", "stable"]), retry_ms: z.number().finite().optional(),
    attempt: z.number().int().nonnegative().optional(), ...clockFields}).strict(),
  z.object({kind: z.literal("projection_received"), round_number: roundNumber, state: roundState,
    has_question: z.boolean(), ...clockFields}).strict(),
  z.object({kind: z.literal("participant_display"), round_number: z.number().int().min(0).max(MAX_MANUAL_ROUNDS),
    phase: z.enum(["completed", "answering", "result", "collecting", "scheduled", "waiting"]),
    synchronized: z.boolean(), has_question: z.boolean(),
    submission_status: z.enum(["submitting", "accepted", "unknown", "not-stored", "rejected"]).optional(),
    answer_dom_present: z.boolean().optional(), answer_dom_visible: z.boolean().optional(),
    answer_notice_visible: z.boolean().optional(),
    answer_deadline_monotonic_ms: z.number().finite().optional(),
    result_dom_present: z.boolean().optional(), ...clockFields}).strict(),
  z.object({kind: z.literal("answer"), phase: z.enum(["sending", "accepted", "unknown", "rejected"]),
    round_number: roundNumber, ...clockFields}).strict(),
  z.object({kind: z.literal("api_request"), operation: z.enum(["answer_post", "answer_status", "participant_state", "participant_result", "participant_join", "server_time"]),
    request_sequence: z.number().int().positive(), phase: z.enum(["started", "settled"]),
    elapsed_ms: z.number().finite().nonnegative(), status: z.number().int().min(100).max(599).optional(),
    outcome: z.enum(["validated", "http_error", "contract_error", "transport_error", "aborted", "timeout"]).optional(),
    cancellation_reason: z.enum(["superseded", "hidden", "unmount", "deadline", "run_shutdown", "result_received", "revision_changed", "auth_invalidated"]).optional(),
    headers_elapsed_ms: z.number().finite().nonnegative().optional(), body_elapsed_ms: z.number().finite().nonnegative().optional(),
    validation_ms: z.number().finite().nonnegative().optional(), response_end_elapsed_ms: z.number().finite().nonnegative().optional(),
    delivery_lag_ms: z.number().finite().nonnegative().optional(),
    server_request_id: z.string().length(32).regex(/^[a-f0-9]+$/u).optional(),
    answer_trace: z.string().length(64).regex(/^[a-f0-9]+$/u).optional(),
    api_code: z.enum(["state_conflict", "resource_not_found", "participant_authentication_required",
      "request_validation_failed", "domain_temporarily_unavailable", "database_temporarily_unavailable",
      "audience_service_unavailable", "audience_control_error", "domain_error", "other"]).optional(),
    server_timing: serverTimingSchema.optional(), ...clockFields}).strict(),
  z.object({kind: z.literal("answer_apply"), round_number: roundNumber, source: z.enum(["post", "status", "projection"]),
    outcome: z.enum(["accepted", "not_stored", "unknown", "rejected", "stale", "identity_mismatch"]), ...clockFields}).strict(),
  z.object({kind: z.literal("answer_dom"), round_number: roundNumber,
    status: z.enum(["submitting", "accepted", "unknown", "not-stored", "rejected"]), visible: z.boolean(), ...clockFields}).strict(),
  z.object({kind: z.literal("answer_timeout"), round_number: roundNumber,
    timeout_ms: z.number().finite().nonnegative(), timer_lag_ms: z.number().finite().nonnegative(), ...clockFields}).strict(),
]);
export type Telemetry = z.infer<typeof telemetrySchema>;
export type SafeEvent = {
  event: string; serial?: number; round?: number; durationMs?: number;
  phase?: "setup" | "measurement" | "shutdown";
  lifecycleVersion?: 2;
  reason?: string;
  requestSequence?: number;
  participantKey?: string;
  identityKeyFingerprint?: string;
  cohort?: { count: number; ordinalStart: number };
  count?: number; status?: number; route?: string; telemetry?: Telemetry;
  timing?: { dnsMs: number; connectMs: number; tlsMs: number; firstByteMs: number };
  callbackEpochDeltaUncalibratedMs?: number;
  callbackEpochDeltaReason?: "node_browser_epoch_not_calibrated" | "playwright_finish_relative_origin_mismatch";
  deliveryLagMs?: number; browserProbe?: z.infer<typeof browserProbeSchema>;
  stage?: string; serverTiming?: Record<string, number>;
  networkDurationMs?: number;
  outcome?: "success" | "http_error" | "transport_error";
  mode?: "now" | "when-open"; delayMs?: number; spreadMs?: number; targetOrdinalStart?: number;
  commandId?: string; plannedAtMs?: number; actualAtMs?: number; operationLagMs?: number;
  browserMonotonicMs?: number; browserPlannedMonotonicMs?: number; browserOperationLagMs?: number;
  scope?: "host" | "worker" | "supervisor";
};

export type EvidenceOptions = WriterOptions & {measurementLevel?: "minimal" | "normal"};
export type EvidenceHealth = WriterHealth & {
  serializationMs: number; measurementLevel: "minimal" | "normal";
  lastWrittenSequence: number | null;
  lossClock: "process_monotonic_ms";
};

export class Evidence {
  readonly directory: string;
  readonly started = performance.now();
  readonly wallStarted = Date.now();
  private sequence = 0;
  private serializationMs = 0;
  private readonly writer: BufferedEventWriter;
  private readonly measurementLevel: "minimal" | "normal";
  private closePromise: Promise<void> | undefined;
  private readonly counts = new Map<string, number>();
  constructor(config: BrowserRunConfig, role: string, options: EvidenceOptions = {}) {
    this.directory = resolve(config.outputDirectory, `${config.runId}-${role}-${config.ordinalStart}`);
    mkdirSync(this.directory, { mode: 0o700, recursive: false });
    const eventsPath = resolve(this.directory, "events.jsonl");
    // 準備時だけ排他的に空fileを作り、計測中のevent書込みはbatch化する。
    writeFileSync(eventsPath, "", {mode: 0o600, flag: "wx"});
    const {measurementLevel = "normal", ...writerOptions} = options;
    this.measurementLevel = measurementLevel;
    this.writer = new BufferedEventWriter(batch => appendFile(eventsPath, batch), writerOptions);
    this.save("conditions.json", { ...config, role, startedAt: new Date().toISOString(),
      platform: platform(), cpuCount: cpus().length, memoryBytes: totalmem(),
      recorder: {...this.writer.options, measurementLevel, version: 1},
      tracePolicy: "allowlisted timing, random request correlation, answer HMAC and fixed API codes; no HAR, DOM, screenshots, cookies, raw identifiers or payload" });
  }
  event(record: SafeEvent): void {
    this.counts.set(record.event, (this.counts.get(record.event) ?? 0) + 1);
    const before = performance.now();
    this.writer.enqueue(JSON.stringify({
      sequence: this.sequence++, monotonicMs: before - this.started,
      wallMs: this.wallStarted + before - this.started, ...record,
    }) + "\n");
    this.serializationMs += performance.now() - before;
  }
  save(name: string, value: unknown): void {
    writeFileSync(resolve(this.directory, name), JSON.stringify(value, null, 2) + "\n",
      { mode: 0o600, flag: "wx" });
  }
  overhead(): number { return this.writer.health().writeMs; }
  health(): EvidenceHealth {
    const health = this.writer.health();
    return {...health, serializationMs: this.serializationMs, measurementLevel: this.measurementLevel,
      lastWrittenSequence: health.droppedRecords === 0 && health.writtenRecords > 0 ? health.writtenRecords - 1 : null,
      lossClock: "process_monotonic_ms"};
  }
  flush(): Promise<void> { return this.writer.flush(); }
  close(): Promise<void> {
    this.closePromise ??= this.closeAndSave();
    return this.closePromise;
  }
  private async closeAndSave(): Promise<void> {
    try { await this.writer.close(); }
    finally { this.save("evidence-health.json", this.health()); }
  }
  count(event: string): number { return this.counts.get(event) ?? 0; }
  wallNow(): number { return this.wallStarted + performance.now() - this.started; }
}
