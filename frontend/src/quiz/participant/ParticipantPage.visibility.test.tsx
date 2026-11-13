import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiTransportError } from "../../api/client";
import type { ParticipantState } from "../../api/contracts";
import { ParticipantPage } from "./ParticipantPage";
import { savePendingSubmission } from "./pendingSubmission";

const audienceApi = vi.hoisted(() => ({
  getAnswerStatus: vi.fn(),
  getParticipantSession: vi.fn(),
  joinParticipant: vi.fn(),
  submitAnswer: vi.fn(),
}));
const clock = vi.hoisted(() => ({ now: Date.parse("2026-08-08T01:00:02Z"), isSynchronized: true, get monotonicOffsetMs() { return performance.now() - this.now; } }));
const realtimeHook = vi.hoisted(() => ({ refresh: null as (() => Promise<boolean>) | null }));

vi.mock("../../api/audienceClient", () => audienceApi);
vi.mock("../_hooks/useServerClock", () => ({ CLOCK_RECHECK_EVENT: "quiz:clock-recheck", useServerClock: () => clock }));
vi.mock("../_hooks/useRealtimeRefresh", async () => {
  const { useEffect } = await import("react");
  return {
    useRealtimeRefresh: (options: { enabled: boolean; refresh: () => Promise<boolean> }) => {
      const { enabled, refresh } = options;
      realtimeHook.refresh = refresh;
      useEffect(() => {
        if (!enabled) return;
        const resume = (): void => {
          if (document.visibilityState === "visible") void refresh();
        };
        document.addEventListener("visibilitychange", resume);
        return () => document.removeEventListener("visibilitychange", resume);
      }, [enabled, refresh]);
      return "live";
    },
  };
});

const ids = {
  event: "60000000-0000-4000-8000-000000000001",
  round: "60000000-0000-4000-8000-000000000002",
  question: "60000000-0000-4000-8000-000000000003",
  option: "60000000-0000-4000-8000-000000000004",
  answer: "60000000-0000-4000-8000-000000000005",
} as const;

function openState(): ParticipantState {
  return {
    award_ceremony: null,
    projected_at: "2026-08-08T01:00:02Z",
    event: {
      timing_basis: "client_monotonic_v1", arrival_grace_ms: 2000, clock_tolerance_ms: 500, client_ahead_tolerance_ms: 2500,
      event_id: ids.event, event_title: "復帰確認", event_state: "InProgress",
      event_version: 2, start_delay_seconds: 3,
    },
    participant: { public_label: "ABC", is_ranking_eligible: true },
    current_round: {
      round_id: ids.round, round_number: 3, version: 3, state_version: 12, state: "Open",
      open_at: "2026-08-08T01:00:00Z", standard_close_at: "2026-08-08T01:00:10Z",
      question: {
        question_id: ids.question, prompt: "2 + 2 は？",
        options: [{ option_id: ids.option, label: "A", content: "4" }],
      },
    },
    latest_result: null,
    result_visibility: { distribution: false, personal_ranking: false, overall_ranking: false },
    latest_ranking_snapshot_version: null,
    personal_result_delivery: { status: "none", revision: null },
  };
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((complete) => { resolve = complete; });
  return { promise, resolve };
}

function changeVisibility(visibility: DocumentVisibilityState): void {
  vi.spyOn(document, "visibilityState", "get").mockReturnValue(visibility);
  document.dispatchEvent(new Event("visibilitychange"));
}

describe("ParticipantPage tab visibility", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    window.sessionStorage.clear();
    clock.isSynchronized = true;
    realtimeHook.refresh = null;
    audienceApi.getParticipantSession.mockResolvedValue(openState());
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("hiddenでの初回状態GETを保留し、visibleで一度だけ取得する", async () => {
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    render(<ParticipantPage />);
    await act(async () => undefined);
    expect(audienceApi.getParticipantSession).not.toHaveBeenCalled();

    await act(async () => changeVisibility("visible"));
    expect(audienceApi.getParticipantSession).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "A 4" })).toBeEnabled();
  });

  it("復帰前のGET完了では解除せず、復帰後の状態と時計の確認が揃うまで回答不可", async () => {
    const oldState = deferred<ParticipantState>();
    const resumedState = deferred<ParticipantState>();
    const view = render(<ParticipantPage />);
    await screen.findByRole("button", { name: "A 4" });
    audienceApi.getParticipantSession
      .mockReturnValueOnce(oldState.promise)
      .mockReturnValueOnce(resumedState.promise);
    let oldRefresh: Promise<boolean> | undefined;
    await act(async () => { oldRefresh = realtimeHook.refresh?.(); });

    await act(async () => {
      changeVisibility("hidden");
      changeVisibility("visible");
    });
    // 復帰前のGETが未完了でも、復帰後のGETを直ちに始める。
    expect(audienceApi.getParticipantSession).toHaveBeenCalledTimes(3);
    expect((audienceApi.getParticipantSession.mock.calls[1]?.[0] as AbortSignal).aborted).toBe(true);
    expect(screen.queryByRole("button", { name: "A 4" })).not.toBeInTheDocument();

    await act(async () => { oldState.resolve(openState()); await oldRefresh; });
    expect(screen.queryByRole("button", { name: "A 4" })).not.toBeInTheDocument();
    await act(async () => { void realtimeHook.refresh?.(); });
    expect(audienceApi.getParticipantSession).toHaveBeenCalledTimes(3);

    clock.isSynchronized = false;
    await act(async () => {
      resumedState.resolve(openState());
      await oldRefresh;
    });
    expect(screen.queryByRole("button", { name: "A 4" })).not.toBeInTheDocument();
    clock.isSynchronized = true;
    view.rerender(<ParticipantPage />);
    expect(screen.getByRole("button", { name: "A 4" })).toBeEnabled();
  });

  it("復帰状態GETが失敗したら古い問題で回答させず、成功した取得で解除する", async () => {
    render(<ParticipantPage />);
    await screen.findByRole("button", { name: "A 4" });
    audienceApi.getParticipantSession.mockRejectedValueOnce(new ApiTransportError("offline"));
    await act(async () => {
      changeVisibility("hidden");
      changeVisibility("visible");
    });
    expect(screen.queryByRole("button", { name: "A 4" })).not.toBeInTheDocument();
    await act(async () => { await realtimeHook.refresh?.(); });
    fireEvent.click(screen.getByRole("button", { name: "A 4" }));
    expect(audienceApi.submitAnswer).toHaveBeenCalledOnce();
  });

  it("hiddenではstate GETを止めても保存未確定回答の有限status照会を続ける", async () => {
    vi.useFakeTimers();
    vi.spyOn(Math, "random").mockReturnValue(0);
    const firstStatus = deferred<unknown>();
    savePendingSubmission({client_elapsed_ms: 2000,  round_id: ids.round, round_version: 3, option_id: ids.option, client_answer_id: ids.answer });
    audienceApi.getAnswerStatus
      .mockReturnValueOnce(firstStatus.promise)
      .mockResolvedValueOnce({
        status: "AcceptedLocked",
        answer: {
          client_elapsed_ms: 2000, timing_basis: "client_monotonic_v1",
          answer_id: ids.answer, round_id: ids.round, option_id: ids.option,
          client_answer_id: ids.answer, received_at: "2026-08-08T01:00:02Z",
        },
      });
    render(<ParticipantPage />);
    await act(async () => undefined);
    expect(audienceApi.getAnswerStatus).toHaveBeenCalledOnce();
    await act(async () => {
      changeVisibility("hidden");
      firstStatus.resolve({ status: "NotStored", client_answer_id: ids.answer });
    });
    expect(screen.getByRole("button", { name: "同じ回答を再送" })).toBeDisabled();
    await act(async () => vi.advanceTimersByTimeAsync(250));

    expect(audienceApi.getAnswerStatus).toHaveBeenCalledTimes(2);
    expect(audienceApi.getParticipantSession).toHaveBeenCalledOnce();
    expect(audienceApi.submitAnswer).not.toHaveBeenCalled();
    expect(screen.getByText("回答は保存済みです。変更はできません。")).toBeVisible();
  });
});
