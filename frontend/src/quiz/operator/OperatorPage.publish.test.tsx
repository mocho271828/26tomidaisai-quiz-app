import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiResponseError } from "../../api/client";
import type { OperatorState } from "../../api/operatorContracts";
import { OperatorPage } from "./OperatorPage";

const api = vi.hoisted(() => ({
  getOperatorSession: vi.fn(), getOperatorState: vi.fn(), getOperatorRanking: vi.fn(), mutateRound: vi.fn(),
}));
const responseTimes = vi.hoisted(() => ({ getOperatorResponseTimes: vi.fn() }));
vi.mock("../../api/operatorClient", () => api);
vi.mock("../../api/operatorResponseTimeClient", () => responseTimes);
vi.mock("../_hooks/useRealtimeRefresh", () => ({ useRealtimeRefresh: () => "live" }));

const id = (suffix: string): string => `74000000-0000-4000-8000-0000000000${suffix}`;
function readyState(canPublishResults = true): OperatorState {
  return {
    award_ceremony_phase: null,
    event_id: id("01"), projected_at: "2026-10-15T00:00:13Z", event_title: "手動正解発表",
    event_state: "InProgress", event_version: 2, start_delay_seconds: 3,
    result_visibility: { distribution: true, personal_ranking: true, overall_ranking: true },
    visibility_controls: {
      distribution: { mode: "manual", initially_visible: true, changes: [] },
      personal_ranking: { mode: "manual", initially_visible: true, changes: [] },
      overall_ranking: { mode: "manual", initially_visible: true, changes: [] },
    },
    participant_count: 2, available_questions: [], used_question_ids: [id("03")], latest_ranking_snapshot_version: 1,
    current_round: {
      round_id: id("02"), version: 3, state_version: 14, state: "ReadyToReveal",
      open_at: "2026-10-15T00:00:00Z", standard_close_at: "2026-10-15T00:00:10Z",
      can_publish_results: canPublishResults, result_publication: "Withheld", result_failure_code: null,
      question: { question_id: id("03"), version: 1, prompt: "問題", options: [
        { option_id: id("04"), label: "A", content: "正しい選択肢", is_correct: true },
        { option_id: id("05"), label: "B", content: "別の選択肢", is_correct: false },
      ] },
      statistics: { answer_count: 2, average_response_ms: 2000, options: [
        { option_id: id("04"), answer_count: 1, average_response_ms: 1000 },
        { option_id: id("05"), answer_count: 1, average_response_ms: 3000 },
      ] },
    },
  };
}

describe("運営による正解公開", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    api.getOperatorSession.mockResolvedValue({ operator: { account_id: id("06"), username: "operator", role: "Operator" } });
    api.getOperatorState.mockResolvedValue(readyState());
    api.getOperatorRanking.mockResolvedValue({ snapshot_version: 1, round_id: id("02"), rows: [
      { public_label: "AAA", overall_rank: 1, total_points: 490, correct_count: 1 },
    ] });
    const emptyGroup = { answer_count: 0, average_response_ms: null, median_response_ms: null,
      fastest_response_ms: null, slowest_response_ms: null, rows: [] };
    responseTimes.getOperatorResponseTimes.mockResolvedValue({ timing_basis: "client_monotonic_v1",
      rounds: [], selected_round_id: null, all_answers: emptyGroup, correct_answers: emptyGroup });
  });

  it("非公開でも確定統計とランキングを閲覧でき、明示操作するまで公開要求を送らない", async () => {
    render(<OperatorPage />);
    expect(await screen.findByRole("button", { name: "正解・結果を公開" })).toBeEnabled();
    expect(screen.getByRole("heading", { name: "集計完了・未公開" })).toBeVisible();
    expect(screen.getAllByText("50%").length).toBeGreaterThan(0);
    expect(await screen.findByText("AAA")).toBeVisible();
    expect(responseTimes.getOperatorResponseTimes).toHaveBeenCalledOnce();
    expect(api.mutateRound).not.toHaveBeenCalled();
  });

  it.each(["AwaitingValidation", "ReadyToReveal"] as const)("%sでもサーバーが公開を許可するまではボタンを無効にする", async (state) => {
    const projection = readyState(false);
    projection.current_round!.state = state;
    api.getOperatorState.mockResolvedValue(projection);
    render(<OperatorPage />);
    const button = await screen.findByRole("button", { name: "正解・結果を公開" });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(api.mutateRound).not.toHaveBeenCalled();
  });

  it("操作理由を入力せずに公開でき、版と冪等キーだけを送る", async () => {
    api.mutateRound.mockResolvedValueOnce({ is_replay: false });
    render(<OperatorPage />);
    const button = await screen.findByRole("button", { name: "正解・結果を公開" });
    expect(screen.queryByRole("textbox", { name: "監査記録へ残す理由（必須）" })).not.toBeInTheDocument();
    expect(button).toBeEnabled();
    fireEvent.click(button);
    expect(api.mutateRound).toHaveBeenCalledWith(id("02"), "publish-results", {
      expected_version: 3, idempotency_key: expect.any(String),
    });
    await screen.findByText("正解・結果の公開が完了しました。");
  });

  it.each(["Suspended", "DataStoreUnavailable", "ServiceLost", "Aborted", "Completed", "Archived"] as const)(
    "%sでは公開可能フラグが残っていても公開要求を送らない", async (eventState) => {
      api.getOperatorState.mockResolvedValue({ ...readyState(), event_state: eventState });
      render(<OperatorPage />);
      await screen.findByRole("heading", { name: "運営" });
      await screen.findByText("AAA");
      const button = screen.queryByRole("button", { name: "正解・結果を公開" });
      if (button !== null) {
        expect(button).toBeDisabled();
        fireEvent.click(button);
      }
      expect(api.mutateRound).not.toHaveBeenCalled();
    },
  );

  it("二重公開を抑止し、通信失敗後は同じ冪等キーで再試行して公開済みへ遷移する", async () => {
    let rejectFirst: (error: Error) => void = () => undefined;
    api.mutateRound.mockReturnValueOnce(new Promise((_resolve, reject) => { rejectFirst = reject; }));
    render(<OperatorPage />);
    const button = await screen.findByRole("button", { name: "正解・結果を公開" });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(api.mutateRound).toHaveBeenCalledOnce();
    expect(api.mutateRound).toHaveBeenCalledWith(id("02"), "publish-results", {
      expected_version: 3, idempotency_key: expect.any(String),
    });
    await act(async () => rejectFirst(new ApiResponseError(503, { code: "temporarily_unavailable", message: "一時的に通信できません。" })));
    await screen.findByRole("alert");
    const key = api.mutateRound.mock.calls[0]?.[2]?.idempotency_key;
    const published = readyState(false);
    published.event_version = 3;
    published.current_round!.state = "Reveal";
    published.current_round!.result_publication = "Published";
    api.getOperatorState.mockResolvedValue(published);
    api.mutateRound.mockResolvedValueOnce({ is_replay: true });
    fireEvent.click(button);
    await waitFor(() => expect(screen.queryByRole("button", { name: "正解・結果を公開" })).not.toBeInTheDocument());
    expect(api.mutateRound.mock.calls[1]?.[2]?.idempotency_key).toBe(key);
    expect(screen.getByRole("heading", { name: "公開済み" })).toBeVisible();
  });
});
