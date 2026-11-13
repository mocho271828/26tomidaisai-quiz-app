import { act, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { OperatorState } from "../../api/operatorContracts";
import type { RealtimeStateMessage } from "../../api/realtimeContracts";
import type { RealtimeRefreshOptions } from "../_hooks/useRealtimeRefresh";
import { OperatorPage } from "./OperatorPage";

const api = vi.hoisted(() => ({ getOperatorSession: vi.fn(), getOperatorState: vi.fn(), getOperatorRanking: vi.fn() }));
const realtime = vi.hoisted(() => ({ options: null as RealtimeRefreshOptions | null }));
vi.mock("../../api/operatorClient", () => api);
vi.mock("../_hooks/useRealtimeRefresh", () => ({ useRealtimeRefresh: (options: RealtimeRefreshOptions) => { realtime.options = options; return "live"; } }));
const id = (suffix: string): string => `73000000-0000-4000-8000-0000000000${suffix}`;
function initial(): OperatorState {
  return {
    award_ceremony_phase: null,
    event_id: id("01"), projected_at: "2026-10-01T00:00:02Z", event_title: "運営共通WS", event_state: "InProgress", event_version: 3, start_delay_seconds: 3,
    result_visibility: { distribution: false, personal_ranking: false, overall_ranking: false },
    visibility_controls: {
      distribution: { mode: "manual", initially_visible: false, changes: [] },
      personal_ranking: { mode: "manual", initially_visible: false, changes: [] },
      overall_ranking: { mode: "manual", initially_visible: false, changes: [] },
    },
    participant_count: 2, available_questions: [], used_question_ids: [], latest_ranking_snapshot_version: null,
    current_round: {
      round_id: id("02"), version: 3, state_version: 13, state: "Open",
      open_at: "2026-10-01T00:00:00Z", standard_close_at: "2026-10-01T00:00:10Z",
      can_publish_results: false,
      result_publication: "Withheld", result_failure_code: null,
      question: { question_id: id("03"), version: 1, prompt: "問題", options: [{ option_id: id("04"), label: "A", content: "回答", is_correct: true }] },
      statistics: { answer_count: 0, average_response_ms: null, options: [{ option_id: id("04"), answer_count: 0, average_response_ms: null }] },
    },
  };
}
function snapshot(state: OperatorState): RealtimeStateMessage {
  return { type: "operator.state", audience: "operator", event_id: id("01"), stream_id: id("05"), sequence: 1, state };
}
function withAnswers(state: OperatorState): OperatorState {
  return { ...state, projected_at: "2026-10-01T00:00:03Z", current_round: {
    ...state.current_round!, statistics: { answer_count: 2, average_response_ms: 1_000, options: [{ option_id: id("04"), answer_count: 2, average_response_ms: 1_000 }] },
  } };
}

describe("OperatorPage common WS", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getOperatorSession.mockResolvedValue({ operator: { account_id: id("06"), username: "operator", role: "Operator" } });
    api.getOperatorState.mockResolvedValue(initial());
    api.getOperatorRanking.mockResolvedValue(null);
  });

  it("同versionの回答数を直接反映し、古いcacheで統計を巻き戻さずGETを増やさない", async () => {
    render(<OperatorPage />);
    await screen.findAllByText("0件");
    await act(async () => { expect(realtime.options?.onState(snapshot(withAnswers(initial())))).toBe(true); });
    expect(screen.getAllByText("2件").length).toBeGreaterThan(0);
    await act(async () => { expect(realtime.options?.onState(snapshot(initial()))).toBe(true); });
    expect(screen.getAllByText("2件").length).toBeGreaterThan(0);
    expect(api.getOperatorState).toHaveBeenCalledOnce();
  });

  it("新しいWSの後に古いHTTP取得が完了しても進行/統計を巻き戻さない", async () => {
    render(<OperatorPage />);
    await screen.findAllByText("0件");
    let resolve: (state: OperatorState) => void = () => undefined;
    const pending = new Promise<OperatorState>((done) => { resolve = done; });
    api.getOperatorState.mockReturnValue(pending);
    let flight: Promise<boolean> | undefined;
    await act(async () => { flight = realtime.options?.refresh(); });
    const next: OperatorState = {
      ...withAnswers(initial()),
      event_state: "Completed",
      award_ceremony_phase: "Waiting",
      event_version: 4,
    };
    await act(async () => { expect(realtime.options?.onState(snapshot(next))).toBe(true); });
    expect(screen.getByText("終了済み")).toBeVisible();
    await act(async () => { resolve(initial()); await flight; });
    expect(screen.getByText("終了済み")).toBeVisible();
    expect(screen.getByText("Eventは完了しました。次の問題へ進む操作はありません。")).toBeVisible();
    expect(screen.queryByRole("heading", { name: "回答受付中" })).not.toBeInTheDocument();
    expect(screen.getAllByText("2件").length).toBeGreaterThan(0);
  });
});
