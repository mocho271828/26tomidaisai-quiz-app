import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AnswerReceipt, ParticipantState } from "../../api/contracts";
import { ParticipantPage } from "./ParticipantPage";
import { loadPendingSubmission } from "./pendingSubmission";

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

vi.mock("../../api/audienceClient", () => audienceApi);
vi.mock("../_hooks/useRealtimeRefresh", () => ({
  useRealtimeRefresh: () => "live",
}));

const ids = {
  event: "61000000-0000-4000-8000-000000000001",
  round: "61000000-0000-4000-8000-000000000002",
  question: "61000000-0000-4000-8000-000000000003",
  optionA: "61000000-0000-4000-8000-000000000004",
  optionB: "61000000-0000-4000-8000-000000000005",
} as const;
const waitingAt = "2026-08-08T01:00:00.000Z";

function scheduledState(): ParticipantState {
  return {
    award_ceremony: null,
    projected_at: waitingAt,
    event: {
      timing_basis: "client_monotonic_v1", arrival_grace_ms: 2000, clock_tolerance_ms: 500, client_ahead_tolerance_ms: 2500,
      event_id: ids.event,
      event_title: "予約済み回答開始",
      event_state: "InProgress",
      event_version: 2,
      start_delay_seconds: 5,
    },
    participant: {
      public_label: "ABC",
      is_ranking_eligible: true,
    },
    current_round: {
      round_id: ids.round,
      round_number: 2,
      version: 3,
      state_version: 12,
      state: "Scheduled",
      open_at: "2026-08-08T01:00:05.000Z",
      standard_close_at: "2026-08-08T01:00:15.000Z",
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

async function advanceClock(milliseconds: number): Promise<void> {
  await act(async () => vi.advanceTimersByTimeAsync(milliseconds));
}

describe("ParticipantPage scheduled answer start", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(waitingAt));
    vi.resetAllMocks();
    window.sessionStorage.clear();
    audienceApi.getServerTime.mockResolvedValue({ shared_sample_age_ms: 0, server_time: waitingAt });
    audienceApi.getParticipantSession.mockResolvedValue(scheduledState());
    // 状態更新の応答が届かなくても、実際の同期時計で受付を開始・終了する。
    audienceApi.heartbeatParticipant.mockReturnValue(
      new Promise<ParticipantState>(() => undefined),
    );
    audienceApi.submitAnswer.mockReturnValue(
      new Promise<AnswerReceipt>(() => undefined),
    );
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("Scheduledのままでも予約時刻に回答を開始し、待機時間を含めず選択時間を固定する", async () => {
    render(<ParticipantPage />);
    await advanceClock(0);

    expect(audienceApi.getServerTime).toHaveBeenCalledTimes(3);
    expect(screen.getByRole("timer", { name: "回答開始まで残り5.0秒" })).toBeVisible();
    expect(screen.queryByText("2 + 2 は？")).not.toBeInTheDocument();
    await advanceClock(4_900);
    expect(audienceApi.heartbeatParticipant).not.toHaveBeenCalled();
    expect(screen.queryByText("2 + 2 は？")).not.toBeInTheDocument();

    await advanceClock(100);
    expect(screen.getByRole("heading", { name: "2 + 2 は？" })).toBeVisible();
    expect(screen.getByLabelText("経過時間0.0秒")).toBeVisible();
    expect(screen.getByRole("button", { name: "A 3" })).toBeEnabled();

    await advanceClock(200);
    fireEvent.click(screen.getByRole("button", { name: "A 3" }));
    expect(screen.getByLabelText("回答時間0.2秒")).toBeVisible();
    expect(loadPendingSubmission()?.client_elapsed_ms).toBe(200);
    expect(audienceApi.submitAnswer).toHaveBeenCalledWith(ids.round, {
      client_elapsed_ms: 200,
      round_version: 3,
      option_id: ids.optionA,
      client_answer_id: expect.any(String),
    }, expect.any(AbortSignal));

    await advanceClock(1_000);
    expect(screen.getByLabelText("回答時間0.2秒")).toBeVisible();
    expect(audienceApi.getParticipantSession).toHaveBeenCalledTimes(1);
  });

  it("古いScheduledのまま回答窓の10秒を使え、締切になった時点で選択を無効にして集計へ進む", async () => {
    const { container } = render(<ParticipantPage />);
    await advanceClock(0);
    await advanceClock(5_000);
    const answerButton = screen.getByRole("button", { name: "B 4" });
    expect(screen.getByLabelText("経過時間0.0秒")).toBeVisible();
    expect(answerButton).toBeEnabled();

    await advanceClock(9_900);
    expect(screen.getByLabelText("経過時間9.9秒")).toBeVisible();
    expect(answerButton).toBeEnabled();
    expect(screen.queryByRole("region", { name: "正解発表待ち" })).not.toBeInTheDocument();

    await advanceClock(100);
    expect(answerButton).toBeDisabled();
    expect(screen.getByRole("region", { name: "正解発表待ち" })).toBeVisible();
    expect(container.querySelector(".quiz-stage-progress-bar")).toHaveStyle({ width: "100%" });
    expect(audienceApi.getParticipantSession).toHaveBeenCalledTimes(1);
    expect(audienceApi.heartbeatParticipant).not.toHaveBeenCalled();
    expect(audienceApi.submitAnswer).not.toHaveBeenCalled();
  });
});

describe("端末時計とクリック境界の統合", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(waitingAt));
    vi.resetAllMocks();
    window.sessionStorage.clear();
    audienceApi.getServerTime.mockResolvedValue({ shared_sample_age_ms: 0, server_time: waitingAt });
    audienceApi.getParticipantSession.mockResolvedValue(scheduledState());
    audienceApi.submitAnswer.mockReturnValue(new Promise<AnswerReceipt>(() => undefined));
  });
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

  it("100msの描画tickの間に選択した267msを表示・POST・保留保存で共有する", async () => {
    render(<ParticipantPage />);
    await advanceClock(0);
    await advanceClock(5267);
    expect(screen.getByLabelText("経過時間0.2秒")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "A 3" }));
    expect(loadPendingSubmission()?.client_elapsed_ms).toBe(267);
    expect(audienceApi.submitAnswer.mock.calls[0]?.[1].client_elapsed_ms).toBe(267);
    expect(screen.getByLabelText("回答時間0.2秒")).toBeVisible();
  });

  it("締切の描画更新が遅れてもhandlerが期限後のクリックを拒否する", async () => {
    render(<ParticipantPage />);
    await advanceClock(0);
    await advanceClock(14900);
    const button = screen.getByRole("button", { name: "A 3" });
    expect(button).toBeEnabled();
    vi.spyOn(performance, "now").mockReturnValue(15001);
    fireEvent.click(button);
    expect(audienceApi.submitAnswer).not.toHaveBeenCalled();
    expect(loadPendingSubmission()).toBeNull();
  });

  it("イベントのないsleep相当のclock停止をクリック前に検出して再確認する", async () => {
    render(<ParticipantPage />);
    await advanceClock(0);
    await advanceClock(5200);
    vi.spyOn(performance, "now").mockReturnValue(7200);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "A 3" })));
    expect(audienceApi.submitAnswer).not.toHaveBeenCalled();
    expect(audienceApi.getParticipantSession).toHaveBeenCalledTimes(2);
    expect(audienceApi.getServerTime).toHaveBeenCalledTimes(6);
  });
});
