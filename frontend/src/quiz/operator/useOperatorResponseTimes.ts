import { useEffect, useState } from "react";

import { getOperatorResponseTimes } from "../../api/operatorResponseTimeClient";
import type { OperatorResponseTimes } from "../../api/operatorResponseTimeContracts";

type ResponseTimesLoad =
  | { key: string; status: "ready"; data: OperatorResponseTimes }
  | { key: string; status: "error"; data: null };

export function useOperatorResponseTimes(snapshotVersion: number | null) {
  const [roundId, selectRound] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [load, setLoad] = useState<ResponseTimesLoad | null>(null);
  const key = `${snapshotVersion}:${roundId}:${attempt}`;

  useEffect(() => {
    if (snapshotVersion === null) return;
    const controller = new AbortController();
    let started = false;
    const fetchWhenVisible = (): void => {
      if (started || document.visibilityState !== "visible") return;
      started = true;
      void getOperatorResponseTimes(roundId, controller.signal)
        .then((data) => {
          if (controller.signal.aborted) return;
          if (roundId !== null && data.selected_round_id !== roundId) {
            setLoad({ key, status: "error", data: null });
            return;
          }
          setLoad({ key, status: "ready", data });
        })
        .catch(() => {
          if (!controller.signal.aborted) setLoad({ key, status: "error", data: null });
        });
    };
    fetchWhenVisible();
    document.addEventListener("visibilitychange", fetchWhenVisible);
    return () => {
      controller.abort();
      document.removeEventListener("visibilitychange", fetchWhenVisible);
    };
  }, [snapshotVersion, roundId, key]);

  // 選択条件が変わったrenderから旧結果を隠し、遅延応答もabortで破棄する。
  const current = load?.key === key ? load : null;
  const status = snapshotVersion === null ? "idle" : current?.status ?? "loading";
  return {
    status,
    data: status === "ready" ? current?.data ?? null : null,
    roundId,
    selectRound,
    retry: (): void => setAttempt((value) => value + 1),
  };
}
