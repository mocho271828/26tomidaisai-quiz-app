import type { Browser } from "playwright";
import type { BrowserRunConfig } from "./config.js";
import type { Evidence, Telemetry } from "./evidence.js";
import { fixedFailureReason, type RunLifecycle } from "./lifecycle.js";
import { createActor, type Actor } from "./participant.js";
import { recordParticipantIdentity } from "./participantIdentity.js";
import { PlaywrightManualDriver, type ManualBrowserDriver } from "./manualBrowser.js";
import { manualCommandSchema, spreadOffset, type ManualAction, type ManualParticipantStatus } from "./manualProtocol.js";
import { expectedRoundsSchema, MAX_MANUAL_COMMANDS } from "./controlRounds.js";

export type ManualParticipantOptions = {
  operationTimeoutMs: number; deadlineAtMs: number | null; detailedDiagnostics?: boolean; lifecycle?: RunLifecycle;
  expectedRounds?: number;
};
type PendingAction = {command: ManualAction; plannedAtMs?: number; timer?: ReturnType<typeof setTimeout>; running: boolean};
const CLOSED_STATES = new Set(["ReadyToReveal", "AwaitingValidation", "VoidCandidate", "Reveal",
  "Voided", "RoundBackupPending", "RoundBackupValidated", "RoundBackupFailed", "RoundBackupRetrying"]);

export async function createManualParticipant(browser: Browser, serial: number, config: BrowserRunConfig,
  evidence: Evidence, options: ManualParticipantOptions): Promise<ManualParticipant> {
  const actor = await createActor(browser, serial, config, evidence, options.lifecycle, {
    detailedDiagnostics: options.detailedDiagnostics ?? false,
    onTelemetry: (_actor, telemetry) => participant.observe(telemetry),
  });
  // 高負荷時のID表示取得など、操作期限を明示しないDOM待機にも猶予を持たせる。
  actor.page.setDefaultTimeout(90000);
  const participant = new ManualParticipant(actor, config, evidence, options);
  await participant.initialize();
  return participant;
}

export class ManualParticipant {
  private ready = false;
  private paused = false;
  private stopped = false;
  private currentRound = 0;
  private readonly closed = new Set<number>();
  private readonly commands = new Set<string>();
  private readonly reserved = new Set<string>();
  private readonly pending = new Map<string, PendingAction>();
  private readonly clicked = new Set<number>();
  private readonly answeredResults = new Set<number>();
  private readonly unansweredResults = new Set<number>();
  private readonly resultObservations = new Set<number>();
  private readonly sending = new Map<number, number>();
  private readonly receipts = new Map<number, ReturnType<typeof setTimeout>>();
  private readonly running = new Set<Promise<void>>();
  private cancelPreparation?: () => void;
  private timeouts = 0;
  private readonly driver: ManualBrowserDriver;
  private readonly expectedRounds: number;

  constructor(readonly actor: Actor, private readonly config: BrowserRunConfig, private readonly evidence: Evidence,
    private readonly options: ManualParticipantOptions, driver?: ManualBrowserDriver) {
    if ((config.durationSeconds === null) !== (options.deadlineAtMs === null) ||
      (options.deadlineAtMs !== null && !Number.isFinite(options.deadlineAtMs)) || !Number.isInteger(options.operationTimeoutMs) ||
      options.operationTimeoutMs < 1 || options.operationTimeoutMs > 60000) throw new Error("manual_timeout_invalid");
    this.driver = driver ?? new PlaywrightManualDriver(actor.page, options.operationTimeoutMs);
    this.expectedRounds = expectedRoundsSchema.parse(options.expectedRounds ?? 6);
  }

  async initialize(): Promise<void> { await this.driver.initialize(); }

  async prepare(): Promise<void> {
    let stage = "navigation";
    const cancelled = new Promise<void>(resolve => { this.cancelPreparation = resolve; });
    try {
      if (this.isStopped()) return;
      await Promise.race([this.actor.page.goto(this.config.baseUrl,
        {waitUntil: "domcontentloaded", timeout: this.timeout()}), cancelled]);
      if (this.isStopped()) return;
      stage = "join_button";
      await Promise.race([this.actor.page.getByRole("button", {name: "参加する", exact: true})
        .waitFor({timeout: this.timeout()}), cancelled]);
      if (this.isStopped()) return;
      stage = "fonts";
      // SPAのfont取得はload後も続くため、次の独立Context起動前に有限期限で待つ。
      await Promise.race([this.actor.page.waitForFunction(() => document.fonts.status === "loaded",
        undefined, {timeout: this.timeout()}), cancelled]);
      if (this.isStopped()) return;
      this.ready = true;
      this.evidence.event({event: "manual_prepared", serial: this.actor.serial});
    } catch (error: unknown) { if (!this.isStopped()) this.fail(0, stage, error); }
    finally { this.cancelPreparation = undefined; }
  }

  status(): ManualParticipantStatus {
    return {serial: this.actor.serial, ready: this.ready, joined: this.actor.joined,
      connected: this.actor.connected, paused: this.paused, stopped: this.stopped,
      complete: this.actor.complete, failed: this.actor.failed, currentRound: this.currentRound,
      clicked: [...this.clicked], accepted: [...this.actor.rounds], results: [...this.actor.results],
      answeredResults: [...this.answeredResults], unansweredResults: [...this.unansweredResults],
      failures: this.actor.failures.length, timeouts: this.timeouts,
      disconnects: this.actor.wsCloses, reconnects: Math.max(0, this.actor.wsOpens - 1), pending: this.pending.size};
  }

  async dispatch(input: unknown): Promise<void> {
    const parsed = manualCommandSchema.safeParse(input);
    if (!parsed.success) { this.reject(undefined, "invalid_command"); return; }
    const command = parsed.data;
    if (command.runId !== this.config.runId) { this.reject(command.commandId, "run_mismatch"); return; }
    if (command.kind === "answer" && command.round > this.expectedRounds) {
      this.reject(command.commandId, "round_outside_plan"); return;
    }
    const {ordinalStart, count} = command.target;
    if (this.actor.serial < ordinalStart || this.actor.serial >= ordinalStart + count) return;
    if (this.commands.has(command.commandId)) { this.reject(command.commandId, "duplicate_command"); return; }
    if (this.commands.size >= MAX_MANUAL_COMMANDS) { this.reject(command.commandId, "command_capacity"); return; }
    this.commands.add(command.commandId);
    if (this.isStopped() && command.kind !== "stop") { this.reject(command.commandId, "run_stopped"); return; }
    const accept = (): void => this.evidence.event({event: "manual_command_accepted", serial: this.actor.serial,
      commandId: command.commandId, round: command.kind === "answer" ? command.round : undefined, stage: command.kind});
    if (command.kind === "stop") { accept(); await this.stop(); return; }
    if (command.kind === "pause" || command.kind === "resume") {
      accept();
      this.paused = command.kind === "pause";
      try { await this.driver.pause(this.paused); }
      catch (error: unknown) { this.fail(0, command.kind, error, command.commandId); throw error; }
      for (const operation of this.pending.values()) this.schedule(operation);
      return;
    }
    const key = command.kind === "join" ? "join" : `answer:${command.round}`;
    if (this.reserved.has(key)) { this.reject(command.commandId, "duplicate_operation"); return; }
    if (command.kind === "join" && (!this.ready || this.actor.joined)) {
      this.reject(command.commandId, "participant_not_ready"); return;
    }
    if (command.kind === "answer" && this.expired(command.round)) {
      this.reject(command.commandId, "round_expired"); return;
    }
    if (command.kind === "answer" && command.mode === "now" && !this.actor.joined) {
      this.reject(command.commandId, "round_not_answerable"); return;
    }
    this.reserved.add(key);
    accept();
    const operation: PendingAction = {command, running: false};
    if (command.kind === "join" || command.mode === "now")
      this.plan(operation, command.issuedAtMs + spreadOffset(command, this.actor.serial) +
        (command.kind === "answer" ? command.delayMs : 0));
    this.pending.set(key, operation);
    this.schedule(operation);
  }

  observe(telemetry: Telemetry): void {
    if (this.stopped) return;
    if (telemetry.kind === "projection_received") {
      this.currentRound = Math.max(this.currentRound, telemetry.round_number);
      if (CLOSED_STATES.has(telemetry.state)) this.closed.add(telemetry.round_number);
    }
    if (telemetry.kind === "participant_display") {
      this.currentRound = Math.max(this.currentRound, telemetry.round_number);
      if (telemetry.phase === "collecting" || telemetry.phase === "result" || telemetry.phase === "completed")
        this.closed.add(telemetry.round_number);
      if (telemetry.answer_dom_visible || telemetry.answer_notice_visible) this.accept(telemetry.round_number, telemetry.monotonic_ms);
      if (telemetry.result_dom_present && !this.actor.results.includes(telemetry.round_number) &&
        !this.resultObservations.has(telemetry.round_number)) {
        this.resultObservations.add(telemetry.round_number);
        this.track(this.observeResult(telemetry.round_number));
      }
    }
    if (telemetry.kind === "answer" && telemetry.phase === "sending" && !this.sending.has(telemetry.round_number))
      this.sending.set(telemetry.round_number, telemetry.monotonic_ms);
    if (telemetry.kind === "answer_dom" && telemetry.status === "accepted" && telemetry.visible)
      this.accept(telemetry.round_number, telemetry.monotonic_ms);
    for (const operation of this.pending.values()) this.schedule(operation);
  }

  async stop(): Promise<void> {
    if (this.stopped) return;
    if (this.running.size > 0 || this.receipts.size > 0)
      this.evidence.event({event: "manual_observation_interrupted", serial: this.actor.serial,
        count: this.running.size + this.receipts.size, reason: "operation_or_receipt_in_flight"});
    this.stopped = true; this.actor.stopped = true;
    this.cancelPreparation?.();
    for (const operation of this.pending.values()) {
      clearTimeout(operation.timer);
      if (!operation.running) this.skip(operation, "run_stopped");
    }
    for (const timer of this.receipts.values()) clearTimeout(timer);
    this.receipts.clear();
    try { if (!this.actor.page.isClosed()) await this.driver.pause(true, true); }
    catch { this.evidence.event({event: "manual_stop_guard_failed", serial: this.actor.serial}); }
    await Promise.allSettled([...this.running]);
    this.pending.clear();
  }

  private timeout(): number {
    if (this.options.deadlineAtMs === null) return this.options.operationTimeoutMs;
    const remaining = this.options.deadlineAtMs - Date.now();
    if (remaining <= 0) throw new Error("run_deadline");
    return Math.max(1, Math.min(this.options.operationTimeoutMs, remaining));
  }

  private isStopped(): boolean {
    return this.stopped || (this.options.deadlineAtMs !== null && Date.now() >= this.options.deadlineAtMs);
  }
  private expired(round: number): boolean { return this.currentRound > round || this.closed.has(round); }
  private key(operation: PendingAction): string {
    return operation.command.kind === "join" ? "join" : `answer:${operation.command.round}`;
  }

  private plan(operation: PendingAction, plannedAtMs: number): void {
    operation.plannedAtMs = plannedAtMs;
    this.evidence.event({event: "manual_operation_planned", serial: this.actor.serial,
      commandId: operation.command.commandId, plannedAtMs, stage: operation.command.kind,
      round: operation.command.kind === "answer" ? operation.command.round : undefined});
  }

  private schedule(operation: PendingAction): void {
    if (operation.running) return;
    clearTimeout(operation.timer);
    const {command} = operation;
    if (this.isStopped()) { this.skip(operation, "run_stopped"); return; }
    if (command.kind === "answer" && this.expired(command.round)) { this.skip(operation, "round_expired"); return; }
    if (command.kind === "answer") {
      if (!this.actor.joined) return;
      // 開始通知のNode到着を待つと回答期限を消費するため、予約と実行はbrowserに任せる。
      operation.running = true;
      this.track(this.execute(operation));
      return;
    }
    if (this.paused || operation.plannedAtMs === undefined) return;
    operation.timer = setTimeout(() => {
      if (this.paused || this.isStopped()) return;
      operation.running = true;
      this.track(this.execute(operation));
    }, Math.max(0, operation.plannedAtMs - Date.now()));
  }

  private async execute(operation: PendingAction): Promise<void> {
    const {command, plannedAtMs} = operation;
    const round = command.kind === "answer" ? command.round : 0;
    let deferred = false;
    try {
      const click = command.kind === "answer" ?
        await this.driver.answer(command, this.actor.serial, this.options.deadlineAtMs) :
        await this.driver.click(command, this.actor.serial, this.timeout());
      if (!click.clicked) {
        // nowの未開始判定はbrowserで確定する。この場合だけ、開始後の別命令を受け付ける。
        if (command.kind === "answer" && command.mode === "now" && click.reason === "round_not_answerable")
          this.reserved.delete(this.key(operation));
        if (command.kind === "join" && this.paused && !this.isStopped()) { deferred = true; operation.running = false; }
        else this.skip(operation, click.reason ?? "click_guard_blocked");
        return;
      }
      const actualAtMs = Date.now();
      this.evidence.event({event: command.kind === "answer" ? "manual_answer_clicked" : "manual_join_clicked",
        serial: this.actor.serial, commandId: command.commandId, round, plannedAtMs, actualAtMs,
        operationLagMs: plannedAtMs === undefined ? undefined : actualAtMs - plannedAtMs,
        browserMonotonicMs: click.browserMonotonicMs, browserPlannedMonotonicMs: click.browserPlannedMonotonicMs,
        browserOperationLagMs: click.browserMonotonicMs !== undefined && click.browserPlannedMonotonicMs !== undefined ?
          click.browserMonotonicMs - click.browserPlannedMonotonicMs : undefined,
        reason: command.kind === "answer" ? "browser_dom_click" : "node_observed_click_completion"});
      if (command.kind === "answer") {
        this.clicked.add(round);
        this.updateComplete();
        if (!this.actor.rounds.includes(round) && !this.stopped) this.receipts.set(round, setTimeout(() => {
          this.receipts.delete(round);
          if (!this.stopped && !this.actor.rounds.includes(round)) this.fail(round, "accepted_dom", new Error("receipt_timeout"));
        }, this.timeout()));
      } else {
        await this.actor.page.locator(".participant-live-shell").waitFor({timeout: this.timeout()});
        if (this.isStopped()) return;
        this.actor.joined = true;
        this.evidence.event({event: "joined", serial: this.actor.serial, commandId: command.commandId});
        const cookies = await this.actor.context.cookies();
        if (!cookies.some(cookie => cookie.httpOnly && cookie.secure)) throw new Error("secure_session_missing");
        await recordParticipantIdentity(this.actor.page, this.actor.serial, this.config, this.evidence);
      }
    } catch (error: unknown) { if (!this.isStopped()) this.fail(round, command.kind, error, command.commandId); }
    finally {
      if (!deferred) this.pending.delete(this.key(operation));
      for (const pending of this.pending.values()) this.schedule(pending);
    }
  }

  private accept(round: number, monotonicMs: number): void {
    if (round < 1 || this.actor.rounds.includes(round)) return;
    this.actor.rounds.push(round);
    clearTimeout(this.receipts.get(round)); this.receipts.delete(round);
    this.evidence.event({event: "accepted_dom", serial: this.actor.serial, round, browserMonotonicMs: monotonicMs});
    const started = this.sending.get(round);
    if (started !== undefined && monotonicMs >= started)
      this.evidence.event({event: "answer_send_to_accepted_dom", serial: this.actor.serial, round, durationMs: monotonicMs - started});
    else this.evidence.event({event: "answer_latency_sample_missing", serial: this.actor.serial, round});
    this.updateComplete();
  }

  private async observeResult(round: number): Promise<void> {
    try {
      const result = await this.driver.observeResult(round, this.timeout());
      if (!result) {
        this.evidence.event({event: "result_dom_not_visible", serial: this.actor.serial, round});
        return;
      }
      if (this.actor.results.includes(round)) return;
      this.actor.results.push(round);
      (result.unanswered ? this.unansweredResults : this.answeredResults).add(round);
      this.evidence.event({event: "result_dom", serial: this.actor.serial, round,
        reason: result.unanswered ? "unanswered" : "answered"});
      this.updateComplete();
    } catch (error: unknown) { if (!this.stopped) this.fail(round, "result_observation", error); }
    finally { this.resultObservations.delete(round); }
  }

  private updateComplete(): void {
    const wasComplete = this.actor.complete;
    this.actor.complete = !this.actor.failed && Array.from({length: this.expectedRounds}, (_, index) => index + 1)
      .every(round => this.clicked.has(round) && this.actor.rounds.includes(round) && this.answeredResults.has(round));
    if (this.actor.complete && !wasComplete) this.evidence.event({event: "planned_rounds_complete", serial: this.actor.serial});
  }
  private track(promise: Promise<void>): void {
    this.running.add(promise);
    void promise.finally(() => this.running.delete(promise));
  }
  private skip(operation: PendingAction, reason: string): void {
    clearTimeout(operation.timer);
    this.pending.delete(this.key(operation));
    this.evidence.event({event: "manual_operation_skipped", serial: this.actor.serial,
      commandId: operation.command.commandId, round: operation.command.kind === "answer" ? operation.command.round : undefined, reason});
  }
  private reject(commandId: string | undefined, reason: string): void {
    this.evidence.event({event: "manual_command_rejected", serial: this.actor.serial, commandId, reason});
  }
  private fail(round: number, stage: string, error: unknown, commandId?: string): void {
    const reason = error instanceof Error && error.message === "receipt_timeout" ? "timeout" : fixedFailureReason(error);
    if (reason === "timeout") this.timeouts++;
    this.actor.failed = true;
    this.actor.failures.push({round, stage});
    this.evidence.event({event: "manual_operation_failed", serial: this.actor.serial, round, stage, commandId, reason});
  }
}
