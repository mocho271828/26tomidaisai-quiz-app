import { useEffect, useState } from "react";

import { ApiResponseError } from "../../api/client";
import { getOperatorRanking } from "../../api/operatorClient";
import type { OperatorLatestRanking, OperatorState } from "../../api/operatorContracts";
import { getOperatorResponseTimes } from "../../api/operatorResponseTimeClient";
import type { OperatorResponseTimes } from "../../api/operatorResponseTimeContracts";

type ModeratorDetails = {
  responseTimes: OperatorResponseTimes;
  ranking: OperatorLatestRanking;
};
type DetailsLoad = { key: string; data: ModeratorDetails | null; status: "ready" | "error" };

export function useModeratorDetails(projection: OperatorState | null, onAuthenticationLost: () => void) {
  const [load, setLoad] = useState<DetailsLoad | null>(null);
  const [attempt, setAttempt] = useState(0);
  const eventId = projection?.event_id ?? null;
  const snapshotVersion = projection?.latest_ranking_snapshot_version ?? null;
  // 問題の確定・開始・公開設定変更では直近の確定結果は変わらない。
  const key = [eventId, snapshotVersion, attempt].join(":");

  useEffect(() => {
    if (eventId === null || snapshotVersion === null) return;
    const controller = new AbortController();
    let started = false;
    const fetchWhenVisible = (): void => {
      if (started || document.visibilityState !== "visible") return;
      started = true;
      const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(5000)]);
      // 順位snapshotの問題を使うことで、次問準備中や再読込時も同じ確定結果を取得する。
      void getOperatorRanking(signal).then(async (ranking) => {
        if (controller.signal.aborted) return;
        if (ranking === null || ranking.snapshot_version !== snapshotVersion) {
          setLoad({ key, status: "error", data: null });
          return;
        }
        const responseTimes = await getOperatorResponseTimes(ranking.round_id, signal);
        if (controller.signal.aborted) return;
        if (responseTimes.selected_round_id !== ranking.round_id) {
          setLoad({ key, status: "error", data: null });
          return;
        }
        setLoad({ key, status: "ready", data: { responseTimes, ranking } });
      }).catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        if (cause instanceof ApiResponseError && cause.status === 401) onAuthenticationLost();
        else setLoad({ key, status: "error", data: null });
      });
    };
    fetchWhenVisible();
    document.addEventListener("visibilitychange", fetchWhenVisible);
    return () => { controller.abort(); document.removeEventListener("visibilitychange", fetchWhenVisible); };
  }, [eventId, snapshotVersion, key, onAuthenticationLost]);

  // 開催変更・reset・新しい集計版では、旧結果を今回の確定結果として使わない。
  const current = load?.key === key ? load : null;
  return { data: current?.data ?? null, status: eventId === null || snapshotVersion === null ? "idle" : current?.status ?? "loading",
    retry: (): void => setAttempt((value) => value + 1) };
}
