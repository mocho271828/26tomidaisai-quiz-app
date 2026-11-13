import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { OperatorState } from "../../api/operatorContracts";
import type { RealtimeRefreshOptions } from "../_hooks/useRealtimeRefresh";
import { ModeratorPage } from "./ModeratorPage";

const api = vi.hoisted(() => ({ session: vi.fn(), state: vi.fn(), times: vi.fn(),
  ranking: vi.fn(), login: vi.fn(), logout: vi.fn() }));
const realtime = vi.hoisted(() => ({ options: null as RealtimeRefreshOptions | null }));
vi.mock("../../api/operatorClient", () => ({ getOperatorSession: api.session, getOperatorState: api.state,
  getOperatorRanking: api.ranking, loginOperator: api.login, logoutOperator: api.logout }));
vi.mock("../../api/operatorResponseTimeClient", () => ({ getOperatorResponseTimes: api.times }));
vi.mock("../_hooks/useRealtimeRefresh", () => ({ useRealtimeRefresh: (options: RealtimeRefreshOptions) => {
  realtime.options = options; return "live";
} }));

const id = (suffix: string): string => `78000000-0000-4000-8000-0000000000${suffix}`;
const question = { question_id: id("03"), version: 1, prompt: "富山の県庁所在地は？",
  options: [{ option_id: id("04"), label: "A", content: "富山市", is_correct: true },
    { option_id: id("05"), label: "B", content: "金沢市", is_correct: false }] };
function initial(): OperatorState {
  const control = { mode: "manual" as const, initially_visible: false, changes: [] };
  return {
    award_ceremony_phase: null, event_id: id("01"), event_title: "富大祭", event_version: 4, projected_at: "2026-10-22T03:00:15Z",
    event_state: "InProgress", start_delay_seconds: 3, participant_count: 1, available_questions: [], used_question_ids: [id("03")],
    latest_ranking_snapshot_version: 1, result_visibility: { distribution: false, personal_ranking: false, overall_ranking: false },
    visibility_controls: { distribution: control, personal_ranking: control, overall_ranking: control },
    current_round: { round_id: id("02"), version: 7, state_version: 76, state: "ReadyToReveal", result_publication: "Withheld",
      can_publish_results: true, result_failure_code: null, open_at: "2026-10-22T03:00:00Z", standard_close_at: "2026-10-22T03:00:10Z",
      question, statistics: { answer_count: 1, average_response_ms: 1234,
        options: [{ option_id: id("04"), answer_count: 1, average_response_ms: 1234 }] } } };
}
async function receive(state: OperatorState): Promise<void> {
  await act(async () => { realtime.options?.onState({ type: "operator.state", audience: "operator", event_id: state.event_id,
    stream_id: id("09"), sequence: 2, state }); });
}
const tableName = "順位・参加者ID・得点";

describe("ModeratorPage", () => {
  let state: OperatorState;
  beforeEach(() => {
    vi.resetAllMocks();
    vi.stubGlobal("fetch", vi.fn());
    state = initial();
    api.session.mockResolvedValue({ operator: { account_id: id("08"), username: "operator", role: "Operator" } });
    api.state.mockImplementation(async () => state);
    const group = { answer_count: 1, average_response_ms: 1234, median_response_ms: 1234, fastest_response_ms: 1234, slowest_response_ms: 1234,
      rows: [{ rank: 1, public_label: "ABC", response_ms: 1234, is_correct: true }] };
    api.times.mockResolvedValue({ timing_basis: "client_monotonic_v1", selected_round_id: id("02"),
      rounds: [{ round_id: id("02"), round_number: 2, is_practice: false, prompt: question.prompt }],
      all_answers: group, correct_answers: group });
    api.ranking.mockResolvedValue({ round_id: id("02"), snapshot_version: 1,
      rows: [{ overall_rank: 1, public_label: "ABC", total_points: 999, correct_count: 3 }] });
    api.logout.mockResolvedValue(undefined);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("司会の未公開正解・確定結果と通常参加者のiframeを分け、進行操作を持たない", async () => {
    render(<ModeratorPage />);
    await screen.findByRole("table", { name: tableName });
    const questionPanel = screen.getByRole("region", { name: "今回の問題" });
    expect(within(questionPanel).getByText("正解")).toBeVisible();
    expect(screen.getByTitle("参加者画面")).toHaveAttribute("src", "/?moderator-preview=1");
    expect(screen.getByRole("table", { name: "順位・参加者ID・得点" })).toBeVisible();
    expect(screen.queryByRole("button", { name: /正解・結果を公開|次の問題|Eventを終了/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/CPU|ReadyToReveal|Withheld/)).not.toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });

  it.each(["PreparingAssets", "ReadyToSchedule", "Scheduled", "Open"] as const)("%sから運営と同じ正答を強調する", async (phase) => {
    state = { ...state, latest_ranking_snapshot_version: null,
      current_round: { ...state.current_round!, state: phase, can_publish_results: false } };
    render(<ModeratorPage />);
    const questionPanel = await screen.findByRole("region", { name: "今回の問題" });
    const correct = within(questionPanel).getByText("富山市").closest("li");
    expect(correct).toHaveClass("moderator-correct-option");
    expect(within(questionPanel).getByText("金沢市").closest("li")).not.toHaveClass("moderator-correct-option");
    expect(within(questionPanel).getByText("正解")).toBeVisible();
  });

  it("次問確定で統計・ランキング・参加者iframeを保持し、今回の1位は集計待ちに戻す", async () => {
    render(<ModeratorPage />);
    await screen.findByRole("table", { name: tableName });
    const frame = screen.getByTitle("参加者画面");
    state = { ...state, event_version: 5, current_round: { ...state.current_round!, round_id: id("20"),
      state: "ReadyToSchedule", state_version: 80, can_publish_results: false,
      question: { ...question, prompt: "次の問題" } } };
    await receive(state);
    expect(screen.getByText("次の問題")).toBeVisible();
    expect(screen.getByRole("table", { name: tableName })).toBeVisible();
    expect(screen.getByText("第1問・正答者1人の回答時間")).toBeVisible();
    expect(screen.getByRole("cell", { name: "999点" })).toBeVisible();
    const winner = screen.getByRole("region", { name: "今回の1位 最速の正答者" });
    expect(within(winner).getByText("集計完了をお待ちください。")).toBeVisible();
    expect(within(winner).queryByText("ABC")).not.toBeInTheDocument();
    expect(screen.getByTitle("参加者画面")).toBe(frame);
    expect(api.times).toHaveBeenCalledOnce();
    expect(api.ranking).toHaveBeenCalledOnce();
  });

  it("最初の問題の集計前でも統計とランキングの枠を表示する", async () => {
    state = { ...state, current_round: null, latest_ranking_snapshot_version: null };
    render(<ModeratorPage />);
    expect(await screen.findByRole("heading", { name: "ランキング TOP5" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "今回の1位 最速の正答者" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "回答分布" })).toBeVisible();
    expect(screen.getByText("ランキングはまだありません。")).toBeVisible();
    expect(api.ranking).not.toHaveBeenCalled();
    expect(api.times).not.toHaveBeenCalled();
  });

  it("通信断でも最終取得と案内して情報を残し、参加者の接続を作り直さない", async () => {
    render(<ModeratorPage />);
    await screen.findByRole("table", { name: tableName });
    const frame = screen.getByTitle("参加者画面");
    act(() => realtime.options?.onUnavailable?.());
    expect(screen.getByText(/運営情報は最後に取得した内容/)).toBeVisible();
    expect(screen.getByRole("table", { name: tableName })).toBeVisible();
    expect(screen.getByRole("cell", { name: "999点" })).toBeVisible();
    expect(screen.getByTitle("参加者画面")).toBe(frame);
    fireEvent.click(screen.getByRole("button", { name: "運営情報を再取得" }));
    await waitFor(() => expect(screen.queryByText(/運営情報は最後に取得した内容/)).not.toBeInTheDocument());
    expect(screen.getByTitle("参加者画面")).toBe(frame);
    expect(api.ranking).toHaveBeenCalledOnce();
    expect(api.times).toHaveBeenCalledOnce();
  });

  it("最速正答者の速度と総合点を表示し、分布の上に正答者の4指標を置く", async () => {
    render(<ModeratorPage />);
    await screen.findByRole("table", { name: tableName });
    const winner = screen.getByRole("region", { name: "今回の1位 最速の正答者" });
    expect(within(winner).getByText("ABC")).toBeVisible();
    expect(within(winner).getByText("1.234秒")).toBeVisible();
    expect(within(winner).getByText("999点")).toBeVisible();
    const distribution = screen.getByRole("region", { name: "回答分布" });
    expect(within(distribution).getAllByText("1.234秒")).toHaveLength(4);
    expect(within(distribution).getAllByRole("term").map((node) => node.textContent)).toEqual(["平均", "最速", "最遅", "中央値"]);
    expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();
    expect(api.times).toHaveBeenCalledExactlyOnceWith(id("02"), expect.any(AbortSignal));
    expect(api.ranking).toHaveBeenCalledOnce();
  });

  it("集計取得失敗から再試行しても参加者iframeを再登録させない", async () => {
    api.ranking.mockRejectedValueOnce(new TypeError("offline"));
    render(<ModeratorPage />);
    await screen.findByText("統計・ランキングを読み込めませんでした。");
    const frame = screen.getByTitle("参加者画面");
    fireEvent.click(screen.getByRole("button", { name: "もう一度読み込む" }));
    await screen.findByRole("table", { name: tableName });
    expect(screen.getByTitle("参加者画面")).toBe(frame);
    expect(screen.getByRole("cell", { name: "999点" })).toBeVisible();
  });

  it("認証失効時は司会情報と参加者iframeを破棄する", async () => {
    render(<ModeratorPage />);
    await screen.findByRole("table", { name: tableName });
    act(() => realtime.options?.onAuthenticationLost?.());
    await screen.findByRole("heading", { name: "司会者ログイン" });
    expect(screen.queryAllByText("1.234秒")).toHaveLength(0);
    expect(screen.queryByTitle("参加者画面")).not.toBeInTheDocument();
  });

  it("ログアウト完了で読み取り画面を破棄する", async () => {
    render(<ModeratorPage />);
    await screen.findByRole("table", { name: tableName });
    fireEvent.click(screen.getByRole("button", { name: "ログアウト" }));
    await screen.findByRole("heading", { name: "司会者ログイン" });
    await waitFor(() => expect(api.logout).toHaveBeenCalledOnce());
    expect(screen.queryByTitle("参加者画面")).not.toBeInTheDocument();
  });
});
