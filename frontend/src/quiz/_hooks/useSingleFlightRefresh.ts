import { useCallback, useEffect, useRef } from "react";
import { ApiCancellation } from "../../api/cancellation";

type Flight = {
  generation: string | undefined;
  pending: boolean;
  controller: AbortController;
  promise: Promise<boolean>;
};

// 運営操作後の更新は後続1件へ畳む。参加者は世代内で共有し、復帰前の
// 通信を待たず新しい世代を取得することで古い応答を最新確認に使わない。
export function useSingleFlightRefresh(
  fetchState: (signal: AbortSignal) => Promise<boolean>,
  getGeneration?: () => string,
): () => Promise<boolean> {
  const fetchRef = useRef(fetchState);
  const generationRef = useRef(getGeneration);
  const activeRef = useRef(true);
  const flightRef = useRef<Flight | null>(null);

  useEffect(() => {
    fetchRef.current = fetchState;
    generationRef.current = getGeneration;
  }, [fetchState, getGeneration]);

  useEffect(() => {
    activeRef.current = true;
    return () => {
      activeRef.current = false;
      if (flightRef.current !== null) {
        flightRef.current.pending = false;
        flightRef.current.controller.abort(new ApiCancellation("unmount"));
      }
    };
  }, []);

  return useCallback((): Promise<boolean> => {
    if (!activeRef.current) return Promise.resolve(false);
    const generation = generationRef.current?.();
    const previous = flightRef.current;
    if (previous !== null && !previous.controller.signal.aborted && previous.generation === generation) {
      if (generationRef.current === undefined) previous.pending = true;
      return previous.promise;
    }
    if (previous !== null) {
      previous.pending = false;
      previous.controller.abort(new ApiCancellation("superseded"));
    }
    const flight: Flight = { generation, pending: true,
      controller: new AbortController(), promise: Promise.resolve(false) };
    // 同期的throw/resolveでもflightの代入がfinallyより先になるようにする。
    flight.promise = Promise.resolve().then(async () => {
      let successful = false;
      try {
        while (activeRef.current && flightRef.current === flight && flight.pending) {
          flight.pending = false;
          successful = await fetchRef.current(flight.controller.signal);
        }
        return !flight.controller.signal.aborted && successful;
      } finally {
        // abortを無視する旧通信のfinallyで、新世代のflightを消さない。
        if (flightRef.current === flight) flightRef.current = null;
      }
    });
    flightRef.current = flight;
    return flight.promise;
  }, []);
}
