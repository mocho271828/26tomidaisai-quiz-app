import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type {
  OperatorLatestRanking,
  OperatorQuestion,
  OperatorRoundStatistics,
} from "../../api/operatorContracts";
import { OperatorStatistics } from "./OperatorStatistics";

const ids = {
  round: "70000000-0000-4000-8000-000000000001",
  optionA: "70000000-0000-4000-8000-000000000002",
  optionB: "70000000-0000-4000-8000-000000000003",
} as const;

const options: OperatorQuestion["options"] = [
  { option_id: ids.optionA, label: "A", content: "3", is_correct: false },
  { option_id: ids.optionB, label: "B", content: "4", is_correct: true },
];
const statistics: OperatorRoundStatistics = {
  answer_count: 3,
  average_response_ms: 1250,
  options: [
    { option_id: ids.optionA, answer_count: 1, average_response_ms: 1000 },
    { option_id: ids.optionB, answer_count: 2, average_response_ms: 1375 },
  ],
};
const emptyStatistics: OperatorRoundStatistics = {
  answer_count: 0,
  average_response_ms: null,
  options: options.map((option) => ({
    option_id: option.option_id,
    answer_count: 0,
    average_response_ms: null,
  })),
};
const latestRanking: OperatorLatestRanking = {
  snapshot_version: 2,
  round_id: ids.round,
  rows: [
    { overall_rank: 1, public_label: "AAA", total_points: 10500, correct_count: 1 },
    { overall_rank: 1, public_label: "AAB", total_points: 10500, correct_count: 1 },
    { overall_rank: 3, public_label: "AAC", total_points: 10000, correct_count: 1 },
    { overall_rank: 4, public_label: "BAD", total_points: 0, correct_count: 0 },
  ],
};

function summary(): HTMLElement {
  return screen.getByRole("region", { name: "回答分布" }).querySelector("dl")!;
}

function ranking(): HTMLElement {
  return screen.getByRole("region", { name: "総合ランキング" });
}

describe("OperatorStatistics", () => {
  it("回答概要を回答分布へ統合し、実データの割合・平均と全件の総合順位を表示する", () => {
    const { container } = render(
      <OperatorStatistics
        isResultFinalized
        options={options}
        rankingState={{ status: "ready", latestRanking }}
        statistics={statistics}
      />,
    );

    expect(container.children).toHaveLength(2);
    expect(summary()).toHaveAttribute("aria-live", "polite");
    expect(summary()).toHaveAttribute("aria-atomic", "true");
    expect(within(summary()).getByText("3件")).toBeVisible();
    expect(within(summary()).getByText("1.25秒")).toBeVisible();
    expect(within(summary()).getByText("67%")).toBeVisible();

    const distribution = screen.getByRole("region", { name: "回答分布" });
    expect(within(distribution).getByText("確定")).toBeVisible();
    const optionRows = within(distribution).getAllByRole("listitem");
    expect(within(optionRows[0]!).getByText("1件")).toBeVisible();
    expect(within(optionRows[0]!).getByText("平均回答時間 1.00秒")).toBeVisible();
    expect(within(optionRows[0]!).getByText("33%")).toBeVisible();
    expect(within(optionRows[1]!).getByText("2件")).toBeVisible();
    expect(within(optionRows[1]!).getByText("平均回答時間 1.38秒")).toBeVisible();
    expect(within(optionRows[1]!).getByText("正解")).toBeVisible();
    expect(optionRows[1]!.querySelector("[aria-hidden] > span")).toHaveStyle({ width: "67%" });

    expect(ranking()).not.toHaveAttribute("aria-live");
    expect(within(ranking()).getByRole("region", { name: "総合ランキング一覧" }))
      .toHaveAttribute("tabindex", "0");
    expect(within(ranking()).getByText("全4人")).toBeVisible();
    const table = within(ranking()).getByRole("table", { name: "順位別の合計得点" });
    expect(within(table).getAllByRole("columnheader")).toHaveLength(4);
    const rankingRows = within(table).getAllByRole("row").slice(1);
    expect(rankingRows).toHaveLength(4);
    expect(within(rankingRows[0]!).getByRole("cell", { name: "10,500 点" })).toBeVisible();
    expect(within(rankingRows[1]!).getByRole("cell", { name: "1 位" })).toBeVisible();
    for (const rankingRow of rankingRows.slice(0, 3)) {
      expect(rankingRow).toHaveClass("operator-ranking-top");
    }
    expect(rankingRows[3]).not.toHaveClass("operator-ranking-top");
  });

  it("回答とrankingがない状態を0msと誤表示しない", () => {
    render(
      <OperatorStatistics
        isResultFinalized={false}
        options={options}
        rankingState={{ status: "idle", latestRanking: null }}
        statistics={emptyStatistics}
      />,
    );

    expect(within(summary()).getByText("0件")).toBeVisible();
    expect(within(summary()).getByText("回答なし")).toBeVisible();
    expect(within(summary()).getByText("未確定")).toBeVisible();
    expect(screen.getByText("回答が保存されると、件数と平均速度を表示します。")).toBeVisible();
    expect(screen.queryByText(/0\.00秒/)).not.toBeInTheDocument();
    expect(within(ranking()).getByText("今回の順位は未確定です。")).toBeVisible();
  });

  it("前問のrankingがあっても現在の回答分布を確定と表示しない", () => {
    render(
      <OperatorStatistics
        isResultFinalized={false}
        options={options}
        rankingState={{ status: "ready", latestRanking }}
        statistics={statistics}
      />,
    );

    expect(within(summary()).getByText("未確定")).toBeVisible();
    expect(within(summary()).queryByText("67%")).not.toBeInTheDocument();
    expect(screen.getByRole("rowheader", { name: "AAA" })).toBeVisible();
  });

  it("rankingの読込中・取得失敗・ready空状態を区別する", () => {
    const commonProps = { isResultFinalized: true, options, statistics: emptyStatistics };
    const { rerender } = render(
      <OperatorStatistics
        {...commonProps}
        rankingState={{ status: "loading", latestRanking: null }}
      />,
    );

    expect(within(ranking()).getByRole("status")).toHaveTextContent("総合ランキングを読み込んでいます…");
    expect(within(ranking()).queryByText("今回の順位は未確定です。")).not.toBeInTheDocument();

    rerender(
      <OperatorStatistics
        {...commonProps}
        rankingState={{ status: "error", latestRanking: null }}
      />,
    );
    expect(within(ranking()).getByRole("alert")).toHaveTextContent(
      "総合ランキングを取得できませんでした。時間をおいて画面を再読み込みしてください。",
    );

    rerender(
      <OperatorStatistics
        {...commonProps}
        rankingState={{ status: "ready", latestRanking: { ...latestRanking, rows: [] } }}
      />,
    );
    expect(within(ranking()).getByText("総合ランキングはまだありません。")).toBeVisible();
    expect(within(ranking()).queryByRole("searchbox")).not.toBeInTheDocument();
  });

  it("IDを大文字小文字に関係なく部分一致で絞り込み、元の順位を保持する", () => {
    render(
      <OperatorStatistics
        isResultFinalized
        options={options}
        rankingState={{ status: "ready", latestRanking }}
        statistics={statistics}
      />,
    );
    const search = screen.getByRole("searchbox", { name: "参加者IDで絞り込み" });
    fireEvent.change(search, { target: { value: "ad" } });
    expect(screen.getByRole("status")).toHaveTextContent("1 / 4人");
    expect(screen.getByRole("rowheader", { name: "BAD" })).toBeVisible();
    expect(screen.getByRole("cell", { name: "4 位" })).toBeVisible();
    expect(screen.queryByRole("rowheader", { name: "AAA" })).not.toBeInTheDocument();

    fireEvent.change(search, { target: { value: "zzz" } });
    expect(screen.getByRole("status")).toHaveTextContent("0 / 4人");
    expect(screen.getByText("該当するIDはありません。")).toBeVisible();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();

    fireEvent.change(search, { target: { value: "" } });
    expect(screen.getByRole("status")).toHaveTextContent("全4人");
    expect(screen.getAllByRole("rowheader")).toHaveLength(4);
    expect(screen.queryByText("該当するIDはありません。")).not.toBeInTheDocument();
  });

  it("600人のランキングを省略せず表示し、末尾のIDを検索できる", () => {
    const rows = Array.from({ length: 600 }, (_, index) => ({
      overall_rank: index + 1,
      public_label: `A${String.fromCharCode(65 + Math.floor(index / 26))}${String.fromCharCode(65 + index % 26)}`,
      total_points: 600 - index,
      correct_count: 1,
    }));
    render(
      <OperatorStatistics
        isResultFinalized
        options={options}
        rankingState={{ status: "ready", latestRanking: { ...latestRanking, rows } }}
        statistics={statistics}
      />,
    );

    expect(screen.getAllByRole("rowheader")).toHaveLength(600);
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "axb" } });
    expect(screen.getByRole("rowheader", { name: "AXB" })).toBeVisible();
    expect(screen.getByRole("cell", { name: "600 位" })).toBeVisible();
  });

  it("選択肢なし・確定後の回答0件を区別する", () => {
    render(
      <OperatorStatistics
        isResultFinalized
        options={[]}
        rankingState={{ status: "ready", latestRanking: { ...latestRanking, rows: [] } }}
        statistics={{ ...emptyStatistics, options: [] }}
      />,
    );

    expect(screen.getByText("選択肢はありません。")).toBeVisible();
    expect(within(summary()).getAllByText("回答なし")).toHaveLength(2);
    expect(screen.queryByText("未確定")).not.toBeInTheDocument();
    expect(screen.queryByText(/0\.00秒/)).not.toBeInTheDocument();
  });
});
