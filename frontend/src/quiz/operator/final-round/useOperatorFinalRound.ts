import { useEffect, useRef, useState } from "react";
import type { RefObject } from "react";

import { ApiResponseError, errorMessage } from "../../../api/client";
import type { OperatorState } from "../../../api/operatorContracts";
import { getOperatorFinalRound, setOperatorFinalRound, type OperatorFinalRound } from "../../../api/operatorFinalRoundClient";
import { createClientUuid } from "../../_utils/clientUuid";

type FinalRoundContext = {
  projection: OperatorState | null;
  mutationInFlightRef: RefObject<boolean>;
  mutationKeysRef: RefObject<Map<string, string>>;
  sessionGenerationRef: RefObject<number>;
  refresh: () => Promise<boolean>;
  onAuthenticationLost: () => void;
  onBusy: (value: boolean) => void;
  onError: (value: string | null) => void;
  onMessage: (value: string | null) => void;
};

export type OperatorFinalRoundControl = {
  value: OperatorFinalRound | null;
  status: "idle" | "loading" | "ready" | "error";
  retry: () => void;
  onChange: (isFinal: boolean) => Promise<void>;
};

type Load = { key: string; value: OperatorFinalRound | null; status: "ready" | "error" };

export function useOperatorFinalRound(context: FinalRoundContext): OperatorFinalRoundControl {
  const { projection, onAuthenticationLost } = context;
  const eventId = projection?.event_id ?? null;
  const eventVersion = projection?.event_version ?? null;
  const [attempt, setAttempt] = useState(0);
  const [load, setLoad] = useState<Load | null>(null);
  const active = useRef(true);
  const key = `${eventId}:${eventVersion}:${attempt}`;

  useEffect(() => {
    active.current = true;
    return () => { active.current = false; };
  }, []);

  useEffect(() => {
    if (eventId === null) return;
    const controller = new AbortController();
    let started = false;
    const fetchWhenVisible = (): void => {
      if (started || document.visibilityState !== "visible") return;
      started = true;
      void getOperatorFinalRound(AbortSignal.any([controller.signal, AbortSignal.timeout(5000)]))
        .then((value) => {
          if (controller.signal.aborted) return;
          const matches = value.event_id === eventId && value.event_version === eventVersion;
          setLoad({ key, value: matches ? value : null, status: matches ? "ready" : "error" });
        }).catch((cause: unknown) => {
          if (controller.signal.aborted) return;
          if (cause instanceof ApiResponseError && cause.status === 401) onAuthenticationLost();
          else setLoad({ key, value: null, status: "error" });
        });
    };
    fetchWhenVisible();
    document.addEventListener("visibilitychange", fetchWhenVisible);
    return () => { controller.abort(); document.removeEventListener("visibilitychange", fetchWhenVisible); };
  }, [eventId, eventVersion, key, onAuthenticationLost]);

  const current = load?.key === key ? load : null;
  const retry = (): void => setAttempt((value) => value + 1);
  const onChange = async (isFinal: boolean): Promise<void> => {
    const round = projection?.current_round;
    if (projection === null || projection.event_state !== "InProgress" || round == null || round.state === "Voided" ||
        current?.status !== "ready" || current.value === null ||
        context.mutationInFlightRef.current) return;
    if (!isFinal && current.value.final_round_id !== round.round_id) return;

    const generation = context.sessionGenerationRef.current;
    context.mutationInFlightRef.current = true;
    context.onBusy(true);
    context.onError(null);
    context.onMessage(null);
    const logicalOperation = ["final-round", projection.event_id, projection.event_version,
      round.round_id, isFinal].join(":");
    const idempotencyKey = context.mutationKeysRef.current.get(logicalOperation) ?? createClientUuid();
    context.mutationKeysRef.current.set(logicalOperation, idempotencyKey);
    try {
      const result = await setOperatorFinalRound(round.round_id, isFinal, {
        expected_version: projection.event_version, idempotency_key: idempotencyKey,
      }, AbortSignal.timeout(5000));
      if (!active.current || generation !== context.sessionGenerationRef.current) return;
      context.mutationKeysRef.current.delete(logicalOperation);
      setLoad(null);
      context.onMessage(isFinal ? "この問題を最終問題に設定しました。" : "最終問題の指定を解除しました。");
      const refreshed = await context.refresh();
      if (!refreshed || result.event_version === projection.event_version) retry();
    } catch (cause: unknown) {
      if (!active.current || generation !== context.sessionGenerationRef.current) return;
      if (cause instanceof ApiResponseError && cause.status < 500) context.mutationKeysRef.current.delete(logicalOperation);
      if (cause instanceof ApiResponseError && cause.status === 401) { onAuthenticationLost(); return; }
      const message = errorMessage(cause);
      await context.refresh();
      context.onError(message);
    } finally {
      context.mutationInFlightRef.current = false;
      if (active.current) context.onBusy(false);
    }
  };

  return { value: current?.value ?? null, status: eventId === null ? "idle" : current?.status ?? "loading", retry, onChange };
}
