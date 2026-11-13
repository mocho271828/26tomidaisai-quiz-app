import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ParticipantState, PersonalResultResponse } from "../../api/contracts";
import type { ParticipantCommonState, RealtimeStateMessage } from "../../api/realtimeContracts";
import type { RealtimeRefreshOptions } from "../_hooks/useRealtimeRefresh";
import { personalResultRevision } from "./participantState";
import { ParticipantPage } from "./ParticipantPage";

const api = vi.hoisted(() => ({ getParticipantResult: vi.fn(), getParticipantSession: vi.fn(), getAnswerStatus: vi.fn(), submitAnswer: vi.fn(), joinParticipant: vi.fn() }));
const realtime = vi.hoisted(() => ({ options: null as RealtimeRefreshOptions | null }));
vi.mock("../../api/audienceClient", () => api);
vi.mock("../_hooks/useRealtimeRefresh", () => ({ useRealtimeRefresh: (options: RealtimeRefreshOptions) => { realtime.options = options; return "live"; } }));
vi.mock("../_hooks/useServerClock", () => ({ CLOCK_RECHECK_EVENT: "quiz:clock-recheck", useServerClock: () => ({ isSynchronized: true, now: Date.parse("2026-10-01T00:00:02Z"), monotonicOffsetMs: performance.now() - Date.parse("2026-10-01T00:00:02Z") }) }));

const id = (suffix: string): string => `72000000-0000-4000-8000-0000000000${suffix}`;
function initial(): ParticipantState {
  return {
    award_ceremony: null,
    projected_at: "2026-10-01T00:00:02Z",
    event: {
    timing_basis: "client_monotonic_v1", arrival_grace_ms: 2000, clock_tolerance_ms: 500, client_ahead_tolerance_ms: 2500,  event_id: id("01"), event_title: "WSによる進行", event_state: "InProgress", event_version: 2, start_delay_seconds: 3 },
    participant: { public_label: "ABC", is_ranking_eligible: true },
    current_round: {
      round_id: id("02"), round_number: 2, version: 3, state_version: 13, state: "Open",
      open_at: "2026-10-01T00:00:00Z", standard_close_at: "2026-10-01T00:00:10Z",
      question: { question_id: id("03"), prompt: "問題", options: [{ option_id: id("04"), label: "A", content: "回答" }] },
    },
    latest_result: null,
    result_visibility: { distribution: false, personal_ranking: false, overall_ranking: false },
    latest_ranking_snapshot_version: null,
    personal_result_delivery: { status: "none", revision: null },
  };
}
function common(state = initial()): ParticipantCommonState {
  return {
    award_ceremony: null,
    projected_at: state.projected_at, event: state.event, current_round: state.current_round, latest_result: null,
    result_visibility: { distribution: false, personal_ranking: false, overall_ranking: false }, latest_ranking_snapshot_version: null, personal_result_delivery: { status: "none", revision: null },
  };
}
function message(state: ParticipantCommonState): RealtimeStateMessage {
  return { type: "participant.state", audience: "participant", event_id: id("01"), stream_id: id("05"), sequence: 1, state };
}
async function apply(state: ParticipantCommonState): Promise<void> {
  await act(async () => { expect(realtime.options?.onState(message(state))).toBe(true); });
}

describe("ParticipantPage common WS", () => {
  afterEach(() => vi.restoreAllMocks());
  beforeEach(() => {
    vi.clearAllMocks();
    realtime.options = null;
    window.sessionStorage.clear();
    api.getParticipantSession.mockResolvedValue(initial());
  });

  it("表彰は共通WSだけで発表待ち・本人呼出し・終了へ進み、本人結果GETを増やさない", async () => {
    render(<ParticipantPage />);
    await screen.findByRole("button", { name: "A 回答" });
    const waiting: ParticipantCommonState = { ...common(),
      event: { ...initial().event, event_state: "Completed", event_version: 3 }, current_round: null,
      award_ceremony: { phase: "Waiting", top_rows: null },
    };
    await apply(waiting);
    expect(screen.getByRole("heading", { name: "最終結果発表!!" })).toBeVisible();
    const announced: ParticipantCommonState = { ...waiting, event: { ...waiting.event, event_version: 4 },
      award_ceremony: { phase: "Announced", top_rows: [{ public_label: "ABC", overall_rank: 5 }] },
    };
    await apply(announced);
    expect(screen.getByRole("heading", { name: /あなたは.*5.*位!!/u })).toBeVisible();
    await apply({ ...announced, event: { ...announced.event, event_version: 5 }, award_ceremony: { phase: "Finished", top_rows: null } });
    expect(screen.getByText("ご参加ありがとうございました")).toBeVisible();
    expect(api.getParticipantSession).toHaveBeenCalledOnce();
    expect(api.getParticipantResult).not.toHaveBeenCalled();
  });

  it("準備/予約/時間遷移を直接適用し、同versionの本人回答を保持してGETを増やさない", async () => {
    const answered = initial();
    answered.answer = {client_elapsed_ms: 2000, timing_basis: "client_monotonic_v1",  answer_id: id("06"), round_id: id("02"), option_id: id("04"), client_answer_id: id("07"), received_at: "2026-10-01T00:00:01Z" };
    api.getParticipantSession.mockResolvedValue(answered);
    render(<ParticipantPage />);
    expect(await screen.findByText("回答は保存済みです。変更はできません。")).toBeVisible();
    const state = common();
    await apply(state);
    await apply({ ...state, projected_at: "2026-10-01T00:00:03Z" });
    expect(screen.getByText("回答は保存済みです。変更はできません。")).toBeVisible();
    expect(api.getParticipantSession).toHaveBeenCalledOnce();
    const preparing = { ...state, event: { ...state.event, event_version: 3 }, current_round: { ...state.current_round!, state: "PreparingAssets" as const, version: 4, state_version: 16 } };
    await apply(preparing);
    await apply({ ...preparing, event: { ...state.event, event_version: 4 }, current_round: { ...preparing.current_round, state: "ReadyToSchedule", version: 5, state_version: 20 } });
    expect(screen.getByText("第1問もうすぐ始まるよ!!")).toBeVisible();
    expect(api.getParticipantSession).toHaveBeenCalledOnce();
  });

  it("新しい公開結果は本人WSまで確認中を表示し、bootstrap GETを増やさない", async () => {
    render(<ParticipantPage />);
    await screen.findByRole("button", { name: "A 回答" });
    await apply(common());
    const base = initial();
    const state: ParticipantCommonState = {
      ...common(), event: { ...base.event, event_version: 3 }, current_round: null,
      latest_result: {
        ...base.current_round!, state: "Reveal", version: 4, state_version: 16, question: base.current_round!.question!,
        result: { publication: "Published", correct_option_id: id("04"), distribution: null },
      },
    };
    state.personal_result_delivery = { status: "pending", revision: personalResultRevision(state) };
    await apply(state);
    expect(screen.getByText("あなたの結果を確認しています…")).toBeVisible();
    expect(screen.queryByText("未回答")).not.toBeInTheDocument();
    expect(api.getParticipantSession).toHaveBeenCalledOnce();
    const response: PersonalResultResponse = {
      delivery: { status: "ready", revision: personalResultRevision(state) },
      result: { selected_option_id: id("04"), ranking: null,
        outcome: {
    timing_basis: "server_received_v1",  status: "Scored", is_correct: true, total_points: 450, base_points: 400, speed_points: 50, elapsed_ms: 5000, scoring_policy_version: "v1", speed_cohort: "Correct", speed_rank: null, speed_cohort_size: null } },
    };
    await act(async () => realtime.options?.onPersonalResult?.(response));
    expect(screen.getByText("450")).toBeVisible();
    expect(api.getParticipantSession).toHaveBeenCalledOnce();
    expect(api.getParticipantResult).not.toHaveBeenCalled();
  });

  it("同じ問題の集計完了通知だけでは正解を表示せず、追加取得なしで発表を待つ", async () => {
    render(<ParticipantPage />);
    await screen.findByRole("button", { name: "A 回答" });
    const base = common();
    const waiting: ParticipantCommonState = {
      ...base, event: { ...base.event, event_version: 3 },
      current_round: { ...base.current_round!, state: "ReadyToReveal", version: 4, state_version: 16 },
    };
    await apply(waiting);
    expect(screen.getByRole("region", { name: "正解発表待ち" })).toBeVisible();
    expect(screen.getByTestId("quiz-stage-announcer")).toHaveTextContent("正解発表をお待ちください");
    expect(screen.queryByRole("tab", { name: "マイスコア" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "A 回答" })).not.toBeInTheDocument();
    expect(screen.queryByText(/回答を集計|自動集計|補助回答/)).not.toBeInTheDocument();
    expect(api.getParticipantSession).toHaveBeenCalledOnce();
    expect(api.getParticipantResult).not.toHaveBeenCalled();
  });

  it("復帰時のcache再適用/古いGET完了/最新GET失敗では回答を再有効化しない", async () => {
    render(<ParticipantPage />);
    await screen.findByRole("button", { name: "A 回答" });
    await apply(common());
    let completeOld: (state: ParticipantState) => void = () => undefined;
    let failResume: (error: Error) => void = () => undefined;
    const old = new Promise<ParticipantState>((done) => { completeOld = done; });
    const resumed = new Promise<ParticipantState>((_done, reject) => { failResume = reject; });
    api.getParticipantSession.mockReturnValueOnce(old).mockReturnValueOnce(resumed);
    let oldFlight: Promise<boolean> | undefined;
    await act(async () => { oldFlight = realtime.options?.refresh(); });
    const visibility = vi.spyOn(document, "visibilityState", "get");
    await act(async () => {
      visibility.mockReturnValue("hidden");
      document.dispatchEvent(new Event("visibilitychange"));
      visibility.mockReturnValue("visible");
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await apply(common());
    expect(screen.queryByRole("button", { name: "A 回答" })).not.toBeInTheDocument();
    let resumeFlight: Promise<boolean> | undefined;
    await act(async () => { resumeFlight = realtime.options?.refresh(); completeOld(initial()); });
    expect(api.getParticipantSession).toHaveBeenCalledTimes(3);
    await apply({ ...common(), projected_at: "2026-10-01T00:00:03Z" });
    expect(screen.queryByRole("button", { name: "A 回答" })).not.toBeInTheDocument();
    await act(async () => { failResume(new Error("offline")); await oldFlight; await resumeFlight; });
    await apply({ ...common(), projected_at: "2026-10-01T00:00:04Z" });
    expect(screen.queryByRole("button", { name: "A 回答" })).not.toBeInTheDocument();
    api.getParticipantSession.mockResolvedValue(initial());
    await act(async () => { await realtime.options?.refresh(); });
    expect(screen.getByRole("button", { name: "A 回答" })).toBeEnabled();
  });

  it("復帰中に公開設定が変わっても本人GETをhookの取得1回へまとめる", async () => {
    render(<ParticipantPage />);
    await screen.findByRole("button", { name: "A 回答" });
    await apply(common());
    const visibility = vi.spyOn(document, "visibilityState", "get");
    await act(async () => {
      visibility.mockReturnValue("hidden");
      document.dispatchEvent(new Event("visibilitychange"));
      visibility.mockReturnValue("visible");
      document.dispatchEvent(new Event("visibilitychange"));
      const changed = { ...common(), result_visibility: { distribution: false, personal_ranking: true, overall_ranking: false } };
      expect(realtime.options?.onState(message(changed), true)).toBe(true);
    });
    expect(api.getParticipantSession).toHaveBeenCalledOnce();
    await act(async () => { await realtime.options?.refresh(); });
    expect(api.getParticipantSession).toHaveBeenCalledTimes(2);
  });

  it("再接続後も本人GET成功まで共通snapshotだけで回答を再有効化しない", async () => {
    render(<ParticipantPage />);
    await screen.findByRole("button", { name: "A 回答" });
    await apply(common());
    await act(async () => realtime.options?.onUnavailable?.());
    api.getParticipantSession.mockRejectedValueOnce(new Error("worker unavailable"));
    await act(async () => { await realtime.options?.refresh(); });
    await apply({ ...common(), projected_at: "2026-10-01T00:00:03Z" });
    expect(screen.queryByRole("button", { name: "A 回答" })).not.toBeInTheDocument();

    api.getParticipantSession.mockResolvedValue(initial());
    await act(async () => { await realtime.options?.refresh(); });
    expect(screen.getByRole("button", { name: "A 回答" })).toBeEnabled();
  });

  it("切断前に開始した本人GETでは再接続後の本人確認を完了しない", async () => {
    render(<ParticipantPage />);
    await screen.findByRole("button", { name: "A 回答" });
    await apply(common());
    let completeOld: (state: ParticipantState) => void = () => undefined;
    api.getParticipantSession.mockReturnValueOnce(new Promise<ParticipantState>((resolve) => { completeOld = resolve; }));
    let oldFlight: Promise<boolean> | undefined;
    await act(async () => { oldFlight = realtime.options?.refresh(); });
    await act(async () => realtime.options?.onUnavailable?.());
    await apply({ ...common(), projected_at: "2026-10-01T00:00:03Z" });
    await act(async () => { completeOld(initial()); await oldFlight; });
    expect(screen.queryByRole("button", { name: "A 回答" })).not.toBeInTheDocument();

    await act(async () => { await realtime.options?.refresh(); });
    expect(screen.getByRole("button", { name: "A 回答" })).toBeEnabled();
  });
});
