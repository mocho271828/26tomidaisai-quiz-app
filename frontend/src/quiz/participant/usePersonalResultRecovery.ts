import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { getParticipantResult } from "../../api/audienceClient";
import { ApiCancellation, type CancellationReason } from "../../api/cancellation";
import { ApiResponseError, ApiTransportError } from "../../api/client";
import type { PersonalResultResponse } from "../../api/contracts";
import { personalStateKey, resultRevisionKey, type ParticipantViewState } from "./participantState";

// 未実測の暫定値。進行とは独立した表示補完だけを有界にする。
const RESULT_RECOVERY_LIMIT_MS = 30_000;
const RESULT_RECOVERY_ATTEMPTS = 3;
const RESULT_RETRY_BASE_MS = 5_000;

type Options = {
  state: ParticipantViewState | null;
  authenticationInvalidated?: boolean;
  onResult: (response: PersonalResultResponse) => void;
  onAuthenticationLost: () => void;
};

type Budget = { key: string; attempts: number; deadline: number };

function recoveryCancellationReason(mounted: boolean, options: Options, key: string): CancellationReason | undefined {
  if (!mounted) return "unmount";
  if (options.authenticationInvalidated === true) return "auth_invalidated";
  if (document.visibilityState !== "visible") return "hidden";
  if (options.state === null) return undefined;
  if (personalStateKey(options.state) !== key) return "revision_changed";
  if (options.state.received_result_revision !== undefined &&
      resultRevisionKey(options.state.received_result_revision) === key) return "result_received";
  return undefined;
}

export function usePersonalResultRecovery(options: Options): void {
  const optionsRef = useRef(options);
  const budgetRef = useRef<Budget | null>(null);
  const visibilityRef = useRef(0);
  const mountedRef = useRef(true);
  const [visibilityGeneration, setVisibilityGeneration] = useState(0);
  const state = options.state;
  const authenticationInvalidated = options.authenticationInvalidated === true;
  const key = state === null ? null : personalStateKey(state);
  const delivery = state?.personal_result_delivery;
  const isReceived = state?.received_result_revision !== undefined &&
    resultRevisionKey(state.received_result_revision) === key;
  const ready = delivery?.status === "ready" && delivery.revision !== null &&
    resultRevisionKey(delivery.revision) === key && !isReceived;

  // passive cleanupより先に、commit済みの失効・受信・版変更を参照できるようにする。
  useLayoutEffect(() => { optionsRef.current = options; });
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);
  useEffect(() => {
    let visibility = document.visibilityState;
    const changed = (): void => {
      if (visibility === document.visibilityState) return;
      visibility = document.visibilityState;
      visibilityRef.current += 1;
      setVisibilityGeneration(visibilityRef.current);
    };
    document.addEventListener("visibilitychange", changed);
    return () => document.removeEventListener("visibilitychange", changed);
  }, []);

  useEffect(() => {
    if (key === null) { budgetRef.current = null; return; }
    if (authenticationInvalidated || !ready || document.visibilityState !== "visible") return;
    if (budgetRef.current?.key !== key) {
      budgetRef.current = { key, attempts: 0, deadline: performance.now() + RESULT_RECOVERY_LIMIT_MS };
    }
    const budget = budgetRef.current;
    const controller = new AbortController();
    let timer: number | undefined;
    const current = (): boolean => {
      const latest = optionsRef.current.state;
      return !controller.signal.aborted && optionsRef.current.authenticationInvalidated !== true &&
        document.visibilityState === "visible" &&
        visibilityRef.current === visibilityGeneration &&
        latest !== null && personalStateKey(latest) === key &&
        latest.personal_result_delivery.status === "ready" &&
        (latest.received_result_revision === undefined || resultRevisionKey(latest.received_result_revision) !== key);
    };
    const schedule = (): void => {
      if (!current() || budget.attempts >= RESULT_RECOVERY_ATTEMPTS) return;
      const remaining = budget.deadline - performance.now();
      const delay = RESULT_RETRY_BASE_MS * 2 ** Math.max(0, budget.attempts - 1) * (1 + Math.random() * 0.2);
      if (remaining <= delay) return;
      timer = window.setTimeout(() => { void retrieve(); }, delay);
    };
    const retrieve = async (): Promise<void> => {
      if (!current() || performance.now() >= budget.deadline) return;
      budget.attempts += 1;
      try {
        const result = await getParticipantResult(controller.signal);
        if (!current() || performance.now() >= budget.deadline) return;
        optionsRef.current.onResult(result);
        if (result.delivery.status !== "ready") return;
      } catch (error: unknown) {
        if (!current()) return;
        if (error instanceof ApiResponseError && error.status === 401) {
          optionsRef.current.onAuthenticationLost();
          return;
        }
        if (!(error instanceof ApiTransportError ||
          (error instanceof ApiResponseError && error.status >= 500) ||
          (error instanceof DOMException && (error.name === "AbortError" || error.name === "TimeoutError")))) return;
      }
      schedule();
    };
    const deadlineTimer = window.setTimeout(() => controller.abort(new ApiCancellation("deadline")), Math.max(0, budget.deadline - performance.now()));
    schedule();
    return () => {
      const reason = recoveryCancellationReason(mountedRef.current, optionsRef.current, key);
      // 同じ版のunavailableや理由不明の状態消失を正常取消と推定しない。
      controller.abort(reason === undefined
        ? new DOMException("API request cancelled", "AbortError") : new ApiCancellation(reason));
      window.clearTimeout(timer);
      window.clearTimeout(deadlineTimer);
    };
  }, [authenticationInvalidated, key, ready, visibilityGeneration]);
}
