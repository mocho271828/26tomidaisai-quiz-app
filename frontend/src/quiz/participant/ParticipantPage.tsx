import { useCallback, useEffect, useRef, useState } from "react";

import {
  getParticipantSession,
} from "../../api/audienceClient";
import {
  ApiResponseError,
  ApiTransportError,
  errorMessage,
} from "../../api/client";
import type { ParticipantState, PersonalResultDelivery, PersonalResultResponse } from "../../api/contracts";
import type { ParticipantCommonState, RealtimeStateMessage } from "../../api/realtimeContracts";
import { appConfig } from "../../config";
import { useRealtimeRefresh } from "../_hooks/useRealtimeRefresh";
import { CLOCK_RECHECK_EVENT, useServerClock } from "../_hooks/useServerClock";
import { ParticipantEntryView } from "./ParticipantEntryView";
import { ParticipantJoinedView } from "./ParticipantJoinedView";
import { clearPendingParticipantJoin } from "./pendingParticipantJoin";
import {
  clearPendingSubmission,
  loadPendingSubmission,
  savePendingSubmission,
  type PendingSubmission,
} from "./pendingSubmission";
import {
  createAcceptedParticipantSubmission,
  createParticipantSubmission,
  isSameSubmission,
  type ParticipantInFlightRequest,
  type ParticipantSubmission,
} from "./participantUi";
import { useParticipantJoin } from "./useParticipantJoin";
import { useParticipantSubmissionRecovery } from "./useParticipantSubmissionRecovery";
import { useSingleFlightRefresh } from "../_hooks/useSingleFlightRefresh";
import { observeBrowser } from "../_utils/browserObservation";
import {
  applyParticipantCommonState,
  applyParticipantPersonalState,
  applyParticipantResult,
  applyPersonalResultDelivery,
  type ParticipantViewState,
} from "./participantState";
import { submitTimedAnswer } from "./submitTimedAnswer";
import { usePersonalResultRecovery } from "./usePersonalResultRecovery";

type ParticipantPhase = "checking" | "join" | "joined";

export function ParticipantPage({ autoJoin = false }: { autoJoin?: boolean }) {
  const [phase, setPhase] = useState<ParticipantPhase>("checking");
  const [sessionCheckAttempt, setSessionCheckAttempt] = useState(0);
  const [projection, setProjection] = useState<ParticipantViewState | null>(null);
  const [pageError, setPageError] = useState<string | null>(null);
  const [isStateFresh, setIsStateFresh] = useState(false);
  const [submission, setSubmissionState] =
    useState<ParticipantSubmission | null>(() => {
      const pending = loadPendingSubmission();
      return pending === null
        ? null
        : createParticipantSubmission(pending, "unknown", "前回の回答について保存状態を確認しています。");
    });
  const checkedPendingIdsRef = useRef(new Set<string>());
  const projectionRef = useRef<ParticipantViewState | null>(null);
  const commonStateRef = useRef<ParticipantCommonState | null>(null);
  const submissionRef = useRef(submission);
  const sessionGenerationRef = useRef(0);
  const isMountedRef = useRef(true);
  const answerInFlightRef = useRef<ParticipantInFlightRequest | null>(null);
  const stateFreshRef = useRef(false);
  const visibilityGenerationRef = useRef(0);
  const recoveryGenerationRef = useRef(0);
  const visibilityRef = useRef(document.visibilityState);
  const joinVisibilityGenerationRef = useRef(0);
  const resumeCheckRequiredRef = useRef(false);
  const autoJoinAttemptedRef = useRef(false);
  const serverClock = useServerClock();

  useEffect(() => {
    isMountedRef.current = true;
    return () => { isMountedRef.current = false; };
  }, []);

  const updateStateFreshness = useCallback((isFresh: boolean): void => {
    stateFreshRef.current = isFresh;
    setIsStateFresh(isFresh);
  }, []);

  const updateSubmission = useCallback(
    (next: ParticipantSubmission | null): void => {
      submissionRef.current = next;
      setSubmissionState(next);
    },
    [],
  );
  const getRecoveryContext = useCallback(() => ({
    generation: sessionGenerationRef.current,
    roundId: projectionRef.current?.current_round?.round_id ?? null,
    roundNumber: projectionRef.current?.current_round?.round_number ?? 0,
    submission: submissionRef.current,
  }), []);
  const { verifySubmission: recoverSubmission, cancelRecovery } = useParticipantSubmissionRecovery(
    getRecoveryContext, updateSubmission,
  );

  const acceptProjection = useCallback((
    candidate: ParticipantViewState,
    generation: number,
  ): boolean => {
    if (sessionGenerationRef.current !== generation) {
      return false;
    }
    const previousRoundId =
      projectionRef.current?.current_round?.round_id ?? null;
    const currentRoundId = candidate.current_round?.round_id ?? null;
    if (candidate.current_round !== null) {
      observeBrowser({ kind: "projection_received", round_number: candidate.current_round.round_number, state: candidate.current_round.state, has_question: candidate.current_round.question !== undefined });
    }
    projectionRef.current = candidate;
    autoJoinAttemptedRef.current = true;
    setProjection(candidate);
    setPhase("joined");

    const answer =
      candidate.answer?.round_id === currentRoundId
        ? candidate.answer
        : undefined;
    if (answer !== undefined && candidate.current_round !== null) {
      cancelRecovery(previousRoundId !== currentRoundId ? "revision_changed" : "result_received");
      observeBrowser({ kind: "answer_apply", source: "projection", outcome: "accepted", round_number: candidate.current_round.round_number });
      answerInFlightRef.current = null;
      clearPendingSubmission();
      updateSubmission(
        createAcceptedParticipantSubmission(
          answer,
          candidate.current_round?.version ?? 1,
        ),
      );
    } else if (
      submissionRef.current !== null &&
      submissionRef.current.roundId !== currentRoundId
    ) {
      clearPendingSubmission();
      updateSubmission(null);
    }
    if (previousRoundId !== currentRoundId) {
      checkedPendingIdsRef.current.clear();
      answerInFlightRef.current = null;
      cancelRecovery("revision_changed");
    }
    return true;
  }, [cancelRecovery, updateSubmission]);

  const beginParticipantJoin = useCallback((): number => {
    cancelRecovery("superseded");
    sessionGenerationRef.current += 1;
    const generation = sessionGenerationRef.current;
    joinVisibilityGenerationRef.current = visibilityGenerationRef.current;
    projectionRef.current = null;
    commonStateRef.current = null;
    setProjection(null);
    updateStateFreshness(false);
    clearPendingSubmission();
    updateSubmission(null);
    checkedPendingIdsRef.current.clear();
    answerInFlightRef.current = null;
    return generation;
  }, [cancelRecovery, updateStateFreshness, updateSubmission]);

  const isSessionGenerationCurrent = useCallback(
    (generation: number): boolean => sessionGenerationRef.current === generation,
    [],
  );

  const acceptJoinProjection = useCallback((candidate: ParticipantState, generation: number): boolean => {
    const merged = applyParticipantPersonalState(null, candidate, null);
    const accepted = merged !== null && acceptProjection(merged, generation);
    if (accepted) {
      const isFresh = joinVisibilityGenerationRef.current === visibilityGenerationRef.current &&
        document.visibilityState === "visible";
      if (isFresh) resumeCheckRequiredRef.current = false;
      updateStateFreshness(isFresh);
    }
    return accepted;
  }, [acceptProjection, updateStateFreshness]);
  const participantJoin = useParticipantJoin({
    acceptProjection: acceptJoinProjection,
    beginJoin: beginParticipantJoin,
    isGenerationCurrent: isSessionGenerationCurrent,
    onError: setPageError,
  });
  const joinParticipantSession = participantJoin.join;
  const resetParticipantJoin = participantJoin.reset;

  const retryParticipantSessionCheck = useCallback((): void => {
    setPageError(null);
    setPhase("checking");
    setSessionCheckAttempt((attempt) => attempt + 1);
  }, []);

  const enterJoinPhase = useCallback(
    (expectedGeneration: number): boolean => {
      if (sessionGenerationRef.current !== expectedGeneration) {
        return false;
      }
      sessionGenerationRef.current += 1;
      resetParticipantJoin();
      projectionRef.current = null;
      commonStateRef.current = null;
      answerInFlightRef.current = null;
      cancelRecovery("auth_invalidated");
      setProjection(null);
      updateStateFreshness(false);
      setPhase("join");
      return true;
    },
    [cancelRecovery, resetParticipantJoin, updateStateFreshness],
  );

  const fetchState = useCallback(async (signal: AbortSignal): Promise<boolean> => {
    if (document.visibilityState !== "visible") return false;
    const generation = sessionGenerationRef.current;
    const visibilityGeneration = visibilityGenerationRef.current;
    const recoveryGeneration = recoveryGenerationRef.current;
    try {
      const candidate = await getParticipantSession(signal);
      if (signal.aborted || recoveryGenerationRef.current !== recoveryGeneration) return false;
      const merged = applyParticipantPersonalState(projectionRef.current, candidate, commonStateRef.current);
      if (signal.aborted || merged === null || !acceptProjection(merged, generation)) {
        return false;
      }
      clearPendingParticipantJoin();
      // 復帰前に開始したGETは最新確認とみなさず、復帰後の取得だけで回答操作を戻す。
      if (visibilityGenerationRef.current === visibilityGeneration && document.visibilityState === "visible") {
        resumeCheckRequiredRef.current = false;
        updateStateFreshness(true);
      }
      setPageError(null);
      return true;
    } catch (error: unknown) {
      if (signal.aborted || sessionGenerationRef.current !== generation ||
        recoveryGenerationRef.current !== recoveryGeneration) {
        return false;
      }
      if (error instanceof ApiResponseError && error.status === 401) {
        const isJoinRequired = enterJoinPhase(generation);
        // 復帰時もsession確認を終えてから参加する。並行するGETの401で参加成功を無効化しない。
        if (isJoinRequired && autoJoin && !autoJoinAttemptedRef.current && document.visibilityState === "visible") {
          autoJoinAttemptedRef.current = true;
          void joinParticipantSession();
        }
        return false;
      }
      setPageError(errorMessage(error));
      return false;
    }
  }, [acceptProjection, autoJoin, enterJoinPhase, joinParticipantSession, updateStateFreshness]);

  const getRefreshGeneration = useCallback((): string =>
    `${sessionGenerationRef.current}:${visibilityGenerationRef.current}:${recoveryGenerationRef.current}`, []);
  const refresh = useSingleFlightRefresh(fetchState, getRefreshGeneration);

  const acceptCommonState = useCallback((message: RealtimeStateMessage): boolean => {
    if (message.type !== "participant.state" || projectionRef.current === null) return false;
    const common = message.state;
    const previous = commonStateRef.current;
    const merged = applyParticipantCommonState(projectionRef.current, common, previous);
    if (merged === null) return false;
    commonStateRef.current = common;
    const accepted = acceptProjection(merged, sessionGenerationRef.current);
    if (accepted && !resumeCheckRequiredRef.current && document.visibilityState === "visible") {
      updateStateFreshness(true);
    }
    return accepted;
  }, [acceptProjection, updateStateFreshness]);

  const acceptResultDelivery = useCallback((delivery: PersonalResultDelivery): void => {
    const current = projectionRef.current;
    if (current === null) return;
    const next = applyPersonalResultDelivery(current, delivery);
    if (next === null) return;
    projectionRef.current = next;
    setProjection(next);
  }, []);
  const acceptResult = useCallback((response: PersonalResultResponse): void => {
    const current = projectionRef.current;
    if (current === null) return;
    const next = applyParticipantResult(current, response);
    if (next === null) { acceptResultDelivery(response.delivery); return; }
    // 結果だけの遅着で回答操作・保存確認・進行の鮮度を変更しない。
    projectionRef.current = next;
    setProjection(next);
  }, [acceptResultDelivery]);

  usePersonalResultRecovery({ state: projection, authenticationInvalidated: phase === "join", onResult: acceptResult,
    onAuthenticationLost: () => enterJoinPhase(sessionGenerationRef.current) });

  useEffect(() => {
    void refresh();
  }, [refresh, sessionCheckAttempt]);

  useEffect(() => {
    const onVisibilityChange = (): void => {
      if (visibilityRef.current === document.visibilityState) return;
      visibilityRef.current = document.visibilityState;
      visibilityGenerationRef.current += 1;
      if (document.visibilityState !== "visible") resumeCheckRequiredRef.current = true;
      updateStateFreshness(false);
      // joined後の復帰取得はWS hookに集約する。初回確認中もhiddenで開始しない。
      if (document.visibilityState === "visible" && phase !== "joined") void refresh();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => document.removeEventListener("visibilitychange", onVisibilityChange);
  }, [phase, refresh, updateStateFreshness]);

  useEffect(() => {
    const recheckState = (): void => {
      visibilityGenerationRef.current += 1;
      resumeCheckRequiredRef.current = true;
      updateStateFreshness(false);
      if (document.visibilityState === "visible") void refresh();
    };
    window.addEventListener(CLOCK_RECHECK_EVENT, recheckState);
    return () => window.removeEventListener(CLOCK_RECHECK_EVENT, recheckState);
  }, [refresh, updateStateFreshness]);

  useRealtimeRefresh({
    audience: "participant",
    enabled: phase === "joined",
    eventId: projection?.event.event_id ?? null,
    path: `${appConfig.VITE_WS_BASE_URL}/participant/realtime`,
    refresh,
    onState: acceptCommonState,
    refreshOnConnect: false,
    onPersonalResult: acceptResult,
    onPersonalResultDelivery: acceptResultDelivery,
    onUnavailable: () => {
      recoveryGenerationRef.current += 1;
      resumeCheckRequiredRef.current = true;
      updateStateFreshness(false);
    },
    onAuthenticationLost: () => enterJoinPhase(sessionGenerationRef.current),
  });

  const sendAnswer = async (pending: PendingSubmission): Promise<void> => {
    const generation = sessionGenerationRef.current;
    const currentRound = projectionRef.current?.current_round;
    const currentSubmission = submissionRef.current;
    const isConfirmedRetry =
      currentSubmission !== null &&
      currentSubmission.status === "not-stored" &&
      isSameSubmission(currentSubmission, pending);
    if (
      document.visibilityState !== "visible" ||
      !stateFreshRef.current ||
      !serverClock.isSynchronized ||
      currentRound === null ||
      currentRound === undefined ||
      currentRound.round_id !== pending.round_id ||
      (!isConfirmedRetry && currentRound.version !== pending.round_version) ||
      (currentSubmission?.roundId === pending.round_id && !isConfirmedRetry) ||
      answerInFlightRef.current !== null
    ) {
      return;
    }
    const clockOffset = serverClock.monotonicOffsetMs;
    const openAt = currentRound.open_at === null ? Number.NaN : Date.parse(currentRound.open_at);
    const closeAt = currentRound.standard_close_at === null ? Number.NaN : Date.parse(currentRound.standard_close_at);
    const graceMs = projectionRef.current!.event.arrival_grace_ms;
    const canSendInWindow = (): boolean => {
      if (clockOffset === undefined || serverClock.isCurrent?.() === false || !stateFreshRef.current ||
        document.visibilityState !== "visible") return false;
      const now = performance.now() - clockOffset;
      return Number.isInteger(pending.client_elapsed_ms) && pending.client_elapsed_ms >= 0 &&
        pending.client_elapsed_ms < closeAt - openAt && now >= openAt &&
        now < closeAt + graceMs && now < openAt + pending.client_elapsed_ms + graceMs;
    };
    if (!canSendInWindow()) return;
    cancelRecovery("superseded");
    // 手動同一回答再送も未確定になり得るため、この新しいPOSTには有限照会を1本許可する。
    checkedPendingIdsRef.current.delete(pending.client_answer_id);
    const attempt = {
      clientAnswerId: pending.client_answer_id,
      generation,
    };
    const isCurrentAttempt = (): boolean =>
      isMountedRef.current && sessionGenerationRef.current === generation &&
      projectionRef.current?.current_round?.round_id === pending.round_id &&
      answerInFlightRef.current?.clientAnswerId === pending.client_answer_id &&
      answerInFlightRef.current.generation === generation &&
      submissionRef.current !== null &&
      submissionRef.current.status === "submitting" &&
      isSameSubmission(submissionRef.current, pending);
    answerInFlightRef.current = attempt;
    updateSubmission(
      createParticipantSubmission(pending, "submitting", "回答を保存しています…"),
    );
    if (!savePendingSubmission(pending)) {
      updateSubmission(
        createParticipantSubmission(
          pending,
          "rejected",
          "回答を送信できませんでした。ブラウザ設定を確認してください。",
        ),
      );
      answerInFlightRef.current = null;
      return;
    }
    observeBrowser({ kind: "answer", phase: "sending", round_number: currentRound.round_number });
    try {
      const receipt = await submitTimedAnswer(pending, currentRound.round_number,
        () => !isConfirmedRetry && isCurrentAttempt() && canSendInWindow());
      if (!isCurrentAttempt()) {
        observeBrowser({ kind: "answer_apply", source: "post", outcome: "stale", round_number: currentRound.round_number });
        return;
      }
      if (
        receipt.round_id !== pending.round_id ||
        receipt.option_id !== pending.option_id ||
        receipt.client_answer_id !== pending.client_answer_id ||
        receipt.client_elapsed_ms !== pending.client_elapsed_ms || receipt.timing_basis !== "client_monotonic_v1"
      ) {
        observeBrowser({ kind: "answer_apply", source: "post", outcome: "identity_mismatch", round_number: currentRound.round_number });
        updateSubmission(
          createParticipantSubmission(
            pending,
            "unknown",
            "エラーが発生しました",
          ),
        );
        return;
      }
      observeBrowser({ kind: "answer", phase: "accepted", round_number: currentRound.round_number });
      observeBrowser({ kind: "answer_apply", source: "post", outcome: "accepted", round_number: currentRound.round_number });
      clearPendingSubmission();
      updateSubmission(
        {
          ...createParticipantSubmission(
            pending,
            "accepted",
            "回答を保存しました。",
          ),
          receivedAt: receipt.received_at,
        },
      );
    } catch (error: unknown) {
      if (!isCurrentAttempt()) {
        observeBrowser({ kind: "answer_apply", source: "post", outcome: "stale", round_number: currentRound.round_number });
        return;
      }
      observeBrowser({ kind: "answer", phase: error instanceof ApiResponseError && error.status < 500 ? "rejected" : "unknown", round_number: currentRound.round_number });
      observeBrowser({ kind: "answer_apply", source: "post", outcome: error instanceof ApiResponseError && error.status < 500 ? "rejected" : "unknown", round_number: currentRound.round_number });
      if (error instanceof ApiResponseError && error.status < 500) {
        clearPendingSubmission();
        updateSubmission(
          createParticipantSubmission(pending, "rejected", error.message),
        );
        await refresh();
        return;
      }
      updateSubmission(
        createParticipantSubmission(
          pending,
          "unknown",
          error instanceof ApiTransportError
            ? "通信が途切れたため、通信環境を確認してください。"
            : "サーバー応答を確認してください。",
        ),
      );
    } finally {
      if (
        answerInFlightRef.current?.clientAnswerId === attempt.clientAnswerId &&
        answerInFlightRef.current.generation === attempt.generation
      ) {
        answerInFlightRef.current = null;
      }
    }
  };

  const verifySubmission = useCallback(async (target: ParticipantSubmission): Promise<void> => {
    const outcome = await recoverSubmission(target);
    if (outcome === "accepted" || outcome === "unauthorized") await refresh();
  }, [recoverSubmission, refresh]);

  useEffect(() => {
    if (
      phase !== "joined" ||
      submission === null ||
      submission.status !== "unknown" ||
      submission.roundId !== projection?.current_round?.round_id ||
      checkedPendingIdsRef.current.has(submission.clientAnswerId)
    ) {
      return;
    }
    checkedPendingIdsRef.current.add(submission.clientAnswerId);
    void verifySubmission(submission);
  }, [phase, projection?.current_round?.round_id, submission, verifySubmission]);

  if (phase !== "joined" || projection === null) {
    return (
      <ParticipantEntryView
        isJoinDisabled={participantJoin.isJoinDisabled}
        isJoining={participantJoin.isJoining}
        onJoin={participantJoin.onJoin}
        onRetrySessionCheck={retryParticipantSessionCheck}
        pageError={pageError}
        phase={phase === "checking" ? "checking" : "join"}
        serverClock={serverClock}
      />
    );
  }

  return (
    <ParticipantJoinedView
      isStateFresh={isStateFresh}
      onSendAnswer={sendAnswer}
      onVerifySubmission={verifySubmission}
      pageError={pageError}
      projection={projection}
      serverClock={serverClock}
      submission={submission}
    />
  );
}
