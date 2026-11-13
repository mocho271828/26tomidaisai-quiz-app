export type ManualClick = {
  clicked: boolean;
  browserMonotonicMs?: number;
  browserPlannedMonotonicMs?: number;
  reason?: string;
};

type AnswerReservation = {
  commandId: string; round: number; mode: "now" | "when-open"; delayMs: number;
  spreadOffsetMs: number; issuedAtMs: number; deadlineAtMs: number | null; option: number;
};
type PendingAnswer = AnswerReservation & {
  planned?: number; answerDeadline?: number; timer?: number;
  result: Promise<ManualClick>; resolve: (result: ManualClick) => void; done: boolean; executing: boolean;
};
export type BrowserGuard = {
  paused: boolean; stopped: boolean;
  armed: {commandId: string; kind: "join" | "answer"; round: number; deadlineAtMs?: number | null} | null;
  clicked: {commandId: string; monotonicMs: number} | null;
  deadline: {round: number; monotonicMs: number} | null;
  reserveAnswer: (reservation: AnswerReservation) => ManualClick | null;
  waitForAnswer: (commandId: string) => Promise<ManualClick>;
  setPaused: (paused: boolean, stopped: boolean) => void;
};
export type GuardWindow = Window & {
  quizManualGuard?: BrowserGuard;
  quizRunnerProgress?: {round: number; state: string};
};

// addInitScriptで関数本体だけが転送されるため、実行時の依存を関数内に閉じる。
export function installManualBrowserGuard(): void {
  const target = window as GuardWindow;
  const pending = new Map<string, PendingAnswer>();
  const reservedRounds = new Set<number>();
  const clickedRounds = new Set<number>();
  const closedRounds = new Set<number>();
  let currentRound = 0;
  let display: {round: number; answerable: boolean} | undefined;
  const guard: BrowserGuard = {
    paused: false, stopped: false, armed: null, clicked: null, deadline: null,
    reserveAnswer, waitForAnswer: commandId => pending.get(commandId)?.result ??
      Promise.resolve({clicked: false, reason: "reservation_missing"}),
    setPaused(paused, stopped) {
      guard.stopped ||= stopped;
      guard.paused = guard.stopped || paused;
      for (const operation of pending.values()) schedule(operation);
    },
  };
  target.quizManualGuard = guard;

  function isAnswerable(round: number): boolean {
    const progress = target.quizRunnerProgress;
    return display?.round === round && display.answerable && progress?.round === round &&
      ["Scheduled", "Open"].includes(progress.state);
  }

  function finish(operation: PendingAnswer, result: ManualClick): void {
    if (operation.done || operation.executing) return;
    operation.done = true;
    clearTimeout(operation.timer);
    if (guard.armed?.commandId === operation.commandId) guard.armed = null;
    // nowはNodeのissuedAtMs由来なので、未校正の時計差をbrowser内遅延標本に混ぜない。
    operation.resolve({...result, browserPlannedMonotonicMs: operation.mode === "when-open" ? operation.planned : undefined});
  }

  function schedule(operation: PendingAnswer): void {
    if (operation.done || operation.executing) return;
    clearTimeout(operation.timer);
    const skip = (reason: string): void => finish(operation, {clicked: false, reason});
    if (guard.stopped) { skip("run_stopped"); return; }
    const runRemaining = operation.deadlineAtMs === null ? null : operation.deadlineAtMs - Date.now();
    if (runRemaining !== null && runRemaining <= 0) { skip("run_deadline"); return; }
    if (currentRound > operation.round || closedRounds.has(operation.round)) { skip("round_expired"); return; }
    const now = performance.now();
    if (operation.planned === undefined && isAnswerable(operation.round)) {
      if (guard.deadline?.round !== operation.round) { skip("answer_deadline_missing"); return; }
      operation.planned = operation.mode === "when-open"
        ? now + operation.delayMs + operation.spreadOffsetMs
        : now + operation.issuedAtMs + operation.delayMs + operation.spreadOffsetMs - Date.now();
      operation.answerDeadline = guard.deadline.monotonicMs;
    }
    if (operation.planned === undefined) {
      if (runRemaining !== null) operation.timer = window.setTimeout(() => schedule(operation), runRemaining);
      return;
    }
    if (guard.deadline?.round === operation.round) operation.answerDeadline = guard.deadline.monotonicMs;
    const remaining = (operation.answerDeadline ?? now) - now;
    if (remaining <= 0 || operation.planned >= (operation.answerDeadline ?? now)) {
      skip("answer_deadline_expired"); return;
    }
    if (guard.paused || !isAnswerable(operation.round) || guard.deadline?.round !== operation.round) {
      operation.timer = window.setTimeout(() => schedule(operation), Math.min(remaining, runRemaining ?? remaining));
      return;
    }
    if (operation.planned > now) {
      operation.timer = window.setTimeout(() => schedule(operation), Math.min(operation.planned - now, remaining, runRemaining ?? remaining));
      return;
    }
    const question = Array.from(document.querySelectorAll(".participant-question")).find(element =>
      element.querySelector(".participant-round-badge")?.getAttribute("data-round-number") === String(operation.round));
    const button = question?.querySelectorAll(".answer-button")[operation.option];
    if (!(button instanceof HTMLButtonElement)) { skip("answer_dom_unavailable"); return; }
    guard.armed = {commandId: operation.commandId, kind: "answer", round: operation.round, deadlineAtMs: operation.deadlineAtMs};
    guard.clicked = null;
    // 回答はReactの通常onClickへ渡す。HTTP送信やアプリ状態の直接変更はしない。
    operation.executing = true;
    button.click();
    operation.executing = false;
    const clicked = guard.clicked as {commandId: string; monotonicMs: number} | null;
    finish(operation, clicked?.commandId === operation.commandId
      ? {clicked: true, browserMonotonicMs: clicked.monotonicMs}
      : {clicked: false, reason: "click_guard_blocked"});
  }

  function reserveAnswer(reservation: AnswerReservation): ManualClick | null {
    if (guard.stopped) return {clicked: false, reason: "run_stopped"};
    if (pending.has(reservation.commandId) || reservedRounds.has(reservation.round))
      return {clicked: false, reason: "duplicate_operation"};
    if (currentRound > reservation.round || closedRounds.has(reservation.round))
      return {clicked: false, reason: "round_expired"};
    if (reservation.mode === "now" && !isAnswerable(reservation.round))
      return {clicked: false, reason: "round_not_answerable"};
    let resolve!: (result: ManualClick) => void;
    const result = new Promise<ManualClick>(done => { resolve = done; });
    const operation: PendingAnswer = {...reservation, result, resolve, done: false, executing: false};
    pending.set(reservation.commandId, operation);
    reservedRounds.add(reservation.round);
    schedule(operation);
    return null;
  }

  window.addEventListener("quiz:observation", event => {
    const detail: unknown = (event as CustomEvent<unknown>).detail;
    if (typeof detail !== "object" || detail === null || !("kind" in detail) ||
      !("round_number" in detail) || typeof detail.round_number !== "number") return;
    if (detail.kind !== "projection_received" && detail.kind !== "participant_display") return;
    currentRound = Math.max(currentRound, detail.round_number);
    if (detail.kind === "projection_received" && "state" in detail && typeof detail.state === "string") {
      target.quizRunnerProgress = {round: detail.round_number, state: detail.state};
      if (["ReadyToReveal", "AwaitingValidation", "VoidCandidate", "Reveal", "Voided",
        "RoundBackupPending", "RoundBackupValidated", "RoundBackupFailed", "RoundBackupRetrying"].includes(detail.state))
        closedRounds.add(detail.round_number);
    }
    if (detail.kind === "participant_display") {
      const isAnswering = "phase" in detail && detail.phase === "answering";
      const synchronized = "synchronized" in detail && detail.synchronized === true;
      display = {round: detail.round_number, answerable: isAnswering && synchronized &&
        "has_question" in detail && detail.has_question === true};
      guard.deadline = isAnswering && synchronized && "answer_deadline_monotonic_ms" in detail &&
        typeof detail.answer_deadline_monotonic_ms === "number" && Number.isFinite(detail.answer_deadline_monotonic_ms)
        ? {round: detail.round_number, monotonicMs: detail.answer_deadline_monotonic_ms} : null;
      if ("phase" in detail && ["collecting", "result", "completed"].includes(String(detail.phase)))
        closedRounds.add(detail.round_number);
    }
    // 未来問のrun期限timerは予約時のまま維持し、現在/過去問の状態変化だけを反映する。
    for (const operation of pending.values()) if (operation.round <= currentRound) schedule(operation);
  });

  // timerと描画更新の順序が変わっても、期限後や隠れたDOMへのclickはReactへ渡さない。
  document.addEventListener("click", event => {
    const button = event.target instanceof Element ? event.target.closest("button") : null;
    if (!(button instanceof HTMLButtonElement)) return;
    const isAnswer = button.classList.contains("answer-button");
    const isJoin = button.textContent?.trim() === "参加する";
    if (!isAnswer && !isJoin) return;
    const arm = guard.armed;
    const correctQuestion = arm !== null && button.closest(".participant-question")
      ?.querySelector(".participant-round-badge")?.getAttribute("data-round-number") === String(arm.round);
    const progress = target.quizRunnerProgress;
    const isCurrent = arm !== null && progress?.round === arm.round && ["Scheduled", "Open"].includes(progress.state);
    const beforeDeadline = arm !== null && guard.deadline?.round === arm.round && performance.now() < guard.deadline.monotonicMs;
    let visible = button.getClientRects().length > 0;
    for (let element: HTMLElement | null = button; visible && element !== null; element = element.parentElement) {
      const style = getComputedStyle(element);
      visible = style.display !== "none" && !["hidden", "collapse"].includes(style.visibility) && style.opacity !== "0";
    }
    const allowed = arm !== null && !guard.paused && !guard.stopped && !button.disabled && visible &&
      (arm.deadlineAtMs === undefined || arm.deadlineAtMs === null || Date.now() < arm.deadlineAtMs) &&
      (isJoin ? arm.kind === "join" : arm.kind === "answer" && correctQuestion && isCurrent && beforeDeadline &&
        !clickedRounds.has(arm.round) && button.closest(".participant-live-shell-answering") !== null &&
        button.closest('[inert], [hidden], [aria-hidden="true"]') === null);
    if (!allowed) { event.preventDefault(); event.stopImmediatePropagation(); return; }
    guard.clicked = {commandId: arm.commandId, monotonicMs: performance.now()};
    if (isAnswer) clickedRounds.add(arm.round);
    guard.armed = null;
  }, true);
}
