import { useEffect, useRef, useState, type RefObject } from "react";

import { ApiResponseError, errorMessage } from "../../../api/client";
import { setAwardCeremony, type AwardCeremonyTarget } from "../../../api/operatorAwardCeremonyClient";
import type { OperatorState } from "../../../api/operatorContracts";
import { createClientUuid } from "../../_utils/clientUuid";

type AwardCeremonyContext = {
  projection: OperatorState | null;
  projectionRef: RefObject<OperatorState | null>;
  mutationInFlightRef: RefObject<boolean>;
  mutationKeysRef: RefObject<Map<string, string>>;
  sessionGenerationRef: RefObject<number>;
  refresh: () => Promise<boolean>;
  onAuthenticationLost: () => void;
  onBusy: (value: boolean) => void;
  onError: (value: string | null) => void;
  onMessage: (value: string | null) => void;
};

export type OperatorAwardCeremonyControl = {
  isSynchronizing: boolean;
  retry: () => Promise<boolean>;
  onChange: (phase: AwardCeremonyTarget) => Promise<void>;
};

export function useOperatorAwardCeremony(context: AwardCeremonyContext): OperatorAwardCeremonyControl {
  const active = useRef(true);
  const [committed, setCommitted] = useState<{ eventId: string; version: number; generation: number } | null>(null);
  const { projection } = context;
  const isSynchronizing = committed !== null && committed.eventId === projection?.event_id &&
    committed.generation === context.sessionGenerationRef.current && projection.event_version < committed.version;

  useEffect(() => {
    active.current = true;
    return () => { active.current = false; };
  }, []);

  const onChange = async (phase: AwardCeremonyTarget): Promise<void> => {
    const current = context.projectionRef.current;
    if (current === null || current.event_state !== "Completed" || isSynchronizing ||
      context.mutationInFlightRef.current) return;
    if ((phase === "Announced" && current.award_ceremony_phase !== "Waiting") ||
      (phase === "Finished" && current.award_ceremony_phase !== "Announced")) return;

    const generation = context.sessionGenerationRef.current;
    const isCurrent = (): boolean => active.current && generation === context.sessionGenerationRef.current;
    const logicalOperation = ["award-ceremony", current.event_id, current.event_version, phase].join(":");
    const idempotencyKey = context.mutationKeysRef.current.get(logicalOperation) ?? createClientUuid();
    context.mutationKeysRef.current.set(logicalOperation, idempotencyKey);
    context.mutationInFlightRef.current = true;
    context.onBusy(true);
    context.onError(null);
    context.onMessage(null);
    try {
      const result = await setAwardCeremony(phase, {
        expected_version: current.event_version, idempotency_key: idempotencyKey,
      }, AbortSignal.timeout(5000));
      if (!isCurrent()) return;
      context.mutationKeysRef.current.delete(logicalOperation);
      // 更新後の版を取得するまでは、古い表示から次の操作を受け付けない。
      setCommitted({ eventId: current.event_id, version: result.event_version, generation });
      context.onMessage(phase === "Announced" ? "表彰対象者への呼び出しと最終ランキングを公開しました。" : "アンケート・ホームページへの案内を表示しました。");
      await context.refresh();
    } catch (cause: unknown) {
      if (!isCurrent()) return;
      if (cause instanceof ApiResponseError && cause.status < 500) context.mutationKeysRef.current.delete(logicalOperation);
      if (cause instanceof ApiResponseError && cause.status === 401) { context.onAuthenticationLost(); return; }
      const message = errorMessage(cause);
      await context.refresh();
      if (isCurrent()) context.onError(message);
    } finally {
      context.mutationInFlightRef.current = false;
      if (active.current) context.onBusy(false);
    }
  };

  return { isSynchronizing, retry: context.refresh, onChange };
}
