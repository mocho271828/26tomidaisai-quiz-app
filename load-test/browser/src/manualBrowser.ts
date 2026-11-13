import type { Page } from "playwright";
import { spreadOffset, type ManualAction } from "./manualProtocol.js";
import { installManualBrowserGuard, type GuardWindow, type ManualClick } from "./manualAnswer.js";
export type { ManualClick } from "./manualAnswer.js";
export interface ManualBrowserDriver {
  initialize(): Promise<void>;
  pause(paused: boolean, stopped?: boolean): Promise<void>;
  click(command: Extract<ManualAction, {kind: "join"}>, serial: number, timeout: number): Promise<ManualClick>;
  answer(command: Extract<ManualAction, {kind: "answer"}>, serial: number, deadlineAtMs: number | null): Promise<ManualClick>;
  observeResult(round: number, timeout: number): Promise<{unanswered: boolean} | null>;
}

export class PlaywrightManualDriver implements ManualBrowserDriver {
  private timedOut = false;
  private stopped = false;
  private readonly stopController = new AbortController();
  constructor(private readonly page: Page, private readonly operationTimeoutMs = 30000) {}

  async initialize(): Promise<void> {
    await this.bounded(() => this.page.addInitScript(installManualBrowserGuard), this.operationTimeoutMs);
  }

  async pause(paused: boolean, stopped = false): Promise<void> {
    if (stopped) { this.stopped = true; this.stopController.abort(); }
    else if (this.stopped) return;
    if (this.timedOut && stopped) return;
    await this.bounded(async () => this.page.evaluate(({paused, stopped}) => {
      const guard = (window as GuardWindow).quizManualGuard;
      guard?.setPaused(paused, stopped);
    }, {paused, stopped}), this.operationTimeoutMs);
  }

  async click(command: Extract<ManualAction, {kind: "join"}>, _serial: number, timeout: number): Promise<ManualClick> {
    if (this.stopped) return {clicked: false};
    return this.bounded(() => this.performClick(command, timeout), timeout);
  }

  async answer(command: Extract<ManualAction, {kind: "answer"}>, serial: number, deadlineAtMs: number | null): Promise<ManualClick> {
    if (this.stopped) return {clicked: false, reason: "run_stopped"};
    if (deadlineAtMs !== null && deadlineAtMs <= Date.now()) return {clicked: false, reason: "run_deadline"};
    const initial = await this.bounded(() => this.page.evaluate(reservation => {
      const guard = (window as GuardWindow).quizManualGuard;
      return guard?.reserveAnswer(reservation) ?? (guard ? null : {clicked: false, reason: "guard_missing"});
    }, {commandId: command.commandId, round: command.round, mode: command.mode, delayMs: command.delayMs,
      spreadOffsetMs: spreadOffset(command, serial), issuedAtMs: command.issuedAtMs, deadlineAtMs,
      option: (serial - 1) % 4}), this.operationTimeoutMs);
    this.assertActive();
    if (initial) return initial;
    // AWSの人待ちだけを明示停止まで許し、登録evaluateと停止処理の有限timeoutは維持する。
    const waitForAnswer = (): Promise<ManualClick> => this.page.evaluate(commandId => {
      const guard = (window as GuardWindow).quizManualGuard;
      return guard?.waitForAnswer(commandId) ?? {clicked: false, reason: "guard_missing"};
    }, command.commandId);
    if (deadlineAtMs === null) return this.untilStopped(waitForAnswer);
    return this.bounded(waitForAnswer, Math.max(1, deadlineAtMs - Date.now()) + this.operationTimeoutMs);
  }

  async observeResult(round: number, timeout: number): Promise<{unanswered: boolean} | null> {
    return this.bounded(async () => this.page.evaluate(round => {
      const root = Array.from(document.querySelectorAll(".participant-result-stage")).find(element =>
        element.querySelector(".participant-result-round")?.getAttribute("data-round-number") === String(round));
      const score = root?.querySelector(".participant-score-result");
      if (!(score instanceof HTMLElement) || score.getClientRects().length === 0 ||
        score.classList.contains("participant-result-pending") ||
        score.closest('[inert], [hidden], [aria-hidden="true"]') !== null) return null;
      // 接続済みDOMだけでは、非表示tabやoverlay下の結果を表示済みと扱えない。
      for (let element: HTMLElement | null = score; element !== null; element = element.parentElement) {
        const style = getComputedStyle(element);
        if (style.display === "none" || ["hidden", "collapse"].includes(style.visibility) || style.opacity === "0") return null;
      }
      return {unanswered: Boolean(root?.querySelector(".outcome-unanswered"))};
    }, round), timeout);
  }

  private async performClick(command: Extract<ManualAction, {kind: "join"}>, timeout: number): Promise<ManualClick> {
    await this.page.evaluate(({commandId, kind, round}) => {
      const guard = (window as GuardWindow).quizManualGuard;
      if (!guard || guard.paused || guard.stopped) return;
      guard.armed = {commandId, kind, round}; guard.clicked = null;
    }, {commandId: command.commandId, kind: command.kind, round: 0});
    try {
      this.assertActive();
      if (this.stopped) return {clicked: false};
      await this.page.getByRole("button", {name: "参加する", exact: true}).click({timeout});
      this.assertActive();
      return await this.page.evaluate(commandId => {
        const clicked = (window as GuardWindow).quizManualGuard?.clicked;
        return clicked?.commandId === commandId ? {clicked: true, browserMonotonicMs: clicked.monotonicMs} : {clicked: false};
      }, command.commandId);
    } finally {
      if (!this.timedOut && !this.page.isClosed()) await this.page.evaluate(commandId => {
        const guard = (window as GuardWindow).quizManualGuard;
        if (guard?.armed?.commandId === commandId) guard.armed = null;
      }, command.commandId);
    }
  }

  private assertActive(): void {
    if (this.timedOut) throw Object.assign(new Error("manual_browser_timeout"), {name: "TimeoutError"});
  }

  private async untilStopped(operation: () => Promise<ManualClick>): Promise<ManualClick> {
    if (this.stopped) return {clicked: false, reason: "run_stopped"};
    let cancel!: () => void;
    const stopped = new Promise<ManualClick>(resolve => {
      cancel = () => resolve({clicked: false, reason: "run_stopped"});
      this.stopController.signal.addEventListener("abort", cancel, {once: true});
    });
    try { return await Promise.race([operation(), stopped]); }
    finally { this.stopController.signal.removeEventListener("abort", cancel); }
  }

  private async bounded<T>(operation: () => Promise<T>, timeout: number): Promise<T> {
    this.assertActive();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const deadline = new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => {
        // evaluateにも上限を置き、遅れて返った操作から後続clickを始めない。
        this.timedOut = true;
        void this.page.close({runBeforeUnload: false}).catch(() => undefined);
        reject(Object.assign(new Error("manual_browser_timeout"), {name: "TimeoutError"}));
      }, timeout);
    });
    try { return await Promise.race([operation(), deadline]); }
    finally { clearTimeout(timer); }
  }
}
