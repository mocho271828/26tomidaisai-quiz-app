import { useEffect, useRef, useState } from "react";

import {
  realtimeServerMessageSchema,
  type RealtimeAudience,
  type RealtimeStateMessage,
} from "../../api/realtimeContracts";
import type { PersonalResultDelivery, PersonalResultResponse } from "../../api/contracts";
import { observeBrowser } from "../_utils/browserObservation";

const RECONNECT_BASE_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;
const STABLE_CONNECTION_MS = 10_000;
const TRANSPORT_TIMEOUT_MS = 7_500;
const INITIAL_STATE_TIMEOUT_MS = 6_000;
const POLLING_INTERVAL_MS = 5_000;
const POLLING_JITTER_MS = 1_000;

export type RealtimeStatus = "connecting" | "live" | "polling";

export type RealtimeRefreshOptions = {
  audience: RealtimeAudience;
  enabled: boolean;
  eventId: string | null;
  path: string;
  refresh: () => Promise<boolean>;
  onState: (message: RealtimeStateMessage, refreshScheduled?: boolean) => boolean;
  onPersonalResult?: (response: PersonalResultResponse) => void;
  onPersonalResultDelivery?: (delivery: PersonalResultDelivery) => void;
  refreshOnConnect?: boolean;
  onUnavailable?: () => void;
  onAuthenticationLost?: () => void;
};

function websocketUrl(path: string): string {
  const url = new URL(path, window.location.origin);
  url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

export function useRealtimeRefresh(options: RealtimeRefreshOptions): RealtimeStatus {
  const { audience, enabled, eventId, path } = options;
  const optionsRef = useRef(options);
  const [status, setStatus] = useState<RealtimeStatus>("connecting");

  useEffect(() => { optionsRef.current = options; });

  useEffect(() => {
    if (!enabled || eventId === null) {
      setStatus("connecting");
      return;
    }
    let disposed = false;
    let authenticationLost = false;
    let socket: WebSocket | null = null;
    let pollingTimer: number | undefined;
    let reconnectTimer: number | undefined;
    let transportTimer: number | undefined;
    let initialStateTimer: number | undefined;
    let stableTimer: number | undefined;
    let reconnectAttempt = 0;
    let streamId: string | null = null;
    let sequence = 0;
    let latestState: RealtimeStateMessage | null = null;
    let latestResult: PersonalResultResponse | null = null;
    let validatedSocket: WebSocket | null = null;
    type RefreshFlight = { promise: Promise<boolean>; generation: number; dirty: boolean };
    let refreshFlight: RefreshFlight | null = null;
    let pollingRequired = false;
    let participantRefreshRequired = false;
    let visibilityGeneration = 0;
    let visibility = document.visibilityState;

    const stopPolling = (): void => {
      window.clearTimeout(pollingTimer);
      pollingTimer = undefined;
    };
    const stopTransportWait = (): void => {
      window.clearTimeout(transportTimer);
      transportTimer = undefined;
    };
    const stopInitialStateWait = (): void => {
      window.clearTimeout(initialStateTimer);
      initialStateTimer = undefined;
    };

    const markLive = (candidate: WebSocket): void => {
      if (disposed || authenticationLost || document.visibilityState !== "visible" ||
        participantRefreshRequired || socket !== candidate || validatedSocket !== candidate ||
        candidate.readyState !== WebSocket.OPEN) return;
      pollingRequired = false;
      stopPolling();
      setStatus("live");
      if (stableTimer !== undefined) return;
      stableTimer = window.setTimeout(() => {
        stableTimer = undefined;
        if (!disposed && socket === candidate && validatedSocket === candidate &&
          candidate.readyState === WebSocket.OPEN) {
          reconnectAttempt = 0;
          observeBrowser({ kind: "ws", audience, phase: "stable" });
        }
      }, STABLE_CONNECTION_MS);
    };

    const runRefresh = (changedSession = false): Promise<boolean> => {
      if (disposed || authenticationLost || document.visibilityState !== "visible") return Promise.resolve(false);
      if (audience === "participant") participantRefreshRequired = true;
      if (refreshFlight !== null && (audience !== "participant" || refreshFlight.generation === visibilityGeneration)) {
        // session.changedは本人属性の再確認が必要なので、進行中GET後の1件を残す。
        if (audience !== "participant" || changedSession) refreshFlight.dirty = true;
        return refreshFlight.promise;
      }
      stopPolling();
      const flight: RefreshFlight = { promise: Promise.resolve(false), generation: visibilityGeneration, dirty: false };
      refreshFlight = flight;
      flight.promise = (async () => {
        let successful = false;
        let candidate: WebSocket | null = null;
        let generation = visibilityGeneration;
        try {
          do {
            flight.dirty = false;
            candidate = socket;
            generation = visibilityGeneration;
            successful = await optionsRef.current.refresh();
          } while (refreshFlight === flight && !disposed && !authenticationLost &&
            document.visibilityState === "visible" && flight.dirty);
          if (refreshFlight !== flight || disposed || authenticationLost || document.visibilityState !== "visible" ||
            generation !== visibilityGeneration) return false;
          // 共通snapshotには本人属性・現在回答がないため、本人GETの失敗を相殺しない。
          if (successful) participantRefreshRequired = false;
          if (socket !== candidate && (!successful || audience !== "participant")) return false;
          if (successful && socket !== null) markLive(socket);
          if (!successful) startPolling(false, "http");
          return successful;
        } finally {
          // 復帰前の取得完了では、復帰後のflightや通信状態を変更しない。
          if (refreshFlight === flight) {
            refreshFlight = null;
            schedulePolling();
          }
        }
      })();
      return flight.promise;
    };

    const schedulePolling = (): void => {
      if (disposed || authenticationLost || !pollingRequired || document.visibilityState !== "visible" ||
        pollingTimer !== undefined || refreshFlight !== null) return;
      // 遅いGETの完了後にも待機を挟み、各端末の再取得が同時に集中するのを避ける。
      const delayMs = POLLING_INTERVAL_MS + Math.round(Math.random() * POLLING_JITTER_MS);
      pollingTimer = window.setTimeout(() => {
        pollingTimer = undefined;
        if (!disposed && !authenticationLost && pollingRequired && document.visibilityState === "visible") {
          void runRefresh();
        }
      }, delayMs);
    };

    const startPolling = (shouldRefresh = true, source: "realtime" | "http" = "realtime"): void => {
      if (disposed || authenticationLost) return;
      const wasPolling = pollingRequired;
      const needsParticipantCheck = audience === "participant" && source === "realtime" &&
        (!wasPolling || validatedSocket !== null);
      pollingRequired = true;
      if (audience === "participant") participantRefreshRequired = true;
      // 切断前の本人GETでは復旧を確認できない。同じ障害のerror/closeは後続1件へまとめる。
      if (needsParticipantCheck && refreshFlight !== null) refreshFlight.dirty = true;
      // HTTPの一時失敗は検証済みWSを否定しない。通信障害時だけ購読成立の証拠を破棄する。
      if (source === "realtime") validatedSocket = null;
      if (audience !== "participant" || source === "http" || needsParticipantCheck) {
        optionsRef.current.onUnavailable?.();
      }
      window.clearTimeout(stableTimer);
      stableTimer = undefined;
      setStatus("polling");
      if (document.visibilityState !== "visible") return;
      // errorに続くcloseなど、同じ縮退の重複通知で後続GETを増やさない。
      if (shouldRefresh && !wasPolling) void runRefresh();
      else schedulePolling();
    };

    const waitForTransportMessage = (candidate: WebSocket): void => {
      stopTransportWait();
      if (disposed || document.visibilityState !== "visible" || socket !== candidate ||
        candidate.readyState !== WebSocket.OPEN) return;
      transportTimer = window.setTimeout(() => {
        transportTimer = undefined;
        if (disposed || socket !== candidate) return;
        startPolling();
        candidate.close();
      }, TRANSPORT_TIMEOUT_MS);
    };

    const waitForInitialState = (candidate: WebSocket): void => {
      if (disposed || document.visibilityState !== "visible" || socket !== candidate ||
        candidate.readyState !== WebSocket.OPEN || latestState !== null ||
        initialStateTimer !== undefined) return;
      // DB取得待ち中のheartbeatは通信確認だけに使い、初回状態の期限を延長しない。
      initialStateTimer = window.setTimeout(() => {
        initialStateTimer = undefined;
        if (disposed || socket !== candidate || latestState !== null) return;
        startPolling();
        candidate.close();
      }, INITIAL_STATE_TIMEOUT_MS);
    };

    const scheduleReconnect = (): void => {
      if (disposed || authenticationLost || reconnectTimer !== undefined) return;
      const ceiling = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** Math.min(reconnectAttempt, 5));
      const delayMs = Math.round(ceiling * (0.5 + Math.random() / 2));
      reconnectAttempt += 1;
      observeBrowser({ kind: "ws", audience, phase: "retry", retry_ms: delayMs, attempt: reconnectAttempt });
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = undefined;
        connect();
      }, delayMs);
    };

    const connect = (): void => {
      if (disposed || authenticationLost || socket !== null || reconnectTimer !== undefined) return;
      streamId = null;
      sequence = 0;
      latestState = null;
      latestResult = null;
      validatedSocket = null;
      observeBrowser({ kind: "ws", audience, phase: "connecting" });
      const next = new WebSocket(websocketUrl(path));
      socket = next;
      setStatus(pollingRequired ? "polling" : "connecting");
      next.addEventListener("open", () => {
        if (disposed || socket !== next) return;
        observeBrowser({ kind: "ws", audience, phase: "open" });
        waitForTransportMessage(next);
        waitForInitialState(next);
      });
      next.addEventListener("message", (event: MessageEvent<unknown>) => {
        if (disposed || socket !== next) return;
        let decoded: unknown;
        try { decoded = JSON.parse(String(event.data)); }
        catch (error: unknown) {
          if (!(error instanceof SyntaxError)) throw error;
          startPolling();
          next.close();
          return;
        }
        const parsed = realtimeServerMessageSchema.safeParse(decoded);
        if (!parsed.success || parsed.data.type === "polling.required") {
          startPolling();
          next.close();
          return;
        }
        const message = parsed.data;
        if (message.audience !== audience || message.event_id !== eventId ||
          (streamId !== null && message.stream_id !== streamId)) {
          startPolling();
          next.close();
          return;
        }
        if (message.sequence <= sequence) return;
        streamId = message.stream_id;
        sequence = message.sequence;
        waitForTransportMessage(next);
        if (message.type === "heartbeat") {
          if (document.visibilityState === "visible" && message.personal_result_delivery !== undefined) {
            optionsRef.current.onPersonalResultDelivery?.(message.personal_result_delivery);
          }
          return;
        }
        if (message.type === "participant.result") {
          latestResult = message.result;
          if (latestState !== null && document.visibilityState === "visible") {
            optionsRef.current.onPersonalResult?.(message.result);
          }
          return;
        }
        if (message.type === "session.changed") {
          // hidden中のcontrolも復帰時の本人GETへ集約する。
          if (document.visibilityState === "visible") void runRefresh(true);
          return;
        }
        stopInitialStateWait();
        const isInitial = latestState === null;
        latestState = message;
        if (document.visibilityState !== "visible") return;
        const refreshScheduled = isInitial && optionsRef.current.refreshOnConnect !== false;
        if (!optionsRef.current.onState(message, refreshScheduled)) {
          startPolling();
          return;
        }
        validatedSocket = next;
        markLive(next);
        if (latestResult !== null) optionsRef.current.onPersonalResult?.(latestResult);
        if (refreshScheduled) void runRefresh();
      });
      next.addEventListener("error", () => {
        if (disposed || socket !== next) return;
        startPolling();
        next.close();
      });
      next.addEventListener("close", (event: CloseEvent) => {
        if (disposed || socket !== next) return;
        stopTransportWait();
        stopInitialStateWait();
        window.clearTimeout(stableTimer);
        stableTimer = undefined;
        socket = null;
        observeBrowser({ kind: "ws", audience, phase: "closed" });
        if (event.code === 4401 || event.code === 1008) {
          authenticationLost = true;
          stopPolling();
          optionsRef.current.onAuthenticationLost?.();
          return;
        }
        startPolling();
        scheduleReconnect();
      });
    };

    const onVisibilityChange = (): void => {
      if (visibility === document.visibilityState) return;
      visibility = document.visibilityState;
      visibilityGeneration += 1;
      if (document.visibilityState !== "visible") {
        stopPolling();
        stopTransportWait();
        stopInitialStateWait();
        window.clearTimeout(stableTimer);
        stableTimer = undefined;
        if (refreshFlight !== null) refreshFlight.dirty = false;
        return;
      }
      if (socket !== null && latestState !== null && optionsRef.current.onState(latestState, true)) {
        validatedSocket = socket;
        if (latestResult !== null) optionsRef.current.onPersonalResult?.(latestResult);
      }
      // 本人確認の成功と、現在のsocketのsnapshot検証を別々に確認する。
      void runRefresh();
      if (socket === null) connect();
      else {
        waitForTransportMessage(socket);
        waitForInitialState(socket);
      }
    };

    connect();
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      disposed = true;
      document.removeEventListener("visibilitychange", onVisibilityChange);
      stopPolling();
      stopTransportWait();
      stopInitialStateWait();
      window.clearTimeout(reconnectTimer);
      window.clearTimeout(stableTimer);
      socket?.close();
      socket = null;
    };
  }, [audience, enabled, eventId, path]);

  return status;
}
