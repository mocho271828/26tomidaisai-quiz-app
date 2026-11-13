import { useCallback, useEffect, useRef, useState } from "react";

import { ApiResponseError, errorMessage } from "../../api/client";
import {
  advanceRound,
  completeEvent,
  getOperatorSession,
  getOperatorState,
  loginOperator,
  logoutOperator,
  mutateRound,
  retryRoundResults,
  selectRoundQuestion,
  setResultVisibility,
} from "../../api/operatorClient";
import type {
  OperatorSession,
  OperatorState,
  ResultVisibilityTarget,
} from "../../api/operatorContracts";
import type { RealtimeStateMessage } from "../../api/realtimeContracts";
import { appConfig } from "../../config";
import { chooseOperatorProjection } from "../_hooks/projectionOrder";
import { useSingleFlightRefresh } from "../_hooks/useSingleFlightRefresh";
import { useRealtimeRefresh } from "../_hooks/useRealtimeRefresh";
import { createClientUuid } from "../_utils/clientUuid";
import { OperatorDashboard } from "./OperatorDashboard";
import { OperatorLogin } from "../_components/OperatorLogin";
import "./operator.css";
import {
  canPublishRoundResults,
  operationLabel,
  type RoundOperation,
  visibilityTargetLabels,
} from "./operatorUi";
import { useOperatorRanking } from "./useOperatorRanking";
import { useOperatorFinalRound } from "./final-round/useOperatorFinalRound";
import { useOperatorAwardCeremony } from "./award-ceremony/useOperatorAwardCeremony";

type OperatorPhase = "checking" | "login" | "dashboard";

export function OperatorPage() {
  const [phase, setPhase] = useState<OperatorPhase>("checking");
  const [session, setSession] = useState<OperatorSession | null>(null);
  const [projection, setProjection] = useState<OperatorState | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [isBusy, setIsBusy] = useState(false);
  const [pageError, setPageError] = useState<string | null>(null);
  const [operationMessage, setOperationMessage] = useState<string | null>(null);
  const mutationInFlightRef = useRef(false);
  const mutationKeysRef = useRef(new Map<string, string>());
  const projectionRef = useRef<OperatorState | null>(null);
  const sessionGenerationRef = useRef(0);
  const operatorRanking = useOperatorRanking({ onError: setPageError });
  const { resetRanking, synchronizeRanking } = operatorRanking;

  const resetProjection = useCallback((): void => {
    sessionGenerationRef.current += 1;
    projectionRef.current = null;
    resetRanking();
    setProjection(null);
  }, [resetRanking]);

  const acceptProjection = useCallback((candidate: OperatorState): void => {
    const current = projectionRef.current;
    const accepted = chooseOperatorProjection(current, candidate);
    if (accepted !== current) {
      projectionRef.current = accepted;
      setProjection(accepted);
    }
    synchronizeRanking(accepted.latest_ranking_snapshot_version);
  }, [synchronizeRanking]);

  const fetchState = useCallback(async (signal: AbortSignal): Promise<boolean> => {
    if (document.visibilityState !== "visible") return false;
    const generation = sessionGenerationRef.current;
    try {
      const candidate = await getOperatorState(signal);
      if (signal.aborted || generation !== sessionGenerationRef.current) return false;
      acceptProjection(candidate);
      setPageError(null);
      return true;
    } catch (error: unknown) {
      if (signal.aborted || generation !== sessionGenerationRef.current) return false;
      if (error instanceof ApiResponseError && error.status === 401) {
        setSession(null);
        resetProjection();
        setPhase("login");
        return false;
      }
      setPageError(errorMessage(error));
      return false;
    }
  }, [acceptProjection, resetProjection]);

  const refresh = useSingleFlightRefresh(fetchState);
  const handleAuthenticationLost = useCallback((): void => {
    setSession(null);
    resetProjection();
    setPhase("login");
  }, [resetProjection]);
  const finalRound = useOperatorFinalRound({
    projection, mutationInFlightRef, mutationKeysRef, sessionGenerationRef, refresh,
    onAuthenticationLost: handleAuthenticationLost, onBusy: setIsBusy,
    onError: setPageError, onMessage: setOperationMessage,
  });
  const awardCeremony = useOperatorAwardCeremony({
    projection, projectionRef, mutationInFlightRef, mutationKeysRef, sessionGenerationRef, refresh,
    onAuthenticationLost: handleAuthenticationLost, onBusy: setIsBusy,
    onError: setPageError, onMessage: setOperationMessage,
  });

  const acceptCommonState = useCallback((message: RealtimeStateMessage): boolean => {
    if (message.type !== "operator.state") return false;
    const current = projectionRef.current;
    const accepted = chooseOperatorProjection(current, message.state);
    if (accepted !== message.state) {
      // 同じversionのcacheが直前のHTTPより古くても、適用せず購読成立だけを確認する。
      return current !== null && current.event_version === message.state.event_version &&
        current.current_round?.round_id === message.state.current_round?.round_id &&
        current.current_round?.version === message.state.current_round?.version &&
        current.current_round?.state_version === message.state.current_round?.state_version;
    }
    acceptProjection(message.state);
    setPageError(null);
    return true;
  }, [acceptProjection]);

  useEffect(() => {
    const controller = new AbortController();
    let isDisposed = false;
    const initialize = async (): Promise<void> => {
      let authenticated: OperatorSession;
      try {
        authenticated = await getOperatorSession(controller.signal);
      } catch (error: unknown) {
        if (
          isDisposed ||
          (error instanceof DOMException && error.name === "AbortError")
        ) {
          return;
        }
        setPageError(
          error instanceof ApiResponseError && error.status === 401
            ? null
            : errorMessage(error),
        );
        setPhase("login");
        return;
      }
      if (isDisposed) {
        return;
      }
      setSession(authenticated);
      setPhase("dashboard");
      await refresh();
    };
    void initialize();
    return () => {
      isDisposed = true;
      controller.abort();
      projectionRef.current = null;
    };
  }, [refresh]);

  useEffect(() => {
    if (phase !== "dashboard") return;
    const onVisibilityChange = (): void => {
      // 初回取得をhiddenで保留した場合だけ、WS購読前の復帰をここで取得する。
      if (document.visibilityState === "visible" && projectionRef.current === null) void refresh();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => document.removeEventListener("visibilitychange", onVisibilityChange);
  }, [phase, refresh]);

  useRealtimeRefresh({
    audience: "operator",
    enabled: phase === "dashboard" && projection !== null,
    eventId: projection?.event_id ?? null,
    path: `${appConfig.VITE_WS_BASE_URL}/operator/realtime`,
    refresh,
    onState: acceptCommonState,
    refreshOnConnect: false,
    onAuthenticationLost: handleAuthenticationLost,
  });

  const handleLogin = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (mutationInFlightRef.current) {
      return;
    }
    mutationInFlightRef.current = true;
    setIsBusy(true);
    setPageError(null);
    try {
      const authenticated = await loginOperator({ username, password });
      setSession(authenticated);
      setPassword("");
      setPhase("dashboard");
      resetProjection();
      await refresh();
    } catch (error: unknown) {
      setPageError(errorMessage(error));
    } finally {
      mutationInFlightRef.current = false;
      setIsBusy(false);
    }
  };

  const handleLogout = async (): Promise<void> => {
    if (mutationInFlightRef.current) {
      return;
    }
    mutationInFlightRef.current = true;
    setIsBusy(true);
    try {
      await logoutOperator();
      setSession(null);
      resetProjection();
      setPhase("login");
      setOperationMessage(null);
    } catch (error: unknown) {
      setPageError(errorMessage(error));
    } finally {
      mutationInFlightRef.current = false;
      setIsBusy(false);
    }
  };

  const runRoundOperation = async (
    operation: RoundOperation,
  ): Promise<void> => {
    const currentProjection = projectionRef.current;
    const round = currentProjection?.current_round;
    if (currentProjection === null || round === null || round === undefined ||
      (operation === "publish-results" && !canPublishRoundResults(currentProjection))) {
      return;
    }
    if (mutationInFlightRef.current) {
      return;
    }
    mutationInFlightRef.current = true;
    setIsBusy(true);
    setPageError(null);
    const label = operationLabel(operation, currentProjection.start_delay_seconds);
    setOperationMessage(`${label}を実行しています…`);
    const logicalOperation = [
      operation,
      round.round_id,
      round.version,
    ].join(":");
    const idempotencyKey =
      mutationKeysRef.current.get(logicalOperation) ?? createClientUuid();
    mutationKeysRef.current.set(logicalOperation, idempotencyKey);
    try {
      const result = await mutateRound(round.round_id, operation, {
        expected_version: round.version,
        idempotency_key: idempotencyKey,
      });
      mutationKeysRef.current.delete(logicalOperation);
      setOperationMessage(
        `${label}が完了しました${
          result.is_replay ? "（同一操作の再確認）" : ""
        }。`,
      );
      await refresh();
    } catch (error: unknown) {
      if (error instanceof ApiResponseError && error.status < 500) {
        mutationKeysRef.current.delete(logicalOperation);
      }
      const mutationError = errorMessage(error);
      setOperationMessage(null);
      await refresh();
      setPageError(mutationError);
    } finally {
      mutationInFlightRef.current = false;
      setIsBusy(false);
    }
  };

  const retryAutomaticResults = async (): Promise<void> => {
    const round = projection?.current_round;
    if (
      round === null ||
      round === undefined ||
      round.state !== "VoidCandidate" ||
      round.result_publication !== "Withheld" ||
      mutationInFlightRef.current
    ) {
      return;
    }

    mutationInFlightRef.current = true;
    setIsBusy(true);
    setPageError(null);
    setOperationMessage("結果の自動集計を再試行しています…");
    const logicalOperation = [
      "retry-round-results",
      round.round_id,
      round.version,
    ].join(":");
    const idempotencyKey =
      mutationKeysRef.current.get(logicalOperation) ?? createClientUuid();
    mutationKeysRef.current.set(logicalOperation, idempotencyKey);
    try {
      const result = await retryRoundResults(round.round_id, {
        expected_version: round.version,
        idempotency_key: idempotencyKey,
      });
      mutationKeysRef.current.delete(logicalOperation);
      setOperationMessage(
        `結果の自動集計を再試行しました${
          result.is_replay ? "（同一操作の再確認）" : ""
        }。`,
      );
      await refresh();
    } catch (error: unknown) {
      if (error instanceof ApiResponseError && error.status < 500) {
        mutationKeysRef.current.delete(logicalOperation);
      }
      const mutationError = errorMessage(error);
      setOperationMessage(null);
      await refresh();
      setPageError(mutationError);
    } finally {
      mutationInFlightRef.current = false;
      setIsBusy(false);
    }
  };

  const changeRoundQuestion = async (questionId: string): Promise<void> => {
    const round = projection?.current_round;
    const selectedQuestion = projection?.available_questions.find(
      (question) => question.question_id === questionId,
    );
    if (
      projection === null ||
      round === null ||
      round === undefined ||
      selectedQuestion === undefined ||
      round.question.question_id === questionId ||
      projection.used_question_ids.includes(questionId) ||
      round.state !== "PreparingAssets" ||
      mutationInFlightRef.current
    ) {
      return;
    }

    mutationInFlightRef.current = true;
    setIsBusy(true);
    setPageError(null);
    setOperationMessage(`登録問題 ${selectedQuestion.display_order}へ切り替えています…`);
    const logicalOperation = [
      "select-round-question",
      round.round_id,
      round.version,
      questionId,
    ].join(":");
    const idempotencyKey =
      mutationKeysRef.current.get(logicalOperation) ?? createClientUuid();
    mutationKeysRef.current.set(logicalOperation, idempotencyKey);
    try {
      const result = await selectRoundQuestion(round.round_id, questionId, {
        expected_version: round.version,
        idempotency_key: idempotencyKey,
      });
      mutationKeysRef.current.delete(logicalOperation);
      setOperationMessage(
        `登録問題 ${selectedQuestion.display_order}へ切り替えました${
          result.is_replay ? "（同一操作の再確認）" : ""
        }。`,
      );
      await refresh();
    } catch (error: unknown) {
      if (error instanceof ApiResponseError && error.status < 500) {
        mutationKeysRef.current.delete(logicalOperation);
      }
      const mutationError = errorMessage(error);
      setOperationMessage(null);
      await refresh();
      setPageError(mutationError);
    } finally {
      mutationInFlightRef.current = false;
      setIsBusy(false);
    }
  };

  const advanceToNextRound = async (questionId: string): Promise<void> => {
    const round = projection?.current_round;
    const selectedQuestion = projection?.available_questions.find(
      (question) => question.question_id === questionId,
    );
    const canAdvanceFromCurrentState =
      round?.state === "Reveal" || round?.state === "RoundBackupFailed";
    if (
      projection === null ||
      projection.event_state === "Completed" ||
      projection.event_state === "Archived" ||
      round === null ||
      round === undefined ||
      selectedQuestion === undefined ||
      round.question.question_id === questionId ||
      projection.used_question_ids.includes(questionId) ||
      !canAdvanceFromCurrentState ||
      mutationInFlightRef.current
    ) {
      return;
    }

    mutationInFlightRef.current = true;
    setIsBusy(true);
    setPageError(null);
    setOperationMessage(
      `backupを検証して登録問題 ${selectedQuestion.display_order}へ進んでいます…`,
    );
    const logicalOperation = [
      "advance-round",
      round.round_id,
      round.version,
      questionId,
    ].join(":");
    const idempotencyKey =
      mutationKeysRef.current.get(logicalOperation) ?? createClientUuid();
    mutationKeysRef.current.set(logicalOperation, idempotencyKey);
    try {
      const result = await advanceRound(round.round_id, questionId, {
        expected_version: round.version,
        idempotency_key: idempotencyKey,
      });
      mutationKeysRef.current.delete(logicalOperation);
      setOperationMessage(
        `登録問題 ${selectedQuestion.display_order}のRoundを作成しました${
          result.is_replay ? "（同一操作の再確認）" : ""
        }。`,
      );
      await refresh();
    } catch (error: unknown) {
      if (error instanceof ApiResponseError && error.status < 500) {
        mutationKeysRef.current.delete(logicalOperation);
      }
      const mutationError = errorMessage(error);
      setOperationMessage(null);
      await refresh();
      setPageError(mutationError);
    } finally {
      mutationInFlightRef.current = false;
      setIsBusy(false);
    }
  };

  const finishEvent = async (): Promise<void> => {
    const round = projection?.current_round;
    const canCompleteFromCurrentState =
      round?.state === "Reveal" || round?.state === "RoundBackupFailed";
    if (
      projection === null ||
      projection.event_state === "Completed" ||
      projection.event_state === "Archived" ||
      round === null ||
      round === undefined ||
      !canCompleteFromCurrentState ||
      mutationInFlightRef.current
    ) {
      return;
    }

    mutationInFlightRef.current = true;
    setIsBusy(true);
    setPageError(null);
    setOperationMessage("現在のRoundのbackupを検証してEventを完了しています…");
    const logicalOperation = [
      "complete-event",
      round.round_id,
      round.version,
    ].join(":");
    const idempotencyKey =
      mutationKeysRef.current.get(logicalOperation) ?? createClientUuid();
    mutationKeysRef.current.set(logicalOperation, idempotencyKey);
    try {
      const result = await completeEvent(round.round_id, {
        expected_version: round.version,
        idempotency_key: idempotencyKey,
      });
      mutationKeysRef.current.delete(logicalOperation);
      setOperationMessage(
        `Eventを完了しました${
          result.is_replay ? "（同一操作の再確認）" : ""
        }。`,
      );
      await refresh();
    } catch (error: unknown) {
      if (error instanceof ApiResponseError && error.status < 500) {
        mutationKeysRef.current.delete(logicalOperation);
      }
      const mutationError = errorMessage(error);
      setOperationMessage(null);
      await refresh();
      setPageError(mutationError);
    } finally {
      mutationInFlightRef.current = false;
      setIsBusy(false);
    }
  };

  const changeResultVisibility = async (
    target: ResultVisibilityTarget,
    visible: boolean,
  ): Promise<void> => {
    if (projection === null || mutationInFlightRef.current) {
      return;
    }
    mutationInFlightRef.current = true;
    setIsBusy(true);
    setPageError(null);
    const logicalOperation = [
      "result-visibility",
      projection.event_id,
      projection.event_version,
      target,
      visible,
    ].join(":");
    const idempotencyKey =
      mutationKeysRef.current.get(logicalOperation) ?? createClientUuid();
    mutationKeysRef.current.set(logicalOperation, idempotencyKey);
    try {
      await setResultVisibility(target, visible, {
        expected_version: projection.event_version,
        idempotency_key: idempotencyKey,
      });
      mutationKeysRef.current.delete(logicalOperation);
      setOperationMessage(
        `${visibilityTargetLabels[target]}を${visible ? "表示" : "非表示"}にしました。`,
      );
      await refresh();
    } catch (error: unknown) {
      if (error instanceof ApiResponseError && error.status < 500) {
        mutationKeysRef.current.delete(logicalOperation);
      }
      const mutationError = errorMessage(error);
      await refresh();
      setPageError(mutationError);
    } finally {
      mutationInFlightRef.current = false;
      setIsBusy(false);
    }
  };

  if (phase === "checking" || phase === "login" || session === null) {
    return (
      <OperatorLogin
        isChecking={phase === "checking"}
        isBusy={isBusy}
        pageError={pageError}
        username={username}
        password={password}
        onUsernameChange={setUsername}
        onPasswordChange={setPassword}
        onSubmit={handleLogin}
      />
    );
  }

  return (
    <OperatorDashboard
      awardCeremony={awardCeremony}
      finalRound={finalRound}
      isBusy={isBusy}
      onAdvanceRound={advanceToNextRound}
      onCompleteEvent={finishEvent}
      onLogout={handleLogout}
      onQuestionChange={changeRoundQuestion}
      onResultVisibility={changeResultVisibility}
      onRetryResults={retryAutomaticResults}
      onRoundOperation={runRoundOperation}
      operationMessage={operationMessage}
      pageError={pageError}
      projection={projection}
      rankingState={operatorRanking}
      session={session}
    />
  );
}
