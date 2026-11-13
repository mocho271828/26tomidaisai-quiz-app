import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiResponseError } from "../../api/client";
import type { OperatorSession, OperatorState } from "../../api/operatorContracts";
import { OperatorPage } from "./OperatorPage";
import { mockDialogMethods } from "./confirmationDialogTestSupport";

const operatorApi = vi.hoisted(() => ({
  advanceRound: vi.fn(),
  completeEvent: vi.fn(),
  getOperatorRanking: vi.fn(),
  getOperatorSession: vi.fn(),
  getOperatorState: vi.fn(),
  loginOperator: vi.fn(),
  logoutOperator: vi.fn(),
  mutateRound: vi.fn(),
  retryRoundResults: vi.fn(),
  selectRoundQuestion: vi.fn(),
  setResultVisibility: vi.fn(),
}));
const realtimeHook = vi.hoisted(() => ({ refresh: null as (() => Promise<boolean>) | null }));
const responseTimesApi = vi.hoisted(() => ({ getOperatorResponseTimes: vi.fn() }));
const finalRoundApi = vi.hoisted(() => ({ getOperatorFinalRound: vi.fn(), setOperatorFinalRound: vi.fn() }));

vi.mock("../../api/operatorClient", () => operatorApi);
vi.mock("../../api/operatorResponseTimeClient", () => responseTimesApi);
vi.mock("../../api/operatorFinalRoundClient", () => finalRoundApi);
vi.mock("../_hooks/useRealtimeRefresh", async () => {
  const { useEffect } = await import("react");
  return {
    useRealtimeRefresh: ({ enabled, refresh }: { enabled: boolean; refresh: () => Promise<boolean> }) => {
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
  account: "60000000-0000-4000-8000-000000000011",
  event: "60000000-0000-4000-8000-000000000001",
  round: "60000000-0000-4000-8000-000000000002",
  round2: "60000000-0000-4000-8000-000000000029",
  question: "60000000-0000-4000-8000-000000000003",
  optionA: "60000000-0000-4000-8000-000000000004",
  optionB: "60000000-0000-4000-8000-000000000005",
  question2: "60000000-0000-4000-8000-000000000009",
  question3: "60000000-0000-4000-8000-000000000014",
  question4: "60000000-0000-4000-8000-000000000019",
  question5: "60000000-0000-4000-8000-000000000024",
  question6: "60000000-0000-4000-8000-000000000034",
} as const;

const state: OperatorState = {
  award_ceremony_phase: null,
  event_id: ids.event,
  projected_at: "2026-08-08T01:00:00Z",
  event_title: "[DEMO] 初回確認",
  event_state: "LobbyOpen",
  event_version: 4,
  result_visibility: { distribution: true, personal_ranking: false, overall_ranking: false },
  start_delay_seconds: 3,
  visibility_controls: {
    distribution: { mode: "manual", initially_visible: true, changes: [] },
    personal_ranking: { mode: "manual", initially_visible: false, changes: [] },
    overall_ranking: { mode: "manual", initially_visible: false, changes: [] },
  },
  participant_count: 2,
  available_questions: [
    {
      question_id: ids.question,
      version: 1,
      display_order: 1,
      prompt: "[DEMO] 2 + 2 は？",
      options: [
        {
          option_id: ids.optionA,
          label: "A",
          content: "3",
          is_correct: false,
        },
        {
          option_id: ids.optionB,
          label: "B",
          content: "4",
          is_correct: true,
        },
      ],
    },
    {
      question_id: ids.question2,
      version: 2,
      display_order: 2,
      prompt: "[DEMO] 架空の会場の入口は何色？",
      options: [
        {
          option_id: "60000000-0000-4000-8000-000000000010",
          label: "A",
          content: "あか",
          is_correct: false,
        },
        {
          option_id: "60000000-0000-4000-8000-000000000011",
          label: "B",
          content: "あお",
          is_correct: true,
        },
        {
          option_id: "60000000-0000-4000-8000-000000000012",
          label: "C",
          content: "みどり",
          is_correct: false,
        },
        {
          option_id: "60000000-0000-4000-8000-000000000013",
          label: "D",
          content: "きいろ",
          is_correct: false,
        },
      ],
    },
    {
      question_id: ids.question3,
      version: 3,
      display_order: 3,
      prompt: "[DEMO] 架空の合言葉は？",
      options: [
        {
          option_id: "60000000-0000-4000-8000-000000000017",
          label: "C",
          content: "空へジャンプ",
          is_correct: true,
        },
      ],
    },
    {
      question_id: ids.question4,
      version: 4,
      display_order: 4,
      prompt: "[DEMO] 最初に点灯するライトは？",
      options: [
        {
          option_id: "60000000-0000-4000-8000-000000000021",
          label: "B",
          content: "中央の青いライト",
          is_correct: true,
        },
      ],
    },
    {
      question_id: ids.question5,
      version: 5,
      display_order: 5,
      prompt: "[DEMO] フィナーレの乗り物は？",
      options: [
        {
          option_id: "60000000-0000-4000-8000-000000000026",
          label: "B",
          content: "星模様の青い気球",
          is_correct: true,
        },
      ],
    },
    {
      question_id: ids.question6,
      version: 6,
      display_order: 6,
      prompt: "[DEMO] 最後の合図は？",
      options: [
        {
          option_id: "60000000-0000-4000-8000-000000000036",
          label: "B",
          content: "みんなで拍手",
          is_correct: true,
        },
      ],
    },
  ],
  used_question_ids: [],
  current_round: {
    round_id: ids.round,
    version: 3,
    state_version: 12,
    state: "ReadyToSchedule",
    open_at: null,
    standard_close_at: null,
    can_publish_results: false,
    result_publication: "Withheld",
    result_failure_code: null,
    question: {
      question_id: ids.question,
      version: 1,
      prompt: "[DEMO] 2 + 2 は？",
      options: [
        { option_id: ids.optionA, label: "A", content: "3", is_correct: false },
        { option_id: ids.optionB, label: "B", content: "4", is_correct: true },
      ],
    },
    statistics: {
      answer_count: 0,
      average_response_ms: null,
      options: [
        {
          option_id: ids.optionA,
          answer_count: 0,
          average_response_ms: null,
        },
        {
          option_id: ids.optionB,
          answer_count: 0,
          average_response_ms: null,
        },
      ],
    },
  },
  latest_ranking_snapshot_version: null,
};

function progressionState(
  roundState:
    | "AwaitingValidation"
    | "Reveal"
    | "VoidCandidate"
    | "RoundBackupFailed"
    | "RoundBackupPending",
  usedQuestionIds: string[] = [ids.question],
): OperatorState {
  return {
    ...state,
    event_state: "InProgress",
    used_question_ids: usedQuestionIds,
    current_round: {
      ...state.current_round!,
      state: roundState,
      state_version: 28,
      can_publish_results: false,
      result_publication: "Published",
    },
  };
}

const advanceMutationResponse = {
  operation: "advance_round",
  event_id: ids.event,
  event_version: 5,
  round_id: ids.round2,
  round_version: 4,
  round_state: "PreparingAssets",
  result_visibility: { distribution: true, personal_ranking: false, overall_ranking: false },
  open_at: null,
  standard_close_at: null,
  valid_answer_count: null,
  ranking_row_count: null,
  is_replay: false,
} as const;

function changeVisibility(visibility: DocumentVisibilityState): void {
  vi.spyOn(document, "visibilityState", "get").mockReturnValue(visibility);
  document.dispatchEvent(new Event("visibilitychange"));
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((complete) => { resolve = complete; });
  return { promise, resolve };
}

describe("OperatorPage", () => {
  let restoreDialogMethods: () => void;
  afterEach(() => {
    cleanup();
    restoreDialogMethods();
    vi.restoreAllMocks();
  });
  beforeEach(() => {
    vi.clearAllMocks();
    restoreDialogMethods = mockDialogMethods();
    realtimeHook.refresh = null;
    operatorApi.getOperatorSession.mockResolvedValue({
      operator: {
        account_id: ids.account,
        username: "demo-operator",
        role: "Operator",
      },
    });
    operatorApi.getOperatorState.mockResolvedValue(state);
    operatorApi.getOperatorRanking.mockResolvedValue(null);
    finalRoundApi.getOperatorFinalRound.mockResolvedValue({
      event_id: ids.event, event_version: state.event_version, final_round_id: null,
    });
    responseTimesApi.getOperatorResponseTimes.mockResolvedValue({
      rounds: [], selected_round_id: null,
      all_answers: { answer_count: 0, average_response_ms: null, median_response_ms: null,
        fastest_response_ms: null, slowest_response_ms: null, rows: [] },
      correct_answers: { answer_count: 0, average_response_ms: null, median_response_ms: null,
        fastest_response_ms: null, slowest_response_ms: null, rows: [] },
    });
    operatorApi.loginOperator.mockResolvedValue({
      operator: {
        account_id: ids.account,
        username: "demo-operator",
        role: "Operator",
      },
    });
  });

  it("hiddenの初回状態取得を保留し、visible復帰で認証済み状態を1回取得する", async () => {
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    render(<OperatorPage />);
    await act(async () => undefined);
    expect(operatorApi.getOperatorSession).toHaveBeenCalledOnce();
    expect(operatorApi.getOperatorState).not.toHaveBeenCalled();
    expect(operatorApi.getOperatorRanking).not.toHaveBeenCalled();

    await act(async () => changeVisibility("visible"));
    expect(operatorApi.getOperatorState).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "3秒後に開始予約" })).toBeEnabled();
  });

  it("操作理由の入力欄を表示せず、5つのパネルを順に表示して操作できる", async () => {
    const user = userEvent.setup();
    operatorApi.getOperatorState.mockResolvedValueOnce({
      ...state,
      current_round: { ...state.current_round!, state: "PreparingAssets" },
    });
    render(<OperatorPage />);

    expect(await screen.findByRole("region", { name: "現在の問題と進行" })).toBeVisible();
    expect(screen.getByRole("region", { name: "参加者への公開設定" })).toBeVisible();
    expect(screen.getByRole("region", { name: "回答分布" })).toBeVisible();
    expect(screen.getByRole("region", { name: "総合ランキング" })).toBeVisible();
    expect(screen.getByRole("region", { name: "回答速度" })).toBeVisible();
    const panels = document.querySelector(".operator-dashboard-grid")!.children;
    expect(Array.from(panels)).toEqual([
      screen.getByRole("region", { name: "現在の問題と進行" }),
      screen.getByRole("region", { name: "参加者への公開設定" }),
      screen.getByRole("region", { name: "回答分布" }),
      screen.getByRole("region", { name: "総合ランキング" }),
      screen.getByRole("region", { name: "回答速度" }),
    ]);
    expect(screen.queryByText("操作理由")).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "監査記録へ残す理由（必須）" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "問題を確定して配信" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "自分の順位・みんなの順位" })).toBeEnabled();

    await user.click(screen.getByText("候補を確認"));
    const availableActions = [
      screen.getByRole("button", { name: "問題を確定して配信" }),
      screen.getByRole("button", { name: "登録問題 2へ切り替える" }),
      ...within(screen.getByRole("region", { name: "参加者への公開設定" })).getAllByRole("button"),
    ];
    for (const button of availableActions) {
      expect(button).toBeEnabled();
    }
    expect(operatorApi.mutateRound).not.toHaveBeenCalled();
    expect(operatorApi.selectRoundQuestion).not.toHaveBeenCalled();
    expect(operatorApi.setResultVisibility).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "ログアウト" })).toBeEnabled();
  });

  it("状態取得中にhiddenへ移りsnapshotが変わっても順位GETを始めず、復帰後に取得する", async () => {
    const firstState = deferred<OperatorState>();
    const nextState = { ...state, latest_ranking_snapshot_version: 2 };
    operatorApi.getOperatorState.mockReturnValueOnce(firstState.promise).mockResolvedValue(nextState);
    operatorApi.getOperatorRanking.mockResolvedValue({
      snapshot_version: 2, round_id: ids.round,
      rows: [{ overall_rank: 1, public_label: "XYZ", total_points: 400, correct_count: 1 }],
    });
    render(<OperatorPage />);
    await act(async () => undefined);
    expect(operatorApi.getOperatorState).toHaveBeenCalledOnce();

    await act(async () => {
      changeVisibility("hidden");
      firstState.resolve(nextState);
    });
    expect(operatorApi.getOperatorRanking).not.toHaveBeenCalled();
    await act(async () => { await realtimeHook.refresh?.(); });
    expect(operatorApi.getOperatorState).toHaveBeenCalledOnce();

    await act(async () => changeVisibility("visible"));
    expect(operatorApi.getOperatorState).toHaveBeenCalledTimes(2);
    expect(operatorApi.getOperatorRanking).toHaveBeenCalledOnce();
    expect(screen.getByText("XYZ")).toBeVisible();
  });

  it("運営操作がhidden中に完了しても追加state GETは保留し、復帰時に反映する", async () => {
    const mutation = deferred<typeof advanceMutationResponse>();
    operatorApi.mutateRound.mockReturnValueOnce(mutation.promise);
    render(<OperatorPage />);
    const scheduleButton = await screen.findByRole("button", { name: "3秒後に開始予約" });
    fireEvent.click(scheduleButton);
    expect(operatorApi.mutateRound).toHaveBeenCalledOnce();
    await act(async () => {
      changeVisibility("hidden");
      mutation.resolve(advanceMutationResponse);
    });
    expect(operatorApi.getOperatorState).toHaveBeenCalledOnce();
    await act(async () => changeVisibility("visible"));
    expect(operatorApi.getOperatorState).toHaveBeenCalledTimes(2);
  });

  it("二重clickを1要求にし、不明な5xx後は同じidempotency keyで再送する", async () => {
    const user = userEvent.setup();
    let rejectFirst: (reason: unknown) => void = () => undefined;
    const pendingRequest = new Promise((_resolve, reject) => {
      rejectFirst = reject;
    });
    operatorApi.mutateRound
      .mockReturnValueOnce(pendingRequest)
      .mockResolvedValueOnce({
        operation: "schedule_round",
        event_id: ids.event,
        event_version: 5,
        round_id: ids.round,
        round_version: 4,
        round_state: "Scheduled",
        result_visibility: { distribution: true, personal_ranking: false, overall_ranking: false },
        open_at: "2026-08-08T01:00:05Z",
        standard_close_at: "2026-08-08T01:00:10Z",
        valid_answer_count: null,
        ranking_row_count: null,
        is_replay: true,
      });

    render(<OperatorPage />);
    const scheduleButton = await screen.findByRole("button", {
      name: "3秒後に開始予約",
    });
    fireEvent.click(scheduleButton);
    fireEvent.click(scheduleButton);
    expect(operatorApi.mutateRound).toHaveBeenCalledOnce();

    await act(async () => {
      rejectFirst(
        new ApiResponseError(503, {
          code: "database_temporarily_unavailable",
          message: "データベースに接続できません。",
        }),
      );
    });
    await screen.findByRole("alert");

    const firstKey = operatorApi.mutateRound.mock.calls[0]?.[2]?.idempotency_key;
    await user.click(scheduleButton);
    await waitFor(() => expect(operatorApi.mutateRound).toHaveBeenCalledTimes(2));
    expect(operatorApi.mutateRound.mock.calls[1]?.[2]?.idempotency_key).toBe(
      firstKey,
    );
  });

  it("自動集計に失敗したRoundだけ復旧操作を表示して専用endpointへ送る", async () => {
    const user = userEvent.setup();
    const voidCandidateState = progressionState("VoidCandidate");
    voidCandidateState.current_round!.result_publication = "Withheld";
    voidCandidateState.current_round!.result_failure_code =
      "automatic_aggregate_integrity_failure";
    const recoveredState = progressionState("AwaitingValidation");
    recoveredState.current_round!.state_version += 1;
    recoveredState.current_round!.result_publication = "Withheld";
    operatorApi.getOperatorState
      .mockResolvedValueOnce(voidCandidateState)
      .mockResolvedValueOnce(recoveredState);
    operatorApi.retryRoundResults.mockResolvedValue({
      ...advanceMutationResponse,
      operation: "retry_round_results",
      round_id: ids.round,
      round_state: "AwaitingValidation",
      valid_answer_count: null,
      ranking_row_count: null,
    });

    render(<OperatorPage />);
    const retryButton = await screen.findByRole("button", {
      name: "結果集計を再試行",
    });
    await user.click(retryButton);

    expect(operatorApi.retryRoundResults).toHaveBeenCalledWith(
      ids.round,
      expect.objectContaining({
        expected_version: 3,
        idempotency_key: expect.any(String),
      }),
    );
    await waitFor(() => expect(retryButton).not.toBeInTheDocument());
    expect(
      screen.getByText("結果の自動集計を再試行しました。"),
    ).toBeVisible();
  });

  it("問題配信前に6問から出題問題を切り替える", async () => {
    const user = userEvent.setup();
    const selectableState: OperatorState = {
      ...state,
      current_round: {
        ...state.current_round!,
        state: "PreparingAssets",
      },
    };
    const switchedState: OperatorState = {
      ...selectableState,
      event_version: selectableState.event_version + 1,
      current_round: {
        ...selectableState.current_round!,
        version: selectableState.current_round!.version + 1,
        state_version: selectableState.current_round!.state_version + 4,
        question: {
          ...selectableState.current_round!.question,
          question_id: ids.question2,
          version: 2,
          prompt: "[DEMO] 架空の会場の入口は何色？",
          options: selectableState.available_questions[1]!.options,
        },
      },
    };
    operatorApi.getOperatorState
      .mockResolvedValueOnce(selectableState)
      .mockResolvedValueOnce(switchedState);
    operatorApi.selectRoundQuestion.mockResolvedValue({
      operation: "select_round_question",
      event_id: ids.event,
      event_version: 5,
      round_id: ids.round,
      round_version: 4,
      round_state: "PreparingAssets",
      result_visibility: { distribution: true, personal_ranking: false, overall_ranking: false },
      open_at: null,
      standard_close_at: null,
      valid_answer_count: null,
      ranking_row_count: null,
      is_replay: false,
    });

    render(<OperatorPage />);
    await user.click(await screen.findByText("候補を確認"));
    const questionSwitcher = await screen.findByRole("group", {
      name: "プレビューする問題",
    });
    const questionButtons = within(questionSwitcher).getAllByRole("button");
    expect(questionButtons).toHaveLength(6);
    expect(screen.getByText(
      "最初の1問は練習問題です。練習の得点は正答発表時に確認でき、総合ランキングには加算されません。本番は第1問から表示し、出題数と終了のタイミングは運営が決定します。途中参加の方も総合ランキングに含まれます。",
    )).toBeVisible();
    expect(questionButtons[0]).toBeDisabled();
    expect(questionButtons[1]).toHaveAttribute("aria-pressed", "true");

    const nextPreview = screen.getByRole("region", {
      name: /登録問題 2.*架空の会場の入口は何色/,
    });
    for (const option of ["あか", "あお", "みどり", "きいろ"]) {
      expect(within(nextPreview).getByText(option)).toBeVisible();
    }
    expect(within(nextPreview).getByText("正解")).toBeVisible();
    expect(operatorApi.selectRoundQuestion).not.toHaveBeenCalled();

    expect(screen.getByRole("heading", { name: "[DEMO] 2 + 2 は？", level: 2 })).toBeVisible();

    await user.click(questionButtons[2]!);
    expect(
      screen.getByRole("region", { name: /登録問題 3.*架空の合言葉/ }),
    ).toBeVisible();
    expect(operatorApi.selectRoundQuestion).not.toHaveBeenCalled();

    await user.click(questionButtons[1]!);
    await user.click(
      screen.getByRole("button", { name: "登録問題 2へ切り替える" }),
    );

    expect(operatorApi.selectRoundQuestion).toHaveBeenCalledWith(
      ids.round,
      ids.question2,
      expect.objectContaining({
        expected_version: 3,
        idempotency_key: expect.stringMatching(
          /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u,
        ),
      }),
    );
    expect(await screen.findByRole("heading", { name: "[DEMO] 架空の会場の入口は何色？", level: 2 })).toBeVisible();
    expect(screen.getByText("候補を確認").closest("details")).not.toHaveAttribute("open");
    await user.click(screen.getByText("候補を確認"));
    expect(screen.getByRole("button", { name: /登録問題 2.*現在の出題/ })).toBeDisabled();
    expect(screen.getByRole("region", { name: /登録問題 3.*架空の合言葉/ })).toBeVisible();
  });

  it("問題配信後は次問を確認できても切替操作を許可しない", async () => {
    const user = userEvent.setup();
    const preparationStartedState: OperatorState = {
      ...state,
      current_round: {
        ...state.current_round!,
        state: "ReadyToSchedule",
      },
    };
    operatorApi.getOperatorState.mockResolvedValueOnce(preparationStartedState);

    render(<OperatorPage />);

    await user.click(await screen.findByText("候補を確認"));

    expect(
      await screen.findByRole("region", {
        name: /登録問題 2.*架空の会場の入口は何色/,
      }),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "登録問題 2へ切り替える" }),
    ).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "登録問題 2へ切り替える" }));
    expect(
      screen.getByText("問題の切替は、問題を確定して配信する前だけ行えます。"),
    ).toBeVisible();
    expect(operatorApi.selectRoundQuestion).not.toHaveBeenCalled();
  });

  it("Reveal後は使用済み問題を除外してpreviewし、確定時だけ新Roundへ進む", async () => {
    const user = userEvent.setup();
    const revealedState = progressionState("Reveal", [
      ids.question,
      ids.question2,
    ]);
    const advancedState: OperatorState = {
      ...state,
      event_state: "InProgress",
      event_version: revealedState.event_version + 1,
      used_question_ids: [ids.question, ids.question2, ids.question3],
      current_round: {
        ...state.current_round!,
        round_id: ids.round2,
        version: 4,
        state: "PreparingAssets",
        state_version: 16,
        question: {
          ...state.current_round!.question,
          question_id: ids.question3,
          version: 3,
          prompt: "[DEMO] 架空の合言葉は？",
          options: state.available_questions[2]!.options,
        },
        statistics: {
          answer_count: 0,
          average_response_ms: null,
          options: state.available_questions[2]!.options.map((option) => ({
            option_id: option.option_id,
            answer_count: 0,
            average_response_ms: null,
          })),
        },
      },
    };
    operatorApi.getOperatorState
      .mockResolvedValueOnce(revealedState)
      .mockResolvedValueOnce(advancedState);
    operatorApi.advanceRound.mockResolvedValue(advanceMutationResponse);

    render(<OperatorPage />);

    await user.click(await screen.findByText("候補を確認"));
    const switcher = await screen.findByRole("group", {
      name: "次のRoundで出題する問題",
    });
    expect(
      within(switcher).queryByRole("button", {
        name: /登録問題 2.*架空の会場の入口/,
      }),
    ).not.toBeInTheDocument();
    expect(
      within(switcher).getByRole("button", {
        name: /登録問題 3.*プレビュー中/,
      }),
    ).toBeVisible();
    expect(operatorApi.advanceRound).not.toHaveBeenCalled();

    await user.click(
      screen.getByRole("button", {
        name: "backupを検証して登録問題 3へ進む",
      }),
    );

    expect(operatorApi.advanceRound).toHaveBeenCalledWith(
      ids.round,
      ids.question3,
      expect.objectContaining({
        expected_version: 3,
        idempotency_key: expect.stringMatching(
          /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u,
        ),
      }),
    );
    expect(await screen.findByRole("heading", { name: "[DEMO] 架空の合言葉は？", level: 2 })).toBeVisible();
    expect(screen.getByText("候補を確認").closest("details")).not.toHaveAttribute("open");
    await user.click(screen.getByText("候補を確認"));
    expect(screen.getByRole("button", { name: /登録問題 3.*現在の出題/ })).toBeDisabled();
    expect(screen.getByRole("region", { name: /登録問題 4.*最初に点灯するライト/ })).toBeVisible();
    const nextRoundSwitcher = screen.getByRole("group", {
      name: "プレビューする問題",
    });
    expect(
      within(nextRoundSwitcher).queryByRole("button", {
        name: /登録問題 1.*2 \+ 2/,
      }),
    ).not.toBeInTheDocument();
    expect(
      within(nextRoundSwitcher).queryByRole("button", {
        name: /登録問題 2.*架空の会場の入口/,
      }),
    ).not.toBeInTheDocument();
  });

  it("新Round操作の二重clickを抑止し、5xx後は同じidempotency keyで再送する", async () => {
    const user = userEvent.setup();
    const revealedState = progressionState("Reveal");
    let rejectFirst: (reason: unknown) => void = () => undefined;
    const pendingRequest = new Promise((_resolve, reject) => {
      rejectFirst = reject;
    });
    operatorApi.getOperatorState.mockResolvedValue(revealedState);
    operatorApi.advanceRound
      .mockReturnValueOnce(pendingRequest)
      .mockResolvedValueOnce({
        ...advanceMutationResponse,
        is_replay: true,
      });

    render(<OperatorPage />);
    await user.click(await screen.findByText("候補を確認"));
    const advanceButton = await screen.findByRole("button", {
      name: "backupを検証して登録問題 2へ進む",
    });
    fireEvent.click(advanceButton);
    fireEvent.click(advanceButton);
    expect(operatorApi.advanceRound).toHaveBeenCalledOnce();

    await act(async () => {
      rejectFirst(
        new ApiResponseError(503, {
          code: "backup_temporarily_unavailable",
          message: "backupを検証できませんでした。",
        }),
      );
    });
    await screen.findByRole("alert");

    const firstKey = operatorApi.advanceRound.mock.calls[0]?.[2]?.idempotency_key;
    await user.click(advanceButton);
    await waitFor(() => expect(operatorApi.advanceRound).toHaveBeenCalledTimes(2));
    expect(operatorApi.advanceRound.mock.calls[1]?.[2]?.idempotency_key).toBe(
      firstKey,
    );
  });

  it("backup処理中は次操作を隠し、失敗時は理由と再検証操作を表示する", async () => {
    const user = userEvent.setup();
    const pendingState = progressionState("RoundBackupPending");
    operatorApi.getOperatorState.mockResolvedValueOnce(pendingState);

    const { unmount } = render(<OperatorPage />);
    expect(
      await screen.findByText(/backupを作成・検証しています/),
    ).toBeVisible();
    expect(
      screen.queryByRole("group", { name: "次のRoundで出題する問題" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Eventを完了" }),
    ).not.toBeInTheDocument();
    unmount();

    const failedState = progressionState("RoundBackupFailed");
    operatorApi.getOperatorState.mockResolvedValueOnce(failedState);
    render(<OperatorPage />);

    expect(
      await screen.findByText(/backup検証に失敗しました/),
    ).toBeVisible();
    await user.click(screen.getByText("候補を確認"));
    expect(
      screen.getByRole("button", {
        name: "backupを検証して登録問題 2へ進む",
      }),
    ).toBeEnabled();
  });

  it.each([true, false])("全問出題済みが%sでも運営の判断で完了でき、Completed後は操作を隠す", async (allUsed) => {
    const allQuestionIds = state.available_questions.map(
      (question) => question.question_id,
    );
    const finalState = progressionState("Reveal", allUsed ? allQuestionIds : [ids.question]);
    const completedState: OperatorState = {
      ...finalState,
      event_state: "Completed",
      award_ceremony_phase: "Waiting",
      event_version: finalState.event_version + 1,
      current_round: {
        ...finalState.current_round!,
        version: finalState.current_round!.version + 1,
        state_version: finalState.current_round!.state_version + 4,
        state: "RoundBackupValidated",
      },
    };
    operatorApi.getOperatorState
      .mockResolvedValueOnce(finalState)
      .mockResolvedValueOnce(completedState);
    operatorApi.completeEvent.mockResolvedValue({
      ...advanceMutationResponse,
      operation: "complete_event",
      round_id: ids.round,
      round_state: "RoundBackupValidated",
    });

    render(<OperatorPage />);
    expect(
      await screen.findByRole("heading", {
        name: "イベントの終了",
      }),
    ).toBeVisible();
    const nextButton = screen.queryByRole("button", { name: "backupを検証して登録問題 2へ進む" });
    if (allUsed) expect(nextButton).not.toBeInTheDocument();
    else expect(nextButton).toBeEnabled();
    expect(screen.queryByText("すべての問題が終了しました")).not.toBeInTheDocument();

    await screen.findByText("この問題は最終問題に指定されていません。");
    fireEvent.click(screen.getByRole("button", { name: "イベントを終了…" }));
    const dialog = screen.getByRole("dialog", { name: "イベントを終了しますか？" });
    const completeButton = within(dialog).getByRole("button", { name: "イベントを終了する" });
    expect(completeButton).toBeDisabled();
    fireEvent.click(completeButton);
    expect(operatorApi.completeEvent).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "終了後は次の問題へ進めないことを確認しました" }));
    fireEvent.click(completeButton);
    fireEvent.click(completeButton);
    expect(operatorApi.completeEvent).toHaveBeenCalledOnce();
    expect(operatorApi.completeEvent).toHaveBeenCalledWith(
      ids.round,
      expect.objectContaining({
        expected_version: 3,
        idempotency_key: expect.any(String),
      }),
    );
    expect(
      await screen.findByText(
        "Eventは完了しました。次の問題へ進む操作はありません。",
      ),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "イベントを終了…" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /backupを検証して/ }),
    ).not.toBeInTheDocument();
  });

  it("順位の一括変更を一度だけ送り、結果不明の再送で同じkeyを使う", async () => {
    const user = userEvent.setup();
    let rejectFirst: (reason: unknown) => void = () => undefined;
    operatorApi.setResultVisibility
      .mockReturnValueOnce(new Promise((_resolve, reject) => { rejectFirst = reject; }))
      .mockResolvedValueOnce({
        ...advanceMutationResponse,
        operation: "set_result_visibility",
        result_visibility: { distribution: true, personal_ranking: true, overall_ranking: true },
      });
    render(<OperatorPage />);
    const showButton = await screen.findByRole("button", { name: "自分の順位・みんなの順位", pressed: false });

    fireEvent.click(showButton);
    fireEvent.click(showButton);
    expect(operatorApi.setResultVisibility).toHaveBeenCalledOnce();
    expect(operatorApi.setResultVisibility.mock.calls[0]).toEqual([
      "rankings",
      true,
      expect.objectContaining({ expected_version: state.event_version }),
    ]);
    expect(showButton).toBeDisabled();

    await act(async () => {
      rejectFirst(new ApiResponseError(503, {
        code: "database_temporarily_unavailable",
        message: "結果を確認できません。",
      }));
    });
    await screen.findByRole("alert");
    await user.click(showButton);
    await waitFor(() => expect(operatorApi.setResultVisibility).toHaveBeenCalledTimes(2));
    expect(operatorApi.setResultVisibility.mock.calls[1]?.[2]?.idempotency_key).toBe(
      operatorApi.setResultVisibility.mock.calls[0]?.[2]?.idempotency_key,
    );
    expect(await screen.findByText("自分の順位・みんなの順位を表示にしました。")).toBeVisible();
  });

  it("パスワードは平文表示せず伏せ字入力としてログインへ送る", async () => {
    const user = userEvent.setup();
    operatorApi.getOperatorSession.mockRejectedValueOnce(
      new ApiResponseError(401, {
        code: "operator_session_invalid",
        message: "ログインしてください。",
      }),
    );

    render(<OperatorPage />);
    const passwordInput = await screen.findByLabelText("パスワード");
    expect(passwordInput).toHaveAttribute("type", "password");

    const usernameInput = screen.getByLabelText("ユーザー名");
    expect(usernameInput).toHaveValue("");
    await user.type(usernameInput, "operator");
    await user.type(passwordInput, "local-secret");
    await user.click(screen.getByRole("button", { name: "ログイン" }));

    expect(operatorApi.loginOperator).toHaveBeenCalledWith({
      username: "operator",
      password: "local-secret",
    });
  });

  it("ログインは空欄送信を止め、失敗を表示し、再試行中の二重送信を防ぐ", async () => {
    const user = userEvent.setup();
    const retry = deferred<OperatorSession>();
    operatorApi.getOperatorSession.mockRejectedValueOnce(
      new ApiResponseError(401, {
        code: "operator_session_invalid",
        message: "ログインしてください。",
      }),
    );
    operatorApi.loginOperator
      .mockRejectedValueOnce(new ApiResponseError(401, {
        code: "invalid_credentials",
        message: "ユーザー名またはパスワードを確認してください。",
      }))
      .mockReturnValueOnce(retry.promise);
    render(<OperatorPage />);

    const username = await screen.findByLabelText("ユーザー名");
    const password = screen.getByLabelText("パスワード");
    expect(username).toHaveValue("");
    expect(password).toHaveValue("");
    await user.click(screen.getByRole("button", { name: "ログイン" }));
    expect(operatorApi.loginOperator).not.toHaveBeenCalled();

    await user.type(username, "operator");
    await user.type(password, "local-secret");
    await user.click(screen.getByRole("button", { name: "ログイン" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("ユーザー名またはパスワードを確認してください。");
    expect(username).toHaveValue("operator");
    expect(password).toHaveAttribute("type", "password");
    expect(screen.queryByText("local-secret")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "ログイン" }));
    const busyButton = screen.getByRole("button", { name: "確認しています…" });
    expect(busyButton).toBeDisabled();
    await user.click(busyButton);
    expect(operatorApi.loginOperator).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    await act(async () => {
      retry.resolve({ operator: { account_id: ids.account, username: "operator", role: "Operator" } });
    });
    expect(await screen.findByRole("heading", { name: "運営", level: 1 })).toBeVisible();
    expect(screen.queryByLabelText("パスワード")).not.toBeInTheDocument();
  });
});
