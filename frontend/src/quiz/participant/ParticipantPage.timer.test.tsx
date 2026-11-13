import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiResponseError, ApiTransportError } from "../../api/client";
import type { AnswerReceipt, ParticipantAnswer, ParticipantState } from "../../api/contracts";
import { ParticipantPage } from "./ParticipantPage";
import { loadPendingSubmission, savePendingSubmission } from "./pendingSubmission";

const audienceApi = vi.hoisted(() => ({
  getAnswerStatus: vi.fn(),
  getParticipantIdCandidate: vi.fn(),
  getParticipantSession: vi.fn(),
  getServerTime: vi.fn(),
  heartbeatParticipant: vi.fn(),
  joinParticipant: vi.fn(),
  recordReadiness: vi.fn(),
  submitAnswer: vi.fn(),
}));
const clock = vi.hoisted(() => ({ now: 0, isSynchronized: true, monotonicOffsetMs: -Date.parse("2026-08-08T01:00:02.000Z") }));
const realtimeHook = vi.hoisted(() => ({
  refresh: null as (() => Promise<boolean>) | null,
}));

vi.mock("../../api/audienceClient", () => audienceApi);
vi.mock("../_hooks/useServerClock", () => ({ CLOCK_RECHECK_EVENT: "quiz:clock-recheck", useServerClock: () => clock }));
vi.mock("../_hooks/useRealtimeRefresh", () => ({
  useRealtimeRefresh: (options: { refresh: () => Promise<boolean> }) => {
    realtimeHook.refresh = options.refresh;
    return "live";
  },
}));

const ids = {
  event: "60000000-0000-4000-8000-000000000001",
  round: "60000000-0000-4000-8000-000000000002",
  question: "60000000-0000-4000-8000-000000000003",
  optionA: "60000000-0000-4000-8000-000000000004",
  optionB: "60000000-0000-4000-8000-000000000005",
  answer: "60000000-0000-4000-8000-000000000006",
  nextRound: "60000000-0000-4000-8000-000000000007",
} as const;
const selectedAt = "2026-08-08T01:00:02.000Z";
const pending = {
  client_elapsed_ms: 2000,
  round_id: ids.round,
  round_version: 3,
  option_id: ids.optionA,
  client_answer_id: ids.answer,
};

function openState(): ParticipantState {
  return {
    award_ceremony: null,
    projected_at: selectedAt,
    event: {
      timing_basis: "client_monotonic_v1", arrival_grace_ms: 2000, clock_tolerance_ms: 500, client_ahead_tolerance_ms: 2500,
      event_id: ids.event,
      event_title: "回答タイマー",
      event_state: "InProgress",
      event_version: 2,
      start_delay_seconds: 3,
    },
    participant: {
      public_label: "ABC",
      is_ranking_eligible: true,
    },
    current_round: {
      round_id: ids.round,
      round_number: 3,
      version: 3,
      state_version: 12,
      state: "Open",
      open_at: "2026-08-08T01:00:00Z",
      standard_close_at: "2026-08-08T01:00:05Z",
      question: {
        question_id: ids.question,
        prompt: "2 + 2 は？",
        options: [
          { option_id: ids.optionA, label: "A", content: "3" },
          { option_id: ids.optionB, label: "B", content: "4" },
        ],
      },
    },
    latest_result: null,
    result_visibility: { distribution: false, personal_ranking: false, overall_ranking: false },
    latest_ranking_snapshot_version: null,
    personal_result_delivery: { status: "none", revision: null },
  };
}

function savedAnswer(clientAnswerId: string = ids.answer): ParticipantAnswer {
  return {
    client_elapsed_ms: 2000, timing_basis: "client_monotonic_v1",
    answer_id: ids.answer,
    round_id: ids.round,
    option_id: ids.optionA,
    client_answer_id: clientAnswerId,
    received_at: "2026-08-08T01:00:03.500Z",
  };
}

function receipt(clientAnswerId: string = ids.answer): AnswerReceipt {
  return { ...savedAnswer(clientAnswerId), is_replay: false };
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function expectSelectedTimer(container: HTMLElement): void {
  expect(screen.getByText("回答時間")).toBeVisible();
  expect(screen.getByText("2.0")).toBeVisible();
  expect(container.querySelector(".quiz-stage-progress-bar")).toHaveStyle({ width: "40%" });
}

async function refreshWith(projection: ParticipantState): Promise<void> {
  audienceApi.getParticipantSession.mockResolvedValueOnce(projection);
  await act(async () => { await realtimeHook.refresh?.(); });
}

describe("ParticipantPage answer timer", () => {
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });
  beforeEach(() => {
    vi.resetAllMocks();
    vi.spyOn(performance, "now").mockImplementation(() => clock.now - Date.parse(selectedAt));
    clock.now = Date.parse(selectedAt);
    clock.isSynchronized = true;
    realtimeHook.refresh = null;
    window.sessionStorage.clear();
    audienceApi.getParticipantSession.mockResolvedValue(openState());
    audienceApi.heartbeatParticipant.mockResolvedValue(openState());
    audienceApi.getAnswerStatus.mockImplementation(async (clientAnswerId: string) => ({
      status: "NotStored",
      client_answer_id: clientAnswerId,
    }));
  });

  it("StrictModeで復元したpendingもNotStored後に同一IDを再照会して受付を回復する", async () => {
    vi.useFakeTimers();
    vi.spyOn(Math, "random").mockReturnValue(0);
    savePendingSubmission(pending);
    audienceApi.getAnswerStatus.mockResolvedValueOnce({ status: "NotStored", client_answer_id: ids.answer })
      .mockResolvedValueOnce({ status: "AcceptedLocked", answer: savedAnswer() });
    const { unmount } = render(<StrictMode><ParticipantPage /></StrictMode>);
    await act(async () => {});
    expect(audienceApi.getAnswerStatus).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "同じ回答を再送" })).toBeVisible();
    await act(async () => { await vi.advanceTimersByTimeAsync(250); });
    expect(audienceApi.getAnswerStatus).toHaveBeenCalledTimes(2);
    expect(screen.getByText("回答は保存済みです。変更はできません。")).toBeVisible();
    expect(audienceApi.submitAnswer).not.toHaveBeenCalled();
    expect(loadPendingSubmission()).toBeNull();
    unmount();
    vi.restoreAllMocks();
  });

  it.each(["round", "session", "unmount"] as const)("%s境界でPageの自動再照会を中断する", async (boundary) => {
    vi.useFakeTimers();
    savePendingSubmission(pending);
    const { unmount } = render(<ParticipantPage />);
    await act(async () => {});
    expect(audienceApi.getAnswerStatus).toHaveBeenCalledOnce();
    const signal = audienceApi.getAnswerStatus.mock.calls[0]?.[1] as AbortSignal;
    if (boundary === "round") {
      const next = openState();
      next.event.event_version += 1;
      next.current_round = { ...next.current_round!, round_id: ids.nextRound, round_number: 4 };
      await refreshWith(next);
    } else if (boundary === "session") {
      audienceApi.getParticipantSession.mockRejectedValueOnce(new ApiResponseError(401, {
        code: "participant_authentication_required", message: "セッションを確認してください。",
      }));
      await act(async () => { await realtimeHook.refresh?.(); });
    } else unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(audienceApi.getAnswerStatus).toHaveBeenCalledOnce();
    unmount();
  });

  it("NotStoredの後に締切を過ぎてAcceptedになっても集計中の受付確認文を表示する", async () => {
    vi.useFakeTimers();
    vi.spyOn(Math, "random").mockReturnValue(0);
    savePendingSubmission(pending);
    audienceApi.getAnswerStatus.mockResolvedValueOnce({ status: "NotStored", client_answer_id: ids.answer })
      .mockResolvedValueOnce({ status: "AcceptedLocked", answer: savedAnswer() });
    const { rerender, unmount } = render(<ParticipantPage />);
    await act(async () => {});
    expect(screen.getByRole("button", { name: "同じ回答を再送" })).toBeVisible();
    clock.now += 4_000;
    rerender(<ParticipantPage />);
    expect(screen.getByRole("region", { name: "正解発表待ち" })).toBeVisible();
    await act(async () => { await vi.advanceTimersByTimeAsync(250); });
    const notice = screen.getByText("回答は保存済みです。変更はできません。");
    expect(notice).toBeVisible();
    expect(notice.closest("[aria-hidden=true], [inert]")).toBeNull();
    expect(notice.closest(".participant-collection-card")).not.toBeNull();
    expect(screen.queryByRole("button", { name: "A 3" })).not.toBeInTheDocument();
    expect(audienceApi.submitAnswer).not.toHaveBeenCalled();
    unmount();
    vi.restoreAllMocks();
  });

  it("手動同一payload再送が再び未確定でも新しい有限GETで回復する", async () => {
    vi.useFakeTimers();
    audienceApi.submitAnswer.mockRejectedValue(new ApiTransportError(new TypeError("offline")));
    audienceApi.getAnswerStatus.mockImplementationOnce(async (clientAnswerId: string) => ({
      status: "NotStored", client_answer_id: clientAnswerId,
    })).mockImplementationOnce(async (clientAnswerId: string) => ({
      status: "AcceptedLocked", answer: savedAnswer(clientAnswerId),
    }));
    const { unmount } = render(<ParticipantPage />);
    await act(async () => {});
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "A 3" })); });
    const oldSignal = audienceApi.getAnswerStatus.mock.calls[0]?.[1] as AbortSignal;
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "同じ回答を再送" })); });
    expect(oldSignal.aborted).toBe(true);
    expect(audienceApi.submitAnswer).toHaveBeenCalledTimes(3);
    expect(audienceApi.submitAnswer.mock.calls[1]?.[1]).toEqual(audienceApi.submitAnswer.mock.calls[0]?.[1]);
    expect(audienceApi.getAnswerStatus).toHaveBeenCalledTimes(2);
    expect(screen.getByText("回答は保存済みです。変更はできません。")).toBeVisible();
    unmount();
  });

  it("回答開始から経過時間とprogressを進め、整数秒も小数1桁のまま選択時に固定する", async () => {
    const user = userEvent.setup();
    const state = openState();
    state.current_round!.standard_close_at = "2026-08-08T01:00:10Z";
    audienceApi.getParticipantSession.mockResolvedValue(state);
    audienceApi.submitAnswer.mockReturnValue(deferred<AnswerReceipt>().promise);
    clock.now = Date.parse(state.current_round!.open_at!);
    const { container, rerender } = render(<ParticipantPage />);

    expect(await screen.findByLabelText("経過時間0.0秒")).toBeVisible();
    expect(container.querySelector(".quiz-stage-progress-bar")).toHaveStyle({ width: "0%" });
    clock.now += 2_500;
    rerender(<ParticipantPage />);
    expect(screen.getByLabelText("経過時間2.5秒")).toBeVisible();
    expect(container.querySelector(".quiz-stage-progress-bar")).toHaveStyle({ width: "25%" });

    clock.now += 5_500;
    rerender(<ParticipantPage />);
    expect(screen.getByLabelText("経過時間8.0秒")).toBeVisible();
    expect(container.querySelector(".quiz-stage-progress-bar")).toHaveStyle({ width: "80%" });
    await user.click(screen.getByRole("button", { name: "A 3" }));
    clock.now += 1_000;
    rerender(<ParticipantPage />);
    expect(screen.getByLabelText("回答時間8.0秒")).toBeVisible();
    expect(container.querySelector(".quiz-stage-progress-bar")).toHaveStyle({ width: "80%" });
  });

  it("未回答のまま締切を過ぎても経過時間とprogressを回答時間の上限に保つ", async () => {
    const { container, rerender } = render(<ParticipantPage />);
    expect(await screen.findByLabelText("経過時間2.0秒")).toBeVisible();
    const answerButton = screen.getByRole("button", { name: "A 3" });

    clock.now += 6_000;
    rerender(<ParticipantPage />);
    expect(screen.getByLabelText("経過時間5.0秒")).toBeVisible();
    expect(container.querySelector(".quiz-stage-progress-bar")).toHaveStyle({ width: "100%" });
    expect(answerButton).toBeDisabled();
  });

  it("未完了POSTを待たず正式msで表示とprogressを止め、同じ整数msだけをAPIへ送る", async () => {
    const user = userEvent.setup();
    const response = deferred<AnswerReceipt>();
    audienceApi.submitAnswer.mockReturnValue(response.promise);
    const { container, rerender } = render(<ParticipantPage />);
    expect(await screen.findByLabelText("経過時間2.0秒")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "A 3" }));

    expectSelectedTimer(container);
    expect(loadPendingSubmission()?.client_elapsed_ms).toBe(2000);
    expect(audienceApi.submitAnswer).toHaveBeenCalledWith(ids.round, {
      client_elapsed_ms: 2000,
      round_version: 3,
      option_id: ids.optionA,
      client_answer_id: expect.any(String),
    }, expect.any(AbortSignal));

    clock.now += 1_000;
    rerender(<ParticipantPage />);
    expectSelectedTimer(container);
    expect(screen.getByRole("button", { name: "B 4" })).toBeDisabled();
  });

  it("回答保存を知らせる投影と遅いPOST成功でも選択までの時間を受付時刻で上書きしない", async () => {
    const user = userEvent.setup();
    const response = deferred<AnswerReceipt>();
    audienceApi.submitAnswer.mockReturnValue(response.promise);
    const { container } = render(<ParticipantPage />);
    await user.click(await screen.findByRole("button", { name: "A 3" }));
    const request = audienceApi.submitAnswer.mock.calls[0]?.[1] as { client_answer_id: string };
    const accepted = openState();
    accepted.event.event_version += 1;
    accepted.answer = savedAnswer(request.client_answer_id);
    clock.now += 2_000;
    await refreshWith(accepted);
    expectSelectedTimer(container);

    await act(async () => {
      response.resolve(receipt(request.client_answer_id));
      await response.promise;
    });
    expectSelectedTimer(container);
    expect(screen.getByText("回答は保存済みです。変更はできません。")).toBeVisible();
  });

  it("通信断後の同一回答再送でも最初の選択時刻を保存して表示する", async () => {
    const user = userEvent.setup();
    const retryResponse = deferred<AnswerReceipt>();
    audienceApi.submitAnswer
      .mockRejectedValueOnce(new ApiTransportError(new TypeError("offline")))
      .mockReturnValueOnce(retryResponse.promise);
    const { container, rerender } = render(<ParticipantPage />);
    await user.click(await screen.findByRole("button", { name: "A 3" }));
    expect(audienceApi.submitAnswer).toHaveBeenCalledTimes(2);
    expectSelectedTimer(container);

    clock.now += 2_000;
    rerender(<ParticipantPage />);
    expectSelectedTimer(container);
    expect(loadPendingSubmission()?.client_elapsed_ms).toBe(2000);
    expect(audienceApi.submitAnswer.mock.calls[1]?.[1]).toEqual(audienceApi.submitAnswer.mock.calls[0]?.[1]);
  });

  it("保存結果未確定でも時計を止め、回答の受付済み表示へ切り替えない", async () => {
    const user = userEvent.setup();
    const status = deferred<unknown>();
    audienceApi.submitAnswer.mockRejectedValue(new ApiTransportError(new TypeError("offline")));
    audienceApi.getAnswerStatus.mockReturnValue(status.promise);
    const { container, rerender } = render(<ParticipantPage />);
    await user.click(await screen.findByRole("button", { name: "A 3" }));

    expect(await screen.findByRole("button", { name: "保存状態を確認" })).toBeVisible();
    clock.now += 2_000;
    rerender(<ParticipantPage />);
    expectSelectedTimer(container);
    expect(screen.getByText("確認中")).toBeVisible();
    expect(screen.queryByText("受付済み")).not.toBeInTheDocument();
  });

  it("確定した受付失敗でも選択時間を維持し、受付不可を表示する", async () => {
    const user = userEvent.setup();
    audienceApi.submitAnswer.mockRejectedValue(new ApiResponseError(409, {
      code: "round_version_conflict",
      message: "回答条件が更新されました。",
    }));
    const { container, rerender } = render(<ParticipantPage />);
    await user.click(await screen.findByRole("button", { name: "A 3" }));

    expect(await screen.findByText("回答条件が更新されました。")).toBeVisible();
    clock.now += 2_000;
    rerender(<ParticipantPage />);
    expectSelectedTimer(container);
    expect(screen.getByText("受付不可")).toBeVisible();
    expect(screen.queryByText("受付済み")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "B 4" })).toBeDisabled();
  });

  it("新方式の正式時間がない回答は受付時刻へfallbackしない", async () => {
    const accepted = openState();
    accepted.answer = { ...savedAnswer(), client_elapsed_ms: null };
    audienceApi.getParticipantSession.mockResolvedValue(accepted);
    audienceApi.getAnswerStatus.mockReturnValue(deferred<unknown>().promise);
    const { container, rerender } = render(<ParticipantPage />);

    expect(await screen.findByLabelText("回答時間不明")).toBeVisible();
    const progress = container.querySelector<HTMLElement>(".quiz-stage-progress-bar")?.style.width;
    clock.now += 2_000;
    rerender(<ParticipantPage />);
    expect(screen.getByLabelText("回答時間不明")).toBeVisible();
    expect(screen.queryByLabelText(/経過時間/u)).not.toBeInTheDocument();
    expect(container.querySelector<HTMLElement>(".quiz-stage-progress-bar")?.style.width).toBe(progress);
  });

  it("未確定回答の再読込と保存状態照会後も選択時刻を復元する", async () => {
    savePendingSubmission(pending);
    clock.now += 2_000;
    audienceApi.getAnswerStatus.mockResolvedValue({
      status: "AcceptedLocked",
      answer: savedAnswer(),
    });
    const { container } = render(<ParticipantPage />);

    expect(await screen.findByText("回答は保存済みです。変更はできません。")).toBeVisible();
    expectSelectedTimer(container);
    expect(audienceApi.submitAnswer).not.toHaveBeenCalled();
  });

  it("unmount後のtimeoutでは回答POSTを自動再送しない", async () => {
    vi.useFakeTimers();
    audienceApi.submitAnswer.mockReturnValue(new Promise<AnswerReceipt>(() => undefined));
    const { unmount } = render(<ParticipantPage />);
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "A 3" }));
    expect(audienceApi.submitAnswer).toHaveBeenCalledOnce();
    unmount();
    await act(async () => vi.advanceTimersByTimeAsync(2000));
    expect(audienceApi.submitAnswer).toHaveBeenCalledOnce();
  });

  it("保留情報のない再読込でも正式2000msをDB受付3500msへ置き換えない", async () => {
    const accepted = openState();
    accepted.answer = savedAnswer();
    audienceApi.getParticipantSession.mockResolvedValue(accepted);
    const { container } = render(<ParticipantPage />);
    await screen.findByLabelText("回答時間2.0秒");
    expectSelectedTimer(container);
    expect(screen.queryByText("3.5")).not.toBeInTheDocument();
    expect(audienceApi.submitAnswer).not.toHaveBeenCalled();
  });

  it("旧方式の保存済み回答だけ受付時間として固定表示する", async () => {
    const accepted = openState();
    accepted.answer = { ...savedAnswer(), client_elapsed_ms: null, timing_basis: "server_received_v1" };
    audienceApi.getParticipantSession.mockResolvedValue(accepted);
    const { container, rerender } = render(<ParticipantPage />);
    expect(await screen.findByText("受付までの時間")).toBeVisible();
    expect(screen.getByText("3.5")).toBeVisible();

    clock.now += 2_000;
    rerender(<ParticipantPage />);
    expect(screen.getByText("3.5")).toBeVisible();
    expect(screen.queryByText("回答時間")).not.toBeInTheDocument();
    expect(container.querySelector(".quiz-stage-progress-bar")).toHaveStyle({ width: "70%" });
  });

  it("次Roundでタイマーを再開し、旧Roundの遅い成功応答では停止させない", async () => {
    const user = userEvent.setup();
    const response = deferred<AnswerReceipt>();
    audienceApi.submitAnswer.mockReturnValue(response.promise);
    const { container } = render(<ParticipantPage />);
    await user.click(await screen.findByRole("button", { name: "A 3" }));
    expectSelectedTimer(container);
    const request = audienceApi.submitAnswer.mock.calls[0]?.[1] as { client_answer_id: string };
    const nextRound = openState();
    nextRound.event.event_version += 1;
    nextRound.current_round = {
      ...nextRound.current_round!,
      round_id: ids.nextRound,
      round_number: 4,
      open_at: "2026-08-08T01:00:02Z",
      standard_close_at: "2026-08-08T01:00:07Z",
    };
    await refreshWith(nextRound);
    expect(screen.getByLabelText("経過時間0.0秒")).toBeVisible();
    expect(screen.queryByText("回答時間")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "B 4" })).toBeEnabled();
    expect(loadPendingSubmission()).toBeNull();

    await act(async () => {
      response.resolve(receipt(request.client_answer_id));
      await response.promise;
    });
    expect(screen.getByLabelText("経過時間0.0秒")).toBeVisible();
    expect(screen.queryByText("回答時間")).not.toBeInTheDocument();
  });
});
