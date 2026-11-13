import { useCallback, useEffect, useRef, useState } from "react";

import { errorMessage } from "../../api/client";
import { getOperatorRanking } from "../../api/operatorClient";
import type { OperatorLatestRanking } from "../../api/operatorContracts";

type RankingTarget = {
  snapshotVersion: number | null;
};

type UseOperatorRankingOptions = {
  onError: (message: string) => void;
};

export type OperatorRankingState =
  | { status: "idle"; latestRanking: null }
  | { status: "loading"; latestRanking: null }
  | { status: "ready"; latestRanking: OperatorLatestRanking }
  | { status: "error"; latestRanking: null };

type UseOperatorRankingResult = OperatorRankingState & {
  resetRanking: () => void;
  synchronizeRanking: (snapshotVersion: number | null) => void;
};

const idleRankingState: OperatorRankingState = {
  status: "idle",
  latestRanking: null,
};
const loadingRankingState: OperatorRankingState = {
  status: "loading",
  latestRanking: null,
};
const errorRankingState: OperatorRankingState = {
  status: "error",
  latestRanking: null,
};

function isSameTarget(
  left: RankingTarget | null,
  right: RankingTarget,
): boolean {
  return (
    left?.snapshotVersion === right.snapshotVersion
  );
}

export function useOperatorRanking({
  onError,
}: UseOperatorRankingOptions): UseOperatorRankingResult {
  const [rankingState, setRankingState] =
    useState<OperatorRankingState>(idleRankingState);
  const activeTargetRef = useRef<RankingTarget | null>(null);
  const loadedOrPendingTargetRef = useRef<RankingTarget | null>(null);
  const requestTokenRef = useRef(0);

  const cancelPendingRanking = useCallback((): void => {
    requestTokenRef.current += 1;
    activeTargetRef.current = null;
    loadedOrPendingTargetRef.current = null;
  }, []);

  const resetRanking = useCallback((): void => {
    cancelPendingRanking();
    setRankingState(idleRankingState);
  }, [cancelPendingRanking]);

  const synchronizeRanking = useCallback(
    (snapshotVersion: number | null): void => {
      const target = { snapshotVersion };
      if (!isSameTarget(activeTargetRef.current, target)) {
        requestTokenRef.current += 1;
        activeTargetRef.current = target;
        loadedOrPendingTargetRef.current = null;
        setRankingState(
          snapshotVersion === null ? idleRankingState : loadingRankingState,
        );
      }
      if (
        snapshotVersion === null ||
        document.visibilityState !== "visible" ||
        isSameTarget(loadedOrPendingTargetRef.current, target)
      ) {
        return;
      }

      const requestToken = requestTokenRef.current + 1;
      requestTokenRef.current = requestToken;
      loadedOrPendingTargetRef.current = target;
      setRankingState(loadingRankingState);
      void getOperatorRanking()
        .then((candidate) => {
          if (
            requestTokenRef.current !== requestToken ||
            !isSameTarget(activeTargetRef.current, target)
          ) {
            return;
          }
          if (
            candidate === null ||
            candidate.snapshot_version !== snapshotVersion
          ) {
            loadedOrPendingTargetRef.current = null;
            setRankingState(errorRankingState);
            onError("ランキング情報を取得できませんでした。");
            return;
          }
          setRankingState({ status: "ready", latestRanking: candidate });
        })
        .catch((error: unknown) => {
          if (
            requestTokenRef.current === requestToken &&
            isSameTarget(activeTargetRef.current, target)
          ) {
            loadedOrPendingTargetRef.current = null;
            setRankingState(errorRankingState);
            onError(errorMessage(error));
          }
        });
    },
    [onError],
  );

  useEffect(() => cancelPendingRanking, [cancelPendingRanking]);

  return { ...rankingState, resetRanking, synchronizeRanking };
}
