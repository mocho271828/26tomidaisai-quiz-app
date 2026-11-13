import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiResponseError, ApiTransportError } from "../../api/client";
import type { ParticipantState } from "../../api/contracts";
import { ParticipantPage } from "./ParticipantPage";

const audienceApi = vi.hoisted(() => ({
  getAnswerStatus: vi.fn(),
  getParticipantSession: vi.fn(),
  getServerTime: vi.fn(),
  heartbeatParticipant: vi.fn(),
  joinParticipant: vi.fn(),
  recordReadiness: vi.fn(),
  submitAnswer: vi.fn(),
}));
const realtimeHook = vi.hoisted(() => ({
  refresh: null as (() => Promise<boolean>) | null,
}));

vi.mock("../../api/audienceClient", () => audienceApi);
vi.mock("../_hooks/useRealtimeRefresh", () => ({
  useRealtimeRefresh: (options: { refresh: () => Promise<boolean> }) => {
    realtimeHook.refresh = options.refresh;
    return "live";
  },
}));
vi.mock("../_hooks/useServerClock", () => ({ CLOCK_RECHECK_EVENT: "quiz:clock-recheck",
  useServerClock: () => ({
    isSynchronized: true,
    now: Date.parse("2026-08-08T01:00:02Z"),
    monotonicOffsetMs: performance.now() - Date.parse("2026-08-08T01:00:02Z"),
  }),
}));

const ids = {
  event: "60000000-0000-4000-8000-000000000001",
  round: "60000000-0000-4000-8000-000000000002",
  question: "60000000-0000-4000-8000-000000000003",
  optionA: "60000000-0000-4000-8000-000000000004",
  optionB: "60000000-0000-4000-8000-000000000005",
  answer: "60000000-0000-4000-8000-000000000006",
  eventB: "60000000-0000-4000-8000-000000000007",
  roundB: "60000000-0000-4000-8000-000000000008",
} as const;

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

function participantState(
  state: "PreparingAssets" | "Open" = "Open",
): ParticipantState {
  const isOpen = state === "Open";
  return {
    award_ceremony: null,
    projected_at: "2026-08-08T01:00:02Z",
    event: {
      timing_basis: "client_monotonic_v1", arrival_grace_ms: 2000, clock_tolerance_ms: 500, client_ahead_tolerance_ms: 2500,
      event_id: ids.event,
      event_title: "[DEMO] 初回確認",
      event_state: isOpen ? "InProgress" : "LobbyOpen",
      event_version: 2,
      start_delay_seconds: 3,
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
      state,
      open_at: isOpen ? "2026-08-08T01:00:00Z" : null,
      standard_close_at: isOpen ? "2026-08-08T01:00:05Z" : null,
      question: {
        question_id: ids.question,
        prompt: "[DEMO] 2 + 2 は？",
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

function stateWithAnswer(state = participantState()): ParticipantState {
  return {
    ...state,
    answer: {
      client_elapsed_ms: 2000, timing_basis: "client_monotonic_v1",
      answer_id: ids.answer,
      round_id: state.current_round?.round_id ?? ids.round,
      option_id: ids.optionA,
      client_answer_id: ids.answer,
      received_at: "2026-08-08T01:00:02.500Z",
    },
  };
}

function nextRoundState(): ParticipantState {
  const state = participantState();
  if (state.current_round === null) {
    throw new Error("test fixture requires a round");
  }
  return {
    ...state,
    event: {
      ...state.event,
      event_title: "[DEMO] 次のRound",
      event_version: state.event.event_version + 1,
    },
    current_round: {
      ...state.current_round,
      round_id: ids.roundB,
      state_version: 1,
    },
  };
}

describe("ParticipantPage", () => {
  afterEach(() => vi.restoreAllMocks());
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(performance, "now").mockReturnValue(0);
    realtimeHook.refresh = null;
    window.history.replaceState({}, "", "/");
    window.sessionStorage.clear();
    audienceApi.getParticipantSession.mockResolvedValue(participantState());
    audienceApi.heartbeatParticipant.mockResolvedValue(participantState());
    audienceApi.recordReadiness.mockResolvedValue({
      round_id: ids.round,
      round_version: 3,
      round_state: "PreparingAssets",
      state_version: 12,
    });
  });

  it("第2問を再取得・再表示しても回答にはround_version74を送る", async () => {
    const user = userEvent.setup();
    const projection = participantState();
    projection.current_round = {
      ...projection.current_round!,
      round_number: 3,
      version: 74,
    };
    audienceApi.getParticipantSession.mockResolvedValue(projection);
    audienceApi.heartbeatParticipant.mockResolvedValue(projection);
    audienceApi.submitAnswer.mockImplementation(
      async (
        roundId: string,
        request: { option_id: string; client_answer_id: string },
      ) => ({
        client_elapsed_ms: 2000, timing_basis: "client_monotonic_v1",
        answer_id: ids.answer,
        round_id: roundId,
        option_id: request.option_id,
        client_answer_id: request.client_answer_id,
        received_at: "2026-08-08T01:00:02.500Z",
        is_replay: false,
      }),
    );
    const { unmount } = render(<ParticipantPage />);
    expect(await screen.findByText("第2問")).toBeVisible();

    await act(async () => {
      await realtimeHook.refresh?.();
    });
    expect(screen.getByText("第2問")).toBeVisible();
    unmount();
    render(<ParticipantPage />);
    expect(await screen.findByText("第2問")).toBeVisible();
    expect(screen.queryByText(/第74問/)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "A 3" }));

    expect(audienceApi.submitAnswer).toHaveBeenCalledWith(
      ids.round,
      {
        client_elapsed_ms: 2000,
        round_version: 74,
        option_id: ids.optionA,
        client_answer_id: expect.any(String),
      },
      expect.any(AbortSignal),
    );
    expect(
      await screen.findByText("回答を保存しました。"),
    ).toBeVisible();
  });

  it("開始前は受信済み問題文をDOMへ出さずreadiness通知を送らない", async () => {
    audienceApi.getParticipantSession.mockResolvedValue(
      participantState("PreparingAssets"),
    );

    render(<ParticipantPage />);

    expect(
      await screen.findByRole("heading", {
        name: "次の問題を お待ちください",
      }),
    ).toBeVisible();
    expect(screen.queryByText("[DEMO] 2 + 2 は？")).not.toBeInTheDocument();
    expect(audienceApi.recordReadiness).not.toHaveBeenCalled();
  });

  it("pending回答を端末へ保存できない場合はAPIへ送信しない", async () => {
    const user = userEvent.setup();
    render(<ParticipantPage />);
    const optionA = await screen.findByRole("button", { name: "A 3" });
    const storageSpy = vi
      .spyOn(Storage.prototype, "setItem")
      .mockImplementation(() => {
        throw new DOMException("quota exceeded", "QuotaExceededError");
      });

    try {
      await user.click(optionA);

      expect(audienceApi.submitAnswer).not.toHaveBeenCalled();
      expect(optionA).toBeDisabled();
      expect(screen.getByRole("button", { name: "B 4" })).toBeDisabled();
      expect(
        screen.getByText(
          "回答を送信できませんでした。ブラウザ設定を確認してください。",
        ),
      ).toBeVisible();
    } finally {
      storageSpy.mockRestore();
    }
  });

  it("LAN HTTPでrandomUUIDが公開されなくても回答を送信する", async () => {
    const user = userEvent.setup();
    const getRandomValues = vi.fn((bytes: Uint8Array) => {
      bytes.set(Array.from({ length: 16 }, (_value, index) => index));
      return bytes;
    });
    vi.stubGlobal("crypto", { getRandomValues });
    audienceApi.submitAnswer.mockResolvedValueOnce({
      client_elapsed_ms: 2000, timing_basis: "client_monotonic_v1",
      answer_id: ids.answer,
      round_id: ids.round,
      option_id: ids.optionA,
      client_answer_id: "00010203-0405-4607-8809-0a0b0c0d0e0f",
      received_at: "2026-08-08T01:00:02.500Z",
      is_replay: false,
    });

    try {
      render(<ParticipantPage />);
      await user.click(await screen.findByRole("button", { name: "A 3" }));

      await waitFor(() => expect(audienceApi.submitAnswer).toHaveBeenCalledOnce());
      expect(audienceApi.submitAnswer.mock.calls[0]?.[1]).toMatchObject({
        client_elapsed_ms: 2000,
        client_answer_id: "00010203-0405-4607-8809-0a0b0c0d0e0f",
        option_id: ids.optionA,
      });
      expect(getRandomValues).toHaveBeenCalledOnce();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("4xx確定失敗後も同じRoundの他選択肢をunlockしない", async () => {
    const user = userEvent.setup();
    audienceApi.submitAnswer.mockRejectedValue(
      new ApiResponseError(409, {
        code: "round_version_conflict",
        message: "回答条件が更新されました。",
      }),
    );

    render(<ParticipantPage />);
    const optionA = await screen.findByRole("button", { name: "A 3" });
    await user.click(optionA);

    expect(await screen.findByText("回答条件が更新されました。")).toBeVisible();
    expect(optionA).toBeDisabled();
    expect(screen.getByRole("button", { name: "B 4" })).toBeDisabled();
    expect(
      screen.queryByRole("button", { name: "同じ回答を再送" }),
    ).not.toBeInTheDocument();
  });

  it("同一描画内の連続tapでも回答POSTを1件だけ送る", async () => {
    const answer = deferred<{
      answer_id: string;
      client_answer_id: string;
      is_replay: boolean;
      option_id: string;
      received_at: string;
      client_elapsed_ms: number;
      timing_basis: "client_monotonic_v1";
      round_id: string;
    }>();
    audienceApi.submitAnswer.mockReturnValue(answer.promise);

    render(<ParticipantPage />);
    const optionA = await screen.findByRole("button", { name: "A 3" });
    const optionB = screen.getByRole("button", { name: "B 4" });
    act(() => {
      optionA.click();
      optionB.click();
    });

    expect(audienceApi.submitAnswer).toHaveBeenCalledOnce();
    const request = audienceApi.submitAnswer.mock.calls[0]?.[1] as {
      client_answer_id: string;
      option_id: string;
    };
    expect(request.option_id).toBe(ids.optionA);
    await act(async () => {
      answer.resolve({
        client_elapsed_ms: 2000, timing_basis: "client_monotonic_v1",
        answer_id: ids.answer,
        round_id: ids.round,
        option_id: ids.optionA,
        client_answer_id: request.client_answer_id,
        received_at: "2026-08-08T01:00:02.500Z",
        is_replay: false,
      });
      await answer.promise;
    });
  });

  it("選択回答の送信状態をpressedと読み上げ用説明へ反映する", async () => {
    const user = userEvent.setup();
    const answer = deferred<{
      answer_id: string;
      client_answer_id: string;
      is_replay: boolean;
      option_id: string;
      received_at: string;
      client_elapsed_ms: number;
      timing_basis: "client_monotonic_v1";
      round_id: string;
    }>();
    audienceApi.submitAnswer.mockReturnValue(answer.promise);

    render(<ParticipantPage />);
    const optionA = await screen.findByRole("button", { name: "A 3" });
    const optionB = screen.getByRole("button", { name: "B 4" });
    await user.click(optionA);

    expect(optionA).toHaveAttribute("aria-pressed", "true");
    expect(optionA).toHaveClass("selected-answer");
    expect(optionA).toHaveAccessibleDescription("送信中");
    expect(optionB).toHaveAttribute("aria-pressed", "false");
    expect(
      screen.getByText("回答を保存しています…").closest("section"),
    ).toHaveAttribute("aria-live", "assertive");

    const request = audienceApi.submitAnswer.mock.calls[0]?.[1] as {
      client_answer_id: string;
    };
    await act(async () => {
      answer.resolve({
        client_elapsed_ms: 2000, timing_basis: "client_monotonic_v1",
        answer_id: ids.answer,
        round_id: ids.round,
        option_id: ids.optionA,
        client_answer_id: request.client_answer_id,
        received_at: "2026-08-08T01:00:02.500Z",
        is_replay: false,
      });
      await answer.promise;
    });

    await waitFor(() =>
      expect(optionA).toHaveAccessibleDescription("受付済み"),
    );
    expect(
      screen
        .getByText("回答を保存しました。")
        .closest("section"),
    ).toHaveAttribute("aria-live", "assertive");
  });

  it("通信断では同一payloadを自動再送して正式時間を維持する", async () => {
    const user = userEvent.setup();
    audienceApi.submitAnswer
      .mockRejectedValueOnce(new ApiTransportError(new TypeError("offline")))
      .mockImplementationOnce(
        async (_roundId: string, request: Record<string, string | number>) => ({
          client_elapsed_ms: 2000, timing_basis: "client_monotonic_v1",
          answer_id: ids.answer,
          round_id: ids.round,
          option_id: request.option_id,
          client_answer_id: request.client_answer_id,
          received_at: "2026-08-08T01:00:02.500Z",
          is_replay: true,
        }),
      );
    audienceApi.getAnswerStatus.mockImplementation(
      async (clientAnswerId: string) => ({
        status: "NotStored",
        client_answer_id: clientAnswerId,
      }),
    );

    render(<ParticipantPage />);
    const optionA = await screen.findByRole("button", { name: "A 3" });
    await user.click(optionA);

    await screen.findByText("回答を保存しました。");
    expect(optionA).toBeDisabled();
    expect(screen.getByRole("button", { name: "B 4" })).toBeDisabled();

    const firstPayload = audienceApi.submitAnswer.mock.calls[0]?.[1];
    await waitFor(() => expect(audienceApi.submitAnswer).toHaveBeenCalledTimes(2));
    expect(audienceApi.submitAnswer.mock.calls[1]?.[1]).toEqual(firstPayload);
    await waitFor(() =>
      expect(screen.getByText("回答を保存しました。")).toBeVisible(),
    );
  });

  it("新規join時は以前のsessionに属するpending回答を破棄する", async () => {
    const user = userEvent.setup();
    window.sessionStorage.setItem(
      "quiz.pending-participant-answer.v1",
      JSON.stringify({
        client_elapsed_ms: 2000,
        round_id: ids.round,
        round_version: 3,
        option_id: ids.optionA,
        client_answer_id: ids.answer,
      }),
    );
    audienceApi.getParticipantSession.mockRejectedValue(
      new ApiResponseError(401, {
        code: "participant_authentication_required",
        message: "参加者セッションを確認してください。",
      }),
    );
    audienceApi.joinParticipant.mockResolvedValue(participantState());

    render(<ParticipantPage />);
    await user.click(await screen.findByRole("button", { name: "参加する" }));

    expect(
      window.sessionStorage.getItem("quiz.pending-participant-answer.v1"),
    ).toBeNull();
    expect(audienceApi.getAnswerStatus).not.toHaveBeenCalled();
  });

  it("chooseで拒否した別event projectionのanswer副作用を適用しない", async () => {
    render(<ParticipantPage />);
    expect(
      await screen.findByRole("heading", {
        level: 2,
        name: "[DEMO] 2 + 2 は？",
      }),
    ).toBeVisible();
    window.sessionStorage.setItem(
      "quiz.pending-participant-answer.v1",
      JSON.stringify({
        client_elapsed_ms: 2000,
        round_id: ids.round,
        round_version: 3,
        option_id: ids.optionA,
        client_answer_id: ids.answer,
      }),
    );
    const rejectedCandidate = stateWithAnswer();
    rejectedCandidate.event = {
      ...rejectedCandidate.event,
      event_id: ids.eventB,
      event_title: "拒否対象event",
      event_version: 99,
    };
    audienceApi.getParticipantSession.mockResolvedValueOnce(rejectedCandidate);

    await expect(realtimeHook.refresh?.()).resolves.toBe(false);

    expect(screen.getByRole("heading", { name: "[DEMO] 初回確認" })).toBeVisible();
    expect(screen.queryByText("回答は保存済みです。変更はできません。")).not.toBeInTheDocument();
    expect(
      window.sessionStorage.getItem("quiz.pending-participant-answer.v1"),
    ).not.toBeNull();
  });

  it("参加後のheartbeat更新を送らず、認証期限切れで参加画面へ戻る", async () => {
    render(<ParticipantPage />);
    await screen.findByRole("heading", { level: 2, name: "[DEMO] 2 + 2 は？" });
    document.dispatchEvent(new Event("visibilitychange"));
    expect(audienceApi.heartbeatParticipant).not.toHaveBeenCalled();
    audienceApi.getParticipantSession.mockRejectedValueOnce(new ApiResponseError(401, {
      code: "participant_authentication_required", message: "参加者セッションを確認してください。",
    }));
    await act(async () => { await realtimeHook.refresh?.(); });
    expect(await screen.findByRole("button", { name: "参加する" })).toBeVisible();
  });

  it("Round Aのaccepted状態をRound Bへ持ち越さない", async () => {
    audienceApi.getParticipantSession.mockResolvedValueOnce(stateWithAnswer());
    render(<ParticipantPage />);
    expect(
      await screen.findByText("回答は保存済みです。変更はできません。"),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "A 3" })).toBeDisabled();
    audienceApi.getParticipantSession.mockResolvedValueOnce(nextRoundState());

    await act(async () => {
      await expect(realtimeHook.refresh?.()).resolves.toBe(true);
    });

    await waitFor(() =>
      expect(
        screen.queryByText("回答は保存済みです。変更はできません。"),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: "A 3" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "B 4" })).toBeEnabled();
  });

  it("並行status照会を1件にしAcceptedを遅いNotStoredでdowngradeしない", async () => {
    const user = userEvent.setup();
    const answerStatus = deferred<{
      client_answer_id: string;
      status: "NotStored";
    }>();
    window.sessionStorage.setItem(
      "quiz.pending-participant-answer.v1",
      JSON.stringify({
        client_elapsed_ms: 2000,
        round_id: ids.round,
        round_version: 3,
        option_id: ids.optionA,
        client_answer_id: ids.answer,
      }),
    );
    audienceApi.getAnswerStatus.mockReturnValue(answerStatus.promise);
    render(<ParticipantPage />);
    const verifyButton = await screen.findByRole("button", {
      name: "保存状態を確認",
    });
    await user.click(verifyButton);
    expect(audienceApi.getAnswerStatus).toHaveBeenCalledOnce();

    const accepted = stateWithAnswer();
    accepted.event = {
      ...accepted.event,
      event_version: accepted.event.event_version + 1,
    };
    audienceApi.getParticipantSession.mockResolvedValueOnce(accepted);
    await act(async () => {
      await realtimeHook.refresh?.();
    });
    expect(
      await screen.findByText("回答は保存済みです。変更はできません。"),
    ).toBeVisible();

    await act(async () => {
      answerStatus.resolve({
        status: "NotStored",
        client_answer_id: ids.answer,
      });
      await answerStatus.promise;
    });
    expect(screen.getByText("回答は保存済みです。変更はできません。")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "同じ回答を再送" }),
    ).not.toBeInTheDocument();
  });

  it("Round切替後に届いた旧Roundのsubmit成功を無視する", async () => {
    const user = userEvent.setup();
    const receipt = deferred<unknown>();
    audienceApi.submitAnswer.mockReturnValue(receipt.promise);
    render(<ParticipantPage />);
    await user.click(await screen.findByRole("button", { name: "A 3" }));
    const request = audienceApi.submitAnswer.mock.calls[0]?.[1] as {
      client_answer_id: string;
    };
    audienceApi.getParticipantSession.mockResolvedValueOnce(nextRoundState());
    await act(async () => {
      await realtimeHook.refresh?.();
    });
    expect(screen.getByRole("button", { name: "A 3" })).toBeEnabled();

    await act(async () => {
      receipt.resolve({
        client_elapsed_ms: 2000, timing_basis: "client_monotonic_v1",
        answer_id: ids.answer, round_id: ids.round, option_id: ids.optionA,
        client_answer_id: request.client_answer_id,
        received_at: "2026-08-08T01:00:02.500Z", is_replay: false,
      });
      await receipt.promise;
    });
    expect(screen.getByRole("heading", { name: "[DEMO] 次のRound" })).toBeVisible();
    expect(screen.queryByText("回答を保存しました。")).not.toBeInTheDocument();
  });
});
