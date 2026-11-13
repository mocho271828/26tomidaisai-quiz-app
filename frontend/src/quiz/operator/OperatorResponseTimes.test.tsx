import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { OperatorResponseTimes as ResponseTimeData } from "../../api/operatorResponseTimeContracts";
import { OperatorResponseTimes } from "./OperatorResponseTimes";
import { useOperatorResponseTimes } from "./useOperatorResponseTimes";

vi.mock("./useOperatorResponseTimes", () => ({ useOperatorResponseTimes: vi.fn() }));

const practiceRoundId = "71000000-0000-4000-8000-000000000001";
const roundId = "71000000-0000-4000-8000-000000000002";
const emptyGroup: ResponseTimeData["all_answers"] = {
  answer_count: 0,
  average_response_ms: null,
  median_response_ms: null,
  fastest_response_ms: null,
  slowest_response_ms: null,
  rows: [],
};
const data: ResponseTimeData = {
  timing_basis: "client_monotonic_v1",
  rounds: [
    { round_id: practiceRoundId, round_number: 1, is_practice: true, prompt: "練習問題" },
    { round_id: roundId, round_number: 2, is_practice: false, prompt: "本番問題" },
  ],
  selected_round_id: roundId,
  all_answers: {
    answer_count: 4,
    average_response_ms: 1500.5,
    median_response_ms: 1250.5,
    fastest_response_ms: 1000,
    slowest_response_ms: 2501,
    rows: [
      { rank: 1, public_label: "AAA", response_ms: 1000, is_correct: false },
      { rank: 1, public_label: "AAB", response_ms: 1000, is_correct: true },
      { rank: 3, public_label: "AAC", response_ms: 1501, is_correct: true },
      { rank: 4, public_label: "BAD", response_ms: 2501, is_correct: true },
    ],
  },
  correct_answers: {
    answer_count: 3,
    average_response_ms: 5002 / 3,
    median_response_ms: 1501,
    fastest_response_ms: 1000,
    slowest_response_ms: 2501,
    rows: [
      { rank: 1, public_label: "AAB", response_ms: 1000, is_correct: true },
      { rank: 2, public_label: "AAC", response_ms: 1501, is_correct: true },
      { rank: 3, public_label: "BAD", response_ms: 2501, is_correct: true },
    ],
  },
};

function mockState(overrides: Partial<ReturnType<typeof useOperatorResponseTimes>> = {}) {
  const state = {
    status: "ready" as const,
    data,
    roundId: null,
    selectRound: vi.fn(),
    retry: vi.fn(),
    ...overrides,
  };
  vi.mocked(useOperatorResponseTimes).mockReturnValue(state);
  return state;
}

function statistics(): HTMLElement {
  return screen.getByRole("region", { name: "回答速度" }).querySelector("dl")!;
}

describe("OperatorResponseTimes", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockState();
  });

  it("確定済み問題の全回答者順位・5指標を表示し、同順位とミリ秒精度を保持する", () => {
    render(<OperatorResponseTimes snapshotVersion={8} />);

    expect(useOperatorResponseTimes).toHaveBeenCalledWith(8);
    expect(screen.getByRole("heading", { name: "第1問 · 本番問題" })).toBeVisible();
    expect(screen.getByRole("button", { name: "全回答者" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("combobox", { name: "問題" })).toHaveValue("");
    expect(screen.getByRole("option", { name: "第1問（練習）" })).toBeInTheDocument();
    expect(within(statistics()).getByText("4件")).toBeVisible();
    expect(within(statistics()).getByText("1.501秒")).toBeVisible();
    expect(within(statistics()).getByText("1.251秒")).toBeVisible();
    expect(within(statistics()).getByText("1.000秒")).toBeVisible();
    expect(within(statistics()).getByText("2.501秒")).toBeVisible();
    expect(screen.getByRole("region", { name: "回答速度ランキング一覧" })).toHaveAttribute("tabindex", "0");

    const table = screen.getByRole("table", { name: "全回答者の回答速度ランキング" });
    expect(within(table).getAllByRole("columnheader")).toHaveLength(4);
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((row) => within(row).getAllByRole("cell")[0]?.textContent)).toEqual([
      "1位", "1位", "3位", "4位",
    ]);
    expect(within(rows[0]!).getByRole("cell", { name: "誤答" })).toBeVisible();
    expect(screen.queryByRole("combobox", { name: "回答方法" })).not.toBeInTheDocument();
    expect(screen.getByText(/採点上の速度順位とは別/)).toBeVisible();
  });

  it("正答者の順位と統計へ切り替え、元の全回答者順位に戻れる", () => {
    render(<OperatorResponseTimes snapshotVersion={8} />);

    fireEvent.click(screen.getByRole("button", { name: "正答者のみ" }));
    expect(screen.getByRole("button", { name: "正答者のみ" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("rowheader", { name: "AAA" })).not.toBeInTheDocument();
    expect(screen.getByRole("table", { name: "正答者の回答速度ランキング" })).toBeVisible();
    expect(within(statistics()).getByText("3件")).toBeVisible();
    const badRow = screen.getByRole("rowheader", { name: "BAD" }).closest("tr")!;
    expect(within(badRow).getByRole("cell", { name: "3 位" })).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "全回答者" }));
    expect(screen.getByRole("rowheader", { name: "AAA" })).toBeVisible();
    expect(within(statistics()).getByText("4件")).toBeVisible();
    expect(screen.getByRole("cell", { name: "4 位" })).toBeVisible();
  });

  it("問題の選択をhookへ渡し、最新問題にも戻れる", () => {
    const state = mockState();
    render(<OperatorResponseTimes snapshotVersion={8} />);

    fireEvent.change(screen.getByRole("combobox", { name: "問題" }), { target: { value: practiceRoundId } });
    expect(state.selectRound).toHaveBeenLastCalledWith(practiceRoundId);
    fireEvent.change(screen.getByRole("combobox", { name: "問題" }), { target: { value: "" } });
    expect(state.selectRound).toHaveBeenLastCalledWith(null);
  });

  it("問題切替で読み込む間も検索と表示対象の選択を維持する", () => {
    const { rerender } = render(<OperatorResponseTimes snapshotVersion={8} />);
    fireEvent.click(screen.getByRole("button", { name: "正答者のみ" }));
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "bad" } });

    mockState({ status: "loading", data: null, roundId: practiceRoundId });
    rerender(<OperatorResponseTimes snapshotVersion={8} />);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    mockState({ data: { ...data, selected_round_id: practiceRoundId }, roundId: practiceRoundId });
    rerender(<OperatorResponseTimes snapshotVersion={8} />);
    expect(screen.getByRole("searchbox")).toHaveValue("bad");
    expect(screen.getByRole("button", { name: "正答者のみ" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("rowheader", { name: "BAD" })).toBeVisible();
    expect(screen.queryByRole("rowheader", { name: "AAB" })).not.toBeInTheDocument();
  });

  it("IDの部分検索で元の順位・統計を保持し、検索0件と全件復帰を表示する", () => {
    render(<OperatorResponseTimes snapshotVersion={8} />);
    const search = screen.getByRole("searchbox", { name: "回答速度を参加者IDで絞り込み" });
    expect(search).toHaveAttribute("maxlength", "3");

    fireEvent.change(search, { target: { value: "ad" } });
    expect(screen.getByRole("status")).toHaveTextContent("1 / 4人");
    expect(screen.getByRole("rowheader", { name: "BAD" })).toBeVisible();
    expect(screen.getByRole("cell", { name: "4 位" })).toBeVisible();
    expect(within(statistics()).getByText("4件")).toBeVisible();
    expect(within(statistics()).getByText("1.501秒")).toBeVisible();

    fireEvent.change(search, { target: { value: "zzz" } });
    expect(screen.getByRole("status")).toHaveTextContent("0 / 4人");
    expect(screen.getByText("該当するIDはありません。")).toBeVisible();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    fireEvent.change(search, { target: { value: "" } });
    expect(screen.getByRole("status")).toHaveTextContent("全4人");
    expect(screen.getAllByRole("rowheader")).toHaveLength(4);
  });

  it("未確定・読込中・取得失敗を区別し、失敗から再試行できる", () => {
    mockState({ status: "idle", data: null });
    const { rerender } = render(<OperatorResponseTimes snapshotVersion={null} />);
    expect(screen.getByText("問題の結果が確定すると表示します。")).toBeVisible();

    mockState({ status: "loading", data: null, roundId: practiceRoundId });
    rerender(<OperatorResponseTimes snapshotVersion={8} />);
    expect(screen.getByRole("status")).toHaveTextContent("回答速度を読み込んでいます…");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "問題" })).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "問題" })).toHaveValue(practiceRoundId);
    expect(screen.getByRole("option", { name: "選択した問題" })).toBeInTheDocument();

    const state = mockState({ status: "error", data: null, roundId: practiceRoundId });
    rerender(<OperatorResponseTimes snapshotVersion={8} />);
    expect(screen.getByRole("alert")).toHaveTextContent("回答速度を取得できませんでした。");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "再試行" }));
    expect(state.retry).toHaveBeenCalledOnce();
    expect(screen.getByRole("combobox", { name: "問題" })).toBeEnabled();
    fireEvent.change(screen.getByRole("combobox", { name: "問題" }), { target: { value: "" } });
    expect(state.selectRound).toHaveBeenCalledWith(null);
  });

  it("確定問題なし・回答0件・正答者0件で時間を0秒と表示しない", () => {
    mockState({ data: { ...data, rounds: [], selected_round_id: null, all_answers: emptyGroup, correct_answers: emptyGroup } });
    const { rerender } = render(<OperatorResponseTimes snapshotVersion={8} />);
    expect(screen.getByText("確定済みの問題はまだありません。")).toBeVisible();
    expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();

    mockState({ data: { ...data, all_answers: emptyGroup, correct_answers: emptyGroup } });
    rerender(<OperatorResponseTimes snapshotVersion={8} />);
    expect(screen.getByText("この条件に一致する回答はありません。")).toBeVisible();
    expect(within(statistics()).getByText("0件")).toBeVisible();
    expect(within(statistics()).getAllByText("回答なし")).toHaveLength(4);
    expect(screen.queryByText("0.000秒")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "正答者のみ" }));
    expect(screen.getByText("この条件に一致する正答者はいません。")).toBeVisible();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("600人の全行を表示し、末尾の参加者も元の順位で検索できる", () => {
    const rows = Array.from({ length: 600 }, (_, index) => ({
      rank: index + 1,
      public_label: `A${String.fromCharCode(65 + Math.floor(index / 26))}${String.fromCharCode(65 + index % 26)}`,
      response_ms: index,
      is_correct: true,
    }));
    mockState({ data: { ...data, all_answers: { ...data.all_answers, answer_count: 600, rows } } });
    render(<OperatorResponseTimes snapshotVersion={8} />);
    expect(screen.getAllByRole("rowheader")).toHaveLength(600);

    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "axb" } });
    expect(screen.getByRole("rowheader", { name: "AXB" })).toBeVisible();
    expect(screen.getByRole("cell", { name: "600 位" })).toBeVisible();
    expect(screen.getByRole("cell", { name: "0.599秒" })).toBeVisible();
  });
});
