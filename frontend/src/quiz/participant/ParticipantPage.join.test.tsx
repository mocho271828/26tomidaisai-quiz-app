import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiResponseError, ApiTransportError } from "../../api/client";
import type { ParticipantState } from "../../api/contracts";
import { ParticipantPage } from "./ParticipantPage";
import { getPendingParticipantJoinToken } from "./pendingParticipantJoin";
import * as participantUi from "./participantUi";

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
} as const;

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
      round_number: 3,
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

function participantAuthenticationRequired(): ApiResponseError {
  return new ApiResponseError(401, {
    code: "participant_authentication_required",
    message: "参加者セッションを確認してください。",
  });
}

describe("ParticipantPage join", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.clearAllMocks();
    realtimeHook.refresh = null;
    window.history.replaceState({}, "", "/");
    window.sessionStorage.clear();
    audienceApi.getParticipantSession.mockResolvedValue(participantState());
    audienceApi.heartbeatParticipant.mockResolvedValue(participantState());
    vi.spyOn(participantUi, "createParticipantJoinToken");
  });
  afterEach(() => vi.restoreAllMocks());

  it.each([false, true])("自動参加=%s: 初回session確認の一時障害では同じsessionを再確認する", async (autoJoin) => {
    const user = userEvent.setup();
    audienceApi.getParticipantSession
      .mockRejectedValueOnce(
        new ApiResponseError(503, {
          code: "audience_service_unavailable",
          message: "一時的に要求を完了できません。",
        }),
      )
      .mockResolvedValueOnce(participantState());

    render(<ParticipantPage autoJoin={autoJoin} />);

    const retryButton = await screen.findByRole("button", {
      name: "参加状態を再確認",
    });
    expect(screen.queryByRole("button", { name: "参加する" })).not.toBeInTheDocument();

    await user.click(retryButton);

    expect(
      await screen.findByRole("heading", { name: "[DEMO] 初回確認" }),
    ).toBeVisible();
    expect(audienceApi.getParticipantSession).toHaveBeenCalledTimes(2);
    expect(audienceApi.joinParticipant).not.toHaveBeenCalled();
  });

  it.each([false, true])("StrictMode=%s: 初回401後だけ通常APIで一度自動参加する", async (isStrictMode) => {
    audienceApi.getParticipantSession.mockRejectedValue(participantAuthenticationRequired());
    audienceApi.joinParticipant.mockResolvedValue(participantState());
    const page = <ParticipantPage autoJoin />;

    render(isStrictMode ? <StrictMode>{page}</StrictMode> : page);

    expect(await screen.findByRole("heading", { name: "[DEMO] 初回確認" })).toBeVisible();
    expect(audienceApi.joinParticipant).toHaveBeenCalledExactlyOnceWith(expect.stringMatching(/^[A-Za-z0-9_-]{43}$/u));
    expect(participantUi.createParticipantJoinToken).toHaveBeenCalledOnce();
    expect(window.sessionStorage.getItem("quiz.pending-participant-join.v1")).toBeNull();
  });

  it("既存Cookieで参加できるときは自動登録せず、後の失効でも追加登録しない", async () => {
    render(<ParticipantPage autoJoin />);
    expect(await screen.findByRole("heading", { name: "[DEMO] 初回確認" })).toBeVisible();
    expect(audienceApi.joinParticipant).not.toHaveBeenCalled();

    audienceApi.getParticipantSession.mockRejectedValue(participantAuthenticationRequired());
    await act(async () => { await realtimeHook.refresh?.(); });

    expect(await screen.findByRole("button", { name: "参加する" })).toBeVisible();
    expect(audienceApi.joinParticipant).not.toHaveBeenCalled();
    expect(participantUi.createParticipantJoinToken).not.toHaveBeenCalled();
  });

  it("非表示の間は自動参加を保留し、復帰後のsession確認が401でも参加成功を破棄しない", async () => {
    let rejectSession: (error: ApiResponseError) => void = () => { throw new Error("session check missing"); };
    let completeJoin: (state: ParticipantState) => void = () => { throw new Error("join request missing"); };
    audienceApi.getParticipantSession.mockRejectedValue(participantAuthenticationRequired());
    audienceApi.getParticipantSession.mockImplementationOnce(() => new Promise<ParticipantState>((_resolve, reject) => {
      rejectSession = reject;
    }));
    audienceApi.joinParticipant.mockImplementationOnce(() => new Promise<ParticipantState>((resolve) => {
      completeJoin = resolve;
    }));
    render(<ParticipantPage autoJoin />);
    await waitFor(() => expect(audienceApi.getParticipantSession).toHaveBeenCalledOnce());

    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      rejectSession(participantAuthenticationRequired());
    });
    expect(audienceApi.joinParticipant).not.toHaveBeenCalled();

    visibility.mockReturnValue("visible");
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); });
    await waitFor(() => expect(audienceApi.getParticipantSession).toHaveBeenCalledTimes(2));
    expect(audienceApi.joinParticipant).toHaveBeenCalledOnce();
    await act(async () => { completeJoin(participantState()); });

    expect(await screen.findByRole("heading", { name: "[DEMO] 初回確認" })).toBeVisible();
    expect(audienceApi.joinParticipant).toHaveBeenCalledOnce();
  });

  it("非表示中に別画面で参加済みになった場合は復帰時のsessionを受理して自動登録しない", async () => {
    let rejectSession: (error: ApiResponseError) => void = () => { throw new Error("session check missing"); };
    audienceApi.getParticipantSession.mockImplementationOnce(() => new Promise<ParticipantState>((_resolve, reject) => {
      rejectSession = reject;
    }));
    render(<ParticipantPage autoJoin />);
    await waitFor(() => expect(audienceApi.getParticipantSession).toHaveBeenCalledOnce());

    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      rejectSession(participantAuthenticationRequired());
    });
    expect(audienceApi.joinParticipant).not.toHaveBeenCalled();

    visibility.mockReturnValue("visible");
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); });

    expect(await screen.findByRole("heading", { name: "[DEMO] 初回確認" })).toBeVisible();
    expect(audienceApi.getParticipantSession).toHaveBeenCalledTimes(2);
    expect(audienceApi.joinParticipant).not.toHaveBeenCalled();
  });

  it("自動参加の失敗で繰り返し登録せず、手動再試行は保存済みtokenを再利用する", async () => {
    const user = userEvent.setup();
    audienceApi.getParticipantSession.mockRejectedValue(participantAuthenticationRequired());
    audienceApi.joinParticipant
      .mockRejectedValueOnce(new ApiTransportError(new TypeError("offline")))
      .mockResolvedValueOnce(participantState());
    const { rerender } = render(<ParticipantPage autoJoin />);

    expect(await screen.findByText(/もう一度参加ボタンを押してください。/u)).toBeVisible();
    rerender(<ParticipantPage autoJoin />);
    expect(audienceApi.joinParticipant).toHaveBeenCalledOnce();
    const firstAttempt = audienceApi.joinParticipant.mock.calls[0];

    await user.click(screen.getByRole("button", { name: "参加する" }));

    expect(await screen.findByRole("heading", { name: "[DEMO] 初回確認" })).toBeVisible();
    expect(audienceApi.joinParticipant).toHaveBeenCalledTimes(2);
    expect(audienceApi.joinParticipant.mock.calls[1]).toEqual(firstAttempt);
    expect(participantUi.createParticipantJoinToken).toHaveBeenCalledOnce();
  });

  it("ID入力なしで再送識別用tokenだけを送り、サーバーの発行IDを表示する", async () => {
    const user = userEvent.setup();
    audienceApi.getParticipantSession.mockRejectedValue(
      participantAuthenticationRequired(),
    );
    const response = participantState("PreparingAssets");
    response.participant.public_label = "XYZ";
    audienceApi.joinParticipant.mockResolvedValue(response);

    render(<ParticipantPage />);
    const joinButton = await screen.findByRole("button", { name: "参加する" });

    expect(joinButton.closest("main")).toHaveClass("participant-entry-page");
    expect(screen.getByText("1問").parentElement).toHaveTextContent("練習1問＋本番");
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(
      screen.getByText("IDはランキングに表示されます。"),
    ).toBeVisible();
    expect(joinButton).toBeEnabled();
    expect(audienceApi.joinParticipant).not.toHaveBeenCalled();
    expect(participantUi.createParticipantJoinToken).not.toHaveBeenCalled();

    await user.click(joinButton);

    expect(await screen.findAllByText("XYZ")).toHaveLength(2);
    expect(audienceApi.joinParticipant).toHaveBeenCalledExactlyOnceWith(
      expect.stringMatching(/^[A-Za-z0-9_-]{43}$/u),
    );
    expect(window.sessionStorage.getItem("quiz.pending-participant-join.v1")).toBeNull();
  });

  it.each([
    ["transport error", () => new ApiTransportError(new TypeError("offline"))],
    [
      "5xx response",
      () => new ApiResponseError(503, {
        code: "audience_service_unavailable",
        message: "一時的に要求を完了できません。",
      }),
    ],
  ])("%sでは自動再送せず、再操作時に同じtokenを送る", async (_label, createError) => {
    const user = userEvent.setup();
    audienceApi.getParticipantSession.mockRejectedValue(
      participantAuthenticationRequired(),
    );
    audienceApi.joinParticipant
      .mockRejectedValueOnce(createError())
      .mockResolvedValueOnce(participantState());

    render(<ParticipantPage />);
    await user.click(await screen.findByRole("button", { name: "参加する" }));

    expect(
      await screen.findByText(/もう一度参加ボタンを押してください。/u),
    ).toBeVisible();
    expect(audienceApi.joinParticipant).toHaveBeenCalledOnce();
    const firstAttempt = audienceApi.joinParticipant.mock.calls[0];

    await user.click(screen.getByRole("button", { name: "参加する" }));
    await waitFor(() => expect(audienceApi.joinParticipant).toHaveBeenCalledTimes(2));
    expect(audienceApi.joinParticipant.mock.calls[1]).toEqual(firstAttempt);
    expect(participantUi.createParticipantJoinToken).toHaveBeenCalledOnce();
  });

  it.each([
    ["participant_id_exhausted", "参加者IDの発行上限に達しました。"],
    ["participant_join_unavailable", "現在は参加できません。"],
  ])("409 %sを表示して止まり、再操作でも同じtokenを使う", async (code, message) => {
    const user = userEvent.setup();
    audienceApi.getParticipantSession.mockRejectedValue(
      participantAuthenticationRequired(),
    );
    audienceApi.joinParticipant
      .mockRejectedValueOnce(new ApiResponseError(409, { code, message }))
      .mockResolvedValueOnce(participantState());

    render(<ParticipantPage />);
    await user.click(await screen.findByRole("button", { name: "参加する" }));

    expect(await screen.findByText(message)).toBeVisible();
    expect(audienceApi.joinParticipant).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "参加する" })).toBeEnabled();
    const firstAttempt = audienceApi.joinParticipant.mock.calls[0];

    await user.click(screen.getByRole("button", { name: "参加する" }));

    await waitFor(() => expect(audienceApi.joinParticipant).toHaveBeenCalledTimes(2));
    expect(audienceApi.joinParticipant.mock.calls[1]).toEqual(firstAttempt);
    expect(participantUi.createParticipantJoinToken).toHaveBeenCalledOnce();
  });

  it("登録応答待ちの重複操作ではPOSTを増やさない", async () => {
    const user = userEvent.setup();
    let resolveJoin: (state: ParticipantState) => void = () => {
      throw new Error("join request was not started");
    };
    audienceApi.getParticipantSession.mockRejectedValue(
      participantAuthenticationRequired(),
    );
    audienceApi.joinParticipant.mockImplementationOnce(() =>
      new Promise<ParticipantState>((resolve) => { resolveJoin = resolve; }),
    );

    render(<ParticipantPage />);
    await user.dblClick(await screen.findByRole("button", { name: "参加する" }));

    expect(screen.getByRole("button", { name: "参加しています…" })).toBeDisabled();
    expect(audienceApi.joinParticipant).toHaveBeenCalledOnce();
    await act(async () => { resolveJoin(participantState()); });
    expect(await screen.findByRole("heading", { name: "[DEMO] 初回確認" })).toBeVisible();
  });

  it("応答消失後に同じタブを再読込して401でも保存tokenで再送する", async () => {
    const user = userEvent.setup();
    audienceApi.getParticipantSession.mockRejectedValue(participantAuthenticationRequired());
    audienceApi.joinParticipant
      .mockRejectedValueOnce(new ApiTransportError(new TypeError("offline")))
      .mockResolvedValueOnce(participantState());
    const firstPage = render(<ParticipantPage />);
    await user.click(await screen.findByRole("button", { name: "参加する" }));
    await screen.findByText(/もう一度参加ボタンを押してください。/u);
    const firstAttempt = audienceApi.joinParticipant.mock.calls[0];
    firstPage.unmount();

    render(<ParticipantPage />);
    await user.click(await screen.findByRole("button", { name: "参加する" }));

    await waitFor(() => expect(audienceApi.joinParticipant).toHaveBeenCalledTimes(2));
    expect(audienceApi.joinParticipant.mock.calls[1]).toEqual(firstAttempt);
    expect(participantUi.createParticipantJoinToken).toHaveBeenCalledOnce();
    expect(window.sessionStorage.getItem("quiz.pending-participant-join.v1")).toBeNull();
  });

  it("Cookieで参加状態を確認できたら再送tokenを削除し、追加登録しない", async () => {
    getPendingParticipantJoinToken();

    render(<ParticipantPage />);

    expect(await screen.findByRole("heading", { name: "[DEMO] 初回確認" })).toBeVisible();
    expect(window.sessionStorage.getItem("quiz.pending-participant-join.v1")).toBeNull();
    expect(audienceApi.joinParticipant).not.toHaveBeenCalled();
  });

  it.each(["getItem", "setItem"] as const)("storageの%s失敗を表示し、登録HTTPを開始しない", async (method) => {
    const user = userEvent.setup();
    audienceApi.getParticipantSession.mockRejectedValue(participantAuthenticationRequired());
    render(<ParticipantPage />);
    const button = await screen.findByRole("button", { name: "参加する" });
    vi.spyOn(Storage.prototype, method).mockImplementation(() => {
      throw new DOMException("storage unavailable", "SecurityError");
    });

    await user.click(button);

    expect(await screen.findByText(
      "参加情報をこのタブに保存・確認できませんでした。ブラウザ設定を確認してください。",
    )).toBeVisible();
    expect(audienceApi.joinParticipant).not.toHaveBeenCalled();
    expect(button).toBeEnabled();
  });

  it.each([false, true])("StrictMode=%s: unmount後の409では参加を再試行しない", async (isStrictMode) => {
    const user = userEvent.setup();
    let rejectJoin: (error: ApiResponseError) => void = () => {
      throw new Error("join request was not started");
    };
    audienceApi.getParticipantSession.mockRejectedValue(
      participantAuthenticationRequired(),
    );
    audienceApi.joinParticipant.mockImplementationOnce(() =>
      new Promise<ParticipantState>((_resolve, reject) => { rejectJoin = reject; }),
    );

    const page = <ParticipantPage />;
    const view = render(isStrictMode ? <StrictMode>{page}</StrictMode> : page);
    await user.click(await screen.findByRole("button", { name: "参加する" }));
    expect(audienceApi.joinParticipant).toHaveBeenCalledOnce();
    view.unmount();

    await act(async () => {
      rejectJoin(new ApiResponseError(409, {
        code: "participant_id_exhausted",
        message: "参加者IDの発行上限に達しました。",
      }));
    });

    expect(audienceApi.joinParticipant).toHaveBeenCalledOnce();
    expect(participantUi.createParticipantJoinToken).toHaveBeenCalledOnce();
  });
});
