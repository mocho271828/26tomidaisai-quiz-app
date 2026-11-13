import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiResponseError, ApiTransportError } from "../../api/client";
import { cancellationReason } from "../../api/cancellation";
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
  nextRound: "60000000-0000-4000-8000-000000000007",
} as const;

function receipt(clientAnswerId: string): AnswerReceipt {
  return {
    client_elapsed_ms: 2000, timing_basis: "client_monotonic_v1",
    answer_id: ids.answer,
    round_id: ids.round,
    option_id: ids.optionA,
    client_answer_id: clientAnswerId,
    received_at: "2026-08-08T01:00:02.500Z",
    is_replay: false,
  };
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

function openState(): ParticipantState {
  return {
    award_ceremony: null,
    projected_at: "2026-08-08T01:00:02Z",
    event: {
      timing_basis: "client_monotonic_v1", arrival_grace_ms: 2000, clock_tolerance_ms: 500, client_ahead_tolerance_ms: 2500,
      event_id: ids.event,
      event_title: "[DEMO] authoritative answer",
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
      round_number: 2,
      version: 3,
      state_version: 12,
      state: "Open",
      open_at: "2026-08-08T01:00:00Z",
      standard_close_at: "2026-08-08T01:00:05Z",
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

describe("ParticipantPage authoritative answer", () => {
  afterEach(() => vi.restoreAllMocks());
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(performance, "now").mockReturnValue(0);
    realtimeHook.refresh = null;
    window.history.replaceState({}, "", "/");
    window.sessionStorage.clear();
    audienceApi.getParticipantSession.mockResolvedValue(openState());
    audienceApi.heartbeatParticipant.mockResolvedValue(openState());
    audienceApi.getAnswerStatus.mockReset();
    audienceApi.submitAnswer.mockImplementation(
      async (_roundId: string, request: { client_answer_id: string }) => receipt(request.client_answer_id),
    );
  });

  it("正常receiptだけで受付表示・選択時間・lockを維持し、直後のstate GETを送らない", async () => {
    const user = userEvent.setup();
    render(<ParticipantPage />);
    await user.click(await screen.findByRole("button", { name: "A 3" }));

    expect(await screen.findByText("回答を保存しました。")).toBeVisible();
    const selected = screen.getByRole("button", { name: "A 3" });
    expect(selected).toHaveAccessibleDescription("受付済み");
    expect(selected).toHaveAttribute("aria-pressed", "true");
    expect(selected).toBeDisabled();
    expect(screen.getByRole("button", { name: "B 4" })).toBeDisabled();
    expect(screen.getByLabelText("回答時間2.0秒")).toBeVisible();
    expect(loadPendingSubmission()).toBeNull();
    expect(audienceApi.getParticipantSession).toHaveBeenCalledOnce();
    expect(audienceApi.getAnswerStatus).not.toHaveBeenCalled();
  });

  it("receipt後に完了する同一Roundのanswer欠落GETでも受付表示を維持する", async () => {
    const user = userEvent.setup();
    render(<ParticipantPage />);
    await screen.findByRole("button", { name: "A 3" });
    const lateState = deferred<ParticipantState>();
    audienceApi.getParticipantSession.mockReturnValueOnce(lateState.promise);
    let stateRefresh: Promise<boolean> | undefined;
    await act(async () => { stateRefresh = realtimeHook.refresh?.(); });
    await waitFor(() => expect(audienceApi.getParticipantSession).toHaveBeenCalledTimes(2));

    await user.click(screen.getByRole("button", { name: "A 3" }));
    expect(await screen.findByText("回答を保存しました。")).toBeVisible();
    const candidate = openState();
    candidate.projected_at = "2026-08-08T01:00:02.100Z";
    candidate.event.event_title = "answer未反映のprojection";
    await act(async () => {
      lateState.resolve(candidate);
      await stateRefresh;
    });

    expect(screen.getByRole("heading", { name: "answer未反映のprojection" })).toBeVisible();
    expect(screen.getByText("回答を保存しました。")).toBeVisible();
    expect(screen.getByRole("button", { name: "A 3" })).toHaveAccessibleDescription("受付済み");
    expect(screen.getByRole("button", { name: "B 4" })).toBeDisabled();
    expect(screen.getByLabelText("回答時間2.0秒")).toBeVisible();
    expect(audienceApi.getParticipantSession).toHaveBeenCalledTimes(2);
  });

  it.each(["next-round", "void", "published", "unauthorized"] as const)(
    "receipt後も通常refreshで%sへ切り替える",
    async (transition) => {
      const user = userEvent.setup();
      render(<ParticipantPage />);
      await user.click(await screen.findByRole("button", { name: "A 3" }));
      expect(await screen.findByText("回答を保存しました。")).toBeVisible();
      expect(audienceApi.getParticipantSession).toHaveBeenCalledOnce();

      const candidate = openState();
      const round = candidate.current_round!;
      candidate.event.event_version += 1;
      if (transition === "next-round") {
        candidate.current_round = { ...round, round_id: ids.nextRound, round_number: 3 };
      } else if (transition === "void") {
        candidate.current_round = { ...round, state: "Voided", state_version: round.state_version + 1 };
      } else if (transition === "published") {
        candidate.current_round = null;
        candidate.latest_result = {
          ...round,
          question: round.question!,
          state: "Reveal",
          result: {
            publication: "Published", correct_option_id: ids.optionA, distribution: null,
          },
        };
      }
      if (transition === "unauthorized") {
        audienceApi.getParticipantSession.mockRejectedValueOnce(new ApiResponseError(401, {
          code: "participant_authentication_required", message: "参加者セッションを確認してください。",
        }));
      } else {
        audienceApi.getParticipantSession.mockResolvedValueOnce(candidate);
      }
      await act(async () => { await realtimeHook.refresh?.(); });

      expect(audienceApi.getParticipantSession).toHaveBeenCalledTimes(2);
      if (transition === "next-round") {
        expect(screen.getByText("第2問")).toBeVisible();
        expect(screen.getByRole("button", { name: "A 3" })).toBeEnabled();
        expect(screen.queryByText("回答を保存しました。")).not.toBeInTheDocument();
      } else if (transition === "void") {
        expect(screen.getByRole("heading", { name: "しばらく お待ちください" })).toBeVisible();
        expect(screen.queryByRole("button", { name: "A 3" })).not.toBeInTheDocument();
      } else if (transition === "published") {
        expect(screen.getByText("あなたの結果を確認しています…")).toBeVisible();
        expect(screen.queryByText("100点")).not.toBeInTheDocument();
        expect(screen.queryByText("回答を保存しました。")).not.toBeInTheDocument();
      } else {
        expect(screen.getByRole("button", { name: "参加する" })).toBeVisible();
        expect(screen.queryByText("回答を保存しました。")).not.toBeInTheDocument();
      }
    },
  );

  it.each(["round_id", "option_id", "client_answer_id", "client_elapsed_ms"] as const)(
    "%s不一致のreceiptでは受付表示にせずstatus照会を残す",
    async (identityField) => {
      const user = userEvent.setup();
      audienceApi.submitAnswer.mockImplementation(
        async (_roundId: string, request: { client_answer_id: string }) => ({
          ...receipt(request.client_answer_id), [identityField]: identityField === "client_elapsed_ms" ? 1999 : ids.nextRound,
        }),
      );
      const status = deferred<never>();
      audienceApi.getAnswerStatus.mockReturnValue(status.promise);
      render(<ParticipantPage />);
      await user.click(await screen.findByRole("button", { name: "A 3" }));

      await waitFor(() => expect(audienceApi.getAnswerStatus).toHaveBeenCalledOnce());
      expect(screen.getByRole("button", { name: "A 3" })).toHaveAccessibleDescription("確認中");
      expect(screen.queryByText("回答を保存しました。")).not.toBeInTheDocument();
      expect(loadPendingSubmission()).not.toBeNull();
      expect(audienceApi.getParticipantSession).toHaveBeenCalledOnce();
    },
  );

  it("session失効後の旧receiptで受付表示を復活させない", async () => {
    const user = userEvent.setup();
    const pendingReceipt = deferred<AnswerReceipt>();
    audienceApi.submitAnswer.mockReturnValue(pendingReceipt.promise);
    render(<ParticipantPage />);
    await user.click(await screen.findByRole("button", { name: "A 3" }));
    const request = audienceApi.submitAnswer.mock.calls[0]?.[1] as { client_answer_id: string };
    audienceApi.getParticipantSession.mockRejectedValueOnce(new ApiResponseError(401, {
      code: "participant_authentication_required", message: "参加者セッションを確認してください。",
    }));
    await act(async () => { await realtimeHook.refresh?.(); });
    await act(async () => {
      pendingReceipt.resolve(receipt(request.client_answer_id));
      await pendingReceipt.promise;
    });

    expect(screen.getByRole("button", { name: "参加する" })).toBeVisible();
    expect(screen.queryByText("回答を保存しました。")).not.toBeInTheDocument();
    expect(audienceApi.getParticipantSession).toHaveBeenCalledTimes(2);
  });

  it("session失効が保存確認GETを止める場合はauth_invalidatedを明示する", async () => {
    const user = userEvent.setup();
    audienceApi.submitAnswer.mockRejectedValue(new ApiTransportError(new Error("offline")));
    audienceApi.getAnswerStatus.mockImplementation((_id: string, signal: AbortSignal) =>
      new Promise<never>((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(signal.reason), { once: true });
      }));
    render(<ParticipantPage />);
    await user.click(await screen.findByRole("button", { name: "A 3" }));
    await waitFor(() => expect(audienceApi.getAnswerStatus).toHaveBeenCalledOnce());
    const signal = audienceApi.getAnswerStatus.mock.calls[0]?.[1] as AbortSignal;
    audienceApi.getParticipantSession.mockRejectedValueOnce(new ApiResponseError(401, {
      code: "participant_authentication_required", message: "参加者セッションを確認してください。",
    }));
    await act(async () => { await realtimeHook.refresh?.(); });
    expect(cancellationReason(signal)).toBe("auth_invalidated");
    expect(screen.getByRole("button", { name: "参加する" })).toBeVisible();
    expect(audienceApi.getAnswerStatus).toHaveBeenCalledOnce();
  });

  it("matching Accepted projectionを遅いsubmit transport errorでdowngradeしない", async () => {
    const user = userEvent.setup();
    let rejectSubmit: (error: ApiTransportError) => void = () => undefined;
    const pendingSubmit = new Promise<never>((_resolve, reject) => {
      rejectSubmit = reject;
    });
    audienceApi.submitAnswer.mockReturnValue(pendingSubmit);
    render(<ParticipantPage />);
    await user.click(await screen.findByRole("button", { name: "A 3" }));
    const request = audienceApi.submitAnswer.mock.calls[0]?.[1] as {
      client_answer_id: string;
    };
    const accepted = openState();
    accepted.event.event_version += 1;
    accepted.answer = {
      client_elapsed_ms: 2000, timing_basis: "client_monotonic_v1",
      answer_id: ids.answer,
      round_id: ids.round,
      option_id: ids.optionA,
      client_answer_id: request.client_answer_id,
      received_at: "2026-08-08T01:00:02.500Z",
    };
    audienceApi.getParticipantSession.mockResolvedValueOnce(accepted);
    await act(async () => {
      await realtimeHook.refresh?.();
    });
    expect(
      screen.getByText("回答は保存済みです。変更はできません。"),
    ).toBeVisible();

    await act(async () => {
      rejectSubmit(new ApiTransportError(new TypeError("offline")));
      await Promise.allSettled([pendingSubmit]);
    });

    expect(
      screen.getByText("回答は保存済みです。変更はできません。"),
    ).toBeVisible();
    expect(screen.queryByText(/保存結果は未確定/u)).not.toBeInTheDocument();
  });
});
