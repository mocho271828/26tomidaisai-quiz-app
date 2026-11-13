import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { OperatorLatestRanking } from "../../api/operatorContracts";
import { useOperatorRanking } from "./useOperatorRanking";

const operatorApi = vi.hoisted(() => ({
  getOperatorRanking: vi.fn(),
}));

vi.mock("../../api/operatorClient", () => operatorApi);

const ids = {
  round: "60000000-0000-4000-8000-000000000002",
} as const;

function participantId(index: number): string {
  const first = Math.floor(index / (26 * 26)) % 26;
  const second = Math.floor(index / 26) % 26;
  const third = index % 26;
  return [first, second, third]
    .map((value) => String.fromCharCode("A".charCodeAt(0) + value))
    .join("");
}

function createRanking(
  snapshotVersion: number,
  rowCount = 1,
  labelOffset = 0,
): OperatorLatestRanking {
  return {
    snapshot_version: snapshotVersion,
    round_id: ids.round,
    rows: Array.from({ length: rowCount }, (_, index) => ({
      overall_rank: index + 1,
      public_label: participantId(labelOffset + index),
      total_points: rowCount - index,
      correct_count: 1,
    })),
  };
}

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
} {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((promiseResolve) => {
    resolve = promiseResolve;
  });
  return { promise, resolve };
}

describe("useOperatorRanking", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(() => vi.restoreAllMocks());

  it("hiddenで新snapshotを受けても取得せず、visibleで同じsnapshotを同期すると取得する", async () => {
    const onError = vi.fn();
    operatorApi.getOperatorRanking.mockResolvedValueOnce(createRanking(2));
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    const { result } = renderHook(() => useOperatorRanking({ onError }));
    act(() => {
      result.current.synchronizeRanking(1);
      result.current.synchronizeRanking(2);
    });
    expect(operatorApi.getOperatorRanking).not.toHaveBeenCalled();
    expect(result.current.status).toBe("loading");

    visibility.mockReturnValue("visible");
    await act(async () => result.current.synchronizeRanking(2));
    expect(operatorApi.getOperatorRanking).toHaveBeenCalledOnce();
    expect(result.current.latestRanking?.snapshot_version).toBe(2);
    expect(onError).not.toHaveBeenCalled();
  });

  it("500行でも同じsnapshotの1秒poll相当では再取得せず、version変更時だけ取得する", async () => {
    const onError = vi.fn();
    operatorApi.getOperatorRanking
      .mockResolvedValueOnce(createRanking(1, 500))
      .mockResolvedValueOnce(createRanking(2, 500));
    const { result } = renderHook(() => useOperatorRanking({ onError }));

    expect(result.current.status).toBe("idle");
    act(() => result.current.synchronizeRanking(1));
    expect(result.current.status).toBe("loading");
    await waitFor(() =>
      expect(result.current.status).toBe("ready"),
    );
    expect(result.current.latestRanking?.rows).toHaveLength(500);

    act(() => {
      result.current.synchronizeRanking(1);
      result.current.synchronizeRanking(1);
      result.current.synchronizeRanking(1);
    });
    expect(operatorApi.getOperatorRanking).toHaveBeenCalledOnce();
    expect(result.current.status).toBe("ready");

    act(() => result.current.synchronizeRanking(2));
    expect(result.current.status).toBe("loading");
    await waitFor(() =>
      expect(result.current.status).toBe("ready"),
    );
    expect(result.current.latestRanking?.snapshot_version).toBe(2);
    expect(operatorApi.getOperatorRanking).toHaveBeenCalledTimes(2);
    expect(onError).not.toHaveBeenCalled();
  });

  it("snapshot変更後に届いた古い応答を採用しない", async () => {
    const onError = vi.fn();
    const staleRequest = deferred<OperatorLatestRanking | null>();
    operatorApi.getOperatorRanking
      .mockReturnValueOnce(staleRequest.promise)
      .mockResolvedValueOnce(createRanking(2, 1, 26));
    const { result } = renderHook(() => useOperatorRanking({ onError }));

    act(() => result.current.synchronizeRanking(1));
    expect(result.current.status).toBe("loading");
    act(() => result.current.synchronizeRanking(2));
    await waitFor(() =>
      expect(result.current.status).toBe("ready"),
    );
    expect(result.current.latestRanking?.rows[0]?.public_label).toBe("ABA");

    await act(async () => {
      staleRequest.resolve(createRanking(1, 1));
      await staleRequest.promise;
    });
    expect(result.current.status).toBe("ready");
    expect(result.current.latestRanking?.rows[0]?.public_label).toBe("ABA");
    expect(onError).not.toHaveBeenCalled();
  });

  it("取得失敗とnull応答は同じsnapshotを次回同期で再試行できる", async () => {
    const onError = vi.fn();
    operatorApi.getOperatorRanking
      .mockRejectedValueOnce(new Error("一時的な取得失敗"))
      .mockResolvedValueOnce(null)
      .mockResolvedValueOnce(createRanking(1));
    const { result } = renderHook(() => useOperatorRanking({ onError }));

    act(() => result.current.synchronizeRanking(1));
    await waitFor(() =>
      expect(result.current.status).toBe("error"),
    );
    expect(onError).toHaveBeenCalledTimes(1);

    act(() => result.current.synchronizeRanking(1));
    expect(result.current.status).toBe("loading");
    await waitFor(() =>
      expect(result.current.status).toBe("error"),
    );
    expect(onError).toHaveBeenCalledTimes(2);

    act(() => result.current.synchronizeRanking(1));
    expect(result.current.status).toBe("loading");
    await waitFor(() =>
      expect(result.current.status).toBe("ready"),
    );
    expect(result.current.latestRanking?.snapshot_version).toBe(1);
    expect(operatorApi.getOperatorRanking).toHaveBeenCalledTimes(3);
  });

  it("snapshotがnullへ戻った場合は表示を消し、pending応答も破棄する", async () => {
    const onError = vi.fn();
    const pendingRequest = deferred<OperatorLatestRanking | null>();
    operatorApi.getOperatorRanking.mockReturnValue(pendingRequest.promise);
    const { result } = renderHook(() => useOperatorRanking({ onError }));

    act(() => result.current.synchronizeRanking(1));
    expect(result.current.status).toBe("loading");
    act(() => result.current.synchronizeRanking(null));
    expect(result.current.status).toBe("idle");
    await act(async () => {
      pendingRequest.resolve(createRanking(1));
      await pendingRequest.promise;
    });

    expect(result.current.status).toBe("idle");
    expect(result.current.latestRanking).toBeNull();
    expect(onError).not.toHaveBeenCalled();
  });
});
