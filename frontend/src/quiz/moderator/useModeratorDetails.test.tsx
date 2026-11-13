import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiResponseError } from "../../api/client";
import type { OperatorLatestRanking, OperatorState } from "../../api/operatorContracts";
import type { OperatorResponseTimes } from "../../api/operatorResponseTimeContracts";
import { useModeratorDetails } from "./useModeratorDetails";

const api = vi.hoisted(() => ({ times: vi.fn(), ranking: vi.fn() }));
vi.mock("../../api/operatorResponseTimeClient", () => ({ getOperatorResponseTimes: api.times }));
vi.mock("../../api/operatorClient", () => ({ getOperatorRanking: api.ranking }));

const id = (suffix: string): string => `76000000-0000-4000-8000-0000000000${suffix}`;
function operator(): OperatorState {
  const control = { mode: "manual" as const, initially_visible: false, changes: [] };
  return {
    award_ceremony_phase: null,
    event_id: id("01"), event_version: 10, event_title: "富大祭", event_state: "InProgress",
    projected_at: "2026-10-22T03:00:00Z", start_delay_seconds: 3, participant_count: 10,
    available_questions: [], used_question_ids: [], latest_ranking_snapshot_version: 2,
    result_visibility: { distribution: false, personal_ranking: false, overall_ranking: false },
    visibility_controls: { distribution: control, personal_ranking: control, overall_ranking: control },
    current_round: {
      round_id: id("02"), version: 3, state_version: 5, state: "ReadyToReveal",
      open_at: "2026-10-22T02:59:45Z", standard_close_at: "2026-10-22T02:59:55Z",
      result_publication: "Withheld", can_publish_results: true, result_failure_code: null,
      question: { question_id: id("03"), version: 1, prompt: "現在の問題", options: [] },
      statistics: { answer_count: 0, average_response_ms: null, options: [] },
    },
  };
}
const emptyGroup = { answer_count: 0, average_response_ms: null, median_response_ms: null,
  fastest_response_ms: null, slowest_response_ms: null, rows: [] };
function times(roundId = id("02")): OperatorResponseTimes {
  return { selected_round_id: roundId, rounds: [{ round_id: roundId, round_number: 3, is_practice: false, prompt: "確定済みの問題" }],
    timing_basis: "client_monotonic_v1", all_answers: emptyGroup, correct_answers: emptyGroup };
}
const ranking: OperatorLatestRanking = { round_id: id("02"), snapshot_version: 2, rows: [] };
function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  let reject: (reason: unknown) => void = () => undefined;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

describe("useModeratorDetails", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    api.times.mockResolvedValue(times());
    api.ranking.mockResolvedValue(ranking);
  });
  afterEach(() => vi.restoreAllMocks());

  it("未認証・確定結果なしでは取得しない", () => {
    const onLost = vi.fn();
    const initialProps: { state: OperatorState | null } = { state: null };
    const { result, rerender } = renderHook(({ state }) => useModeratorDetails(state, onLost), { initialProps });
    expect(result.current.status).toBe("idle");
    rerender({ state: { ...operator(), latest_ranking_snapshot_version: null } });
    expect(result.current.status).toBe("idle");
    expect(result.current.data).toBeNull();
    expect(api.times).not.toHaveBeenCalled();
    expect(api.ranking).not.toHaveBeenCalled();
  });

  it("未公開・最終指定なしでも順位を取得し、そのsnapshotの問題の回答時間を取得する", async () => {
    const pending = deferred<OperatorLatestRanking>();
    api.ranking.mockReturnValueOnce(pending.promise);
    const onLost = vi.fn();
    const { result } = renderHook(() => useModeratorDetails(operator(), onLost));
    expect(api.times).not.toHaveBeenCalled();
    await act(async () => pending.resolve(ranking));
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.data).toEqual({ ranking, responseTimes: times() });
    expect(api.times).toHaveBeenCalledExactlyOnceWith(id("02"), expect.any(AbortSignal));
    expect(api.ranking).toHaveBeenCalledOnce();
  });

  it("次問確定・配信・公開設定変更で確定結果を消去も再取得もしない", async () => {
    const onLost = vi.fn();
    const state = operator();
    const { result, rerender } = renderHook(({ value }) => useModeratorDetails(value, onLost), { initialProps: { value: state } });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    const retained = result.current.data;
    for (const phase of ["PreparingAssets", "ReadyToSchedule", "Scheduled", "Open", "AwaitingValidation"] as const) {
      rerender({ value: { ...state, event_version: 11, result_visibility: { ...state.result_visibility, distribution: true },
        current_round: { ...state.current_round!, round_id: id("20"), state_version: 10, state: phase } } });
      expect(result.current.data).toBe(retained);
      expect(result.current.status).toBe("ready");
    }
    expect(api.ranking).toHaveBeenCalledOnce();
    expect(api.times).toHaveBeenCalledOnce();
  });

  it("次問準備中の再読込でも直近の確定済み問題へ復帰する", async () => {
    const state = operator();
    state.current_round = { ...state.current_round!, round_id: id("20"), state: "ReadyToSchedule" };
    const onLost = vi.fn();
    const { result } = renderHook(() => useModeratorDetails(state, onLost));
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.data?.ranking.round_id).toBe(id("02"));
    expect(api.times).toHaveBeenCalledWith(id("02"), expect.any(AbortSignal));
  });

  it("新しい集計版では旧データを隠し、遅延応答を採用しない", async () => {
    const stale = deferred<OperatorLatestRanking>();
    api.ranking.mockReturnValueOnce(stale.promise);
    const onLost = vi.fn();
    const { result, rerender } = renderHook(({ value }) => useModeratorDetails(value, onLost), { initialProps: { value: operator() } });
    const firstSignal: AbortSignal = api.ranking.mock.calls[0]![0];
    const nextRanking = { ...ranking, snapshot_version: 3, round_id: id("20") };
    api.ranking.mockResolvedValueOnce(nextRanking);
    api.times.mockResolvedValueOnce(times(id("20")));
    rerender({ value: { ...operator(), latest_ranking_snapshot_version: 3 } });
    expect(firstSignal.aborted).toBe(true);
    expect(result.current.data).toBeNull();
    await waitFor(() => expect(result.current.data?.ranking).toEqual(nextRanking));
    await act(async () => stale.resolve(ranking));
    expect(result.current.data?.ranking).toEqual(nextRanking);
    expect(api.times).toHaveBeenCalledOnce();
  });

  it("旧版の回答時間が遅れても新版へ混在させない", async () => {
    const stale = deferred<OperatorResponseTimes>();
    api.times.mockReturnValueOnce(stale.promise);
    const onLost = vi.fn();
    const { result, rerender } = renderHook(({ value }) => useModeratorDetails(value, onLost), { initialProps: { value: operator() } });
    await waitFor(() => expect(api.times).toHaveBeenCalledOnce());
    const nextRanking = { ...ranking, snapshot_version: 3, round_id: id("20") };
    api.ranking.mockResolvedValueOnce(nextRanking);
    api.times.mockResolvedValueOnce(times(id("20")));
    rerender({ value: { ...operator(), latest_ranking_snapshot_version: 3 } });
    await waitFor(() => expect(result.current.data?.ranking).toEqual(nextRanking));
    await act(async () => stale.resolve(times()));
    expect(result.current.data?.responseTimes.selected_round_id).toBe(id("20"));
  });

  it.each(["event", "reset", "logout"] as const)("%sで既存結果を引き継がない", async (change) => {
    const onLost = vi.fn();
    const initialProps: { value: OperatorState | null } = { value: operator() };
    const { result, rerender } = renderHook(({ value }) => useModeratorDetails(value, onLost), { initialProps });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    api.ranking.mockReturnValue(new Promise(() => undefined));
    const next = change === "logout" ? null : { ...operator(),
      ...(change === "event" ? { event_id: id("90") } : { latest_ranking_snapshot_version: null }) };
    rerender({ value: next });
    expect(result.current.data).toBeNull();
    expect(result.current.status).toBe(change === "event" ? "loading" : "idle");
  });

  it.each([{ ...ranking, snapshot_version: 1 }, { ...ranking, snapshot_version: 3 }, null])(
    "snapshotが一致しない順位応答は採用せず回答時間も要求しない", async (candidate) => {
      api.ranking.mockResolvedValue(candidate);
      const onLost = vi.fn();
      const { result } = renderHook(() => useModeratorDetails(operator(), onLost));
      await waitFor(() => expect(result.current.status).toBe("error"));
      expect(result.current.data).toBeNull();
      expect(api.times).not.toHaveBeenCalled();
    },
  );

  it("順位と違う問題の回答時間は採用しない", async () => {
    api.times.mockResolvedValue(times(id("90")));
    const onLost = vi.fn();
    const { result } = renderHook(() => useModeratorDetails(operator(), onLost));
    await waitFor(() => expect(result.current.status).toBe("error"));
    expect(result.current.data).toBeNull();
  });

  it("非表示では取得を保留し、表示復帰で最新snapshotだけを取得する", async () => {
    const visibilitySpy = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    const onLost = vi.fn();
    const { result, rerender } = renderHook(({ value }) => useModeratorDetails(value, onLost), { initialProps: { value: operator() } });
    rerender({ value: { ...operator(), latest_ranking_snapshot_version: 3 } });
    expect(api.ranking).not.toHaveBeenCalled();
    api.ranking.mockResolvedValue({ ...ranking, snapshot_version: 3 });
    visibilitySpy.mockReturnValue("visible");
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(api.ranking).toHaveBeenCalledOnce();
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    expect(api.ranking).toHaveBeenCalledOnce();
  });

  it("401は認証失効へ渡し、古い要求の401は新しい状態へ伝えない", async () => {
    const unauthorized = new ApiResponseError(401, { code: "operator_session_invalid", message: "expired" });
    const stale = deferred<OperatorLatestRanking>();
    api.ranking.mockReturnValueOnce(stale.promise);
    const onLost = vi.fn();
    const { result, rerender } = renderHook(({ value }) => useModeratorDetails(value, onLost), { initialProps: { value: operator() } });
    api.ranking.mockResolvedValueOnce({ ...ranking, snapshot_version: 3 });
    rerender({ value: { ...operator(), latest_ranking_snapshot_version: 3 } });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    await act(async () => stale.reject(unauthorized));
    expect(onLost).not.toHaveBeenCalled();
    api.times.mockRejectedValueOnce(unauthorized);
    api.ranking.mockResolvedValueOnce({ ...ranking, snapshot_version: 3 });
    act(() => result.current.retry());
    await waitFor(() => expect(onLost).toHaveBeenCalledOnce());
    expect(result.current.data).toBeNull();
  });

  it("取得期限後は自動連打せず、手動retryで回復する", async () => {
    const deadline = new AbortController();
    const timeoutSpy = vi.spyOn(AbortSignal, "timeout").mockReturnValue(deadline.signal);
    api.ranking.mockImplementationOnce((signal: AbortSignal) => new Promise<OperatorLatestRanking>((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(signal.reason), { once: true });
    }));
    const onLost = vi.fn();
    const { result } = renderHook(() => useModeratorDetails(operator(), onLost));
    expect(timeoutSpy).toHaveBeenCalledWith(5000);
    await act(async () => deadline.abort(new DOMException("deadline", "TimeoutError")));
    expect(result.current.status).toBe("error");
    expect(api.ranking).toHaveBeenCalledOnce();
    timeoutSpy.mockRestore();
    act(() => result.current.retry());
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(api.ranking).toHaveBeenCalledTimes(2);
  });

  it("unmount時に要求を中止する", () => {
    api.ranking.mockReturnValue(new Promise(() => undefined));
    const onLost = vi.fn();
    const { unmount } = renderHook(() => useModeratorDetails(operator(), onLost));
    const signal: AbortSignal = api.ranking.mock.calls[0]![0];
    unmount();
    expect(signal.aborted).toBe(true);
  });
});
