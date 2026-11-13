import { useCallback, useEffect, useRef, useState } from "react";

import { ApiResponseError } from "../../api/client";
import { getOperatorState } from "../../api/operatorClient";
import type { OperatorState } from "../../api/operatorContracts";
import type { RealtimeStateMessage } from "../../api/realtimeContracts";
import { appConfig } from "../../config";
import { chooseOperatorProjection } from "../_hooks/projectionOrder";
import { useRealtimeRefresh } from "../_hooks/useRealtimeRefresh";
import { useSingleFlightRefresh } from "../_hooks/useSingleFlightRefresh";

export function useModeratorState(onAuthenticationLost: () => void) {
  const [projection, setProjection] = useState<OperatorState | null>(null);
  const [isFresh, setIsFresh] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const current = useRef<OperatorState | null>(null);
  const visibilityGeneration = useRef(0);

  const accept = useCallback((candidate: OperatorState): boolean => {
    const accepted = chooseOperatorProjection(current.current, candidate);
    const isSameRevision = accepted.event_id === candidate.event_id &&
      accepted.event_version === candidate.event_version &&
      accepted.current_round?.round_id === candidate.current_round?.round_id &&
      accepted.current_round?.state_version === candidate.current_round?.state_version;
    if (!isSameRevision) return false;
    current.current = accepted;
    setProjection(accepted);
    return true;
  }, []);
  const fetchState = useCallback(async (signal: AbortSignal): Promise<boolean> => {
    if (document.visibilityState !== "visible") return false;
    const generation = visibilityGeneration.current;
    try {
      const candidate = await getOperatorState(AbortSignal.any([signal, AbortSignal.timeout(5000)]));
      if (signal.aborted || generation !== visibilityGeneration.current) return false;
      if (!accept(candidate)) return false;
      setIsFresh(true);
      setError(null);
      return true;
    } catch (cause: unknown) {
      if (signal.aborted || generation !== visibilityGeneration.current) return false;
      setIsFresh(false);
      if (cause instanceof ApiResponseError && cause.status === 401) onAuthenticationLost();
      else setError("情報を更新できませんでした。もう一度読み込んでください。");
      return false;
    }
  }, [accept, onAuthenticationLost]);
  const refresh = useSingleFlightRefresh(fetchState, () => String(visibilityGeneration.current));

  useEffect(() => {
    void refresh();
    const onVisibilityChange = (): void => {
      visibilityGeneration.current += 1;
      setIsFresh(false);
      if (document.visibilityState === "visible" && current.current === null) void refresh();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => document.removeEventListener("visibilitychange", onVisibilityChange);
  }, [refresh]);

  const onState = useCallback((message: RealtimeStateMessage, refreshScheduled = false): boolean => {
    if (message.type !== "operator.state") return false;
    // 同じ開催の古いcacheは破棄する。購読自体を失敗扱いにしてGETを増やさない。
    if (!accept(message.state)) return false;
    if (!refreshScheduled) { setIsFresh(true); setError(null); }
    return true;
  }, [accept]);

  useRealtimeRefresh({
    audience: "operator", enabled: projection !== null, eventId: projection?.event_id ?? null,
    path: `${appConfig.VITE_WS_BASE_URL}/operator/realtime`, refresh, onState,
    refreshOnConnect: false, onAuthenticationLost,
    onUnavailable: () => { setIsFresh(false); },
  });
  return { projection, isFresh, error, refresh };
}
