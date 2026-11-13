import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { OperatorLatestRanking, OperatorState } from "../../api/operatorContracts";
import type { OperatorResponseTimes } from "../../api/operatorResponseTimeContracts";
import { ModeratorResults } from "./ModeratorResults";

const roundId = "73000000-0000-4000-8000-000000000001";
const previousRoundId = "73000000-0000-4000-8000-000000000002";
const options = [
  { option_id: "73000000-0000-4000-8000-000000000003", label: "A", content: "富山市", is_correct: false },
  { option_id: "73000000-0000-4000-8000-000000000004", label: "B", content: "高岡市", is_correct: true },
];
const round: NonNullable<OperatorState["current_round"]> = {
  round_id: roundId,
  version: 8,
  state_version: 12,
  state: "ReadyToReveal",
  open_at: "2026-10-22T12:00:00+09:00",
  standard_close_at: "2026-10-22T12:00:10+09:00",
  result_publication: "Withheld",
  can_publish_results: true,
  result_failure_code: null,
  question: {
    question_id: "73000000-0000-4000-8000-000000000005",
    version: 1,
    prompt: "会場がある市は？",
    options,
  },
  statistics: {
    answer_count: 4,
    average_response_ms: 1142,
    options: [
      { option_id: options[0]!.option_id, answer_count: 1, average_response_ms: 100 },
      { option_id: options[1]!.option_id, answer_count: 3, average_response_ms: 4468 / 3 },
    ],
  },
};
const manualVisibility = { mode: "manual", initially_visible: false, changes: [] } as const;
const projection: OperatorState = {
    award_ceremony_phase: null,
  event_id: "73000000-0000-4000-8000-000000000006",
  projected_at: "2026-10-22T12:00:15+09:00",
  event_title: "富大祭クイズ",
  event_state: "InProgress",
  event_version: 30,
  start_delay_seconds: 5,
  result_visibility: { distribution: false, personal_ranking: false, overall_ranking: false },
  visibility_controls: {
    distribution: { ...manualVisibility, changes: [] },
    personal_ranking: { ...manualVisibility, changes: [] },
    overall_ranking: { ...manualVisibility, changes: [] },
  },
  participant_count: 4,
  available_questions: [],
  used_question_ids: [round.question.question_id],
  current_round: round,
  latest_ranking_snapshot_version: 6,
};
const ranking: OperatorLatestRanking = {
  round_id: roundId,
  snapshot_version: 6,
  rows: [
    { overall_rank: 1, public_label: "ONE", total_points: 10000, correct_count: 6 },
    { overall_rank: 2, public_label: "TWO", total_points: 9000, correct_count: 5 },
    { overall_rank: 3, public_label: "THR", total_points: 8000, correct_count: 4 },
    { overall_rank: 4, public_label: "FOU", total_points: 7000, correct_count: 3 },
    { overall_rank: 5, public_label: "FIV", total_points: 6000, correct_count: 2 },
    { overall_rank: 5, public_label: "SIX", total_points: 6000, correct_count: 2 },
    { overall_rank: 7, public_label: "SEV", total_points: 5000, correct_count: 1 },
  ],
};

function show(overrides: Partial<Parameters<typeof ModeratorResults>[0]> = {}) {
  return render(<ModeratorResults responseTimes={null} projection={projection} ranking={ranking} {...overrides} />);
}

function distribution(): HTMLElement {
  return screen.getByRole("region", { name: "回答分布" });
}

describe("ModeratorResults", () => {
  it.each(["Open", "AwaitingValidation", "Voided"] as const)("%sでは未確定の回答分布を表示しない", (state) => {
    show({ projection: { ...projection, current_round: { ...round, state } } });

    expect(within(distribution()).getByText("集計完了をお待ちください。")).toBeVisible();
    expect(within(distribution()).queryByRole("list")).not.toBeInTheDocument();
    expect(within(distribution()).queryByText("回答はありません。")).not.toBeInTheDocument();
  });

  it("問題がない状態を回答0件に補完しない", () => {
    show({ projection: { ...projection, current_round: null } });

    expect(within(distribution()).queryByText("回答はありません。")).not.toBeInTheDocument();
  });

  it("集計済みの回答なしを明示し、割合を作らない", () => {
    show({
      projection: {
        ...projection,
        current_round: {
          ...round,
          statistics: { answer_count: 0, average_response_ms: null,
            options: options.map((option) => ({ option_id: option.option_id, answer_count: 0, average_response_ms: null })) },
        },
      },
    });

    expect(within(distribution()).getByText("回答はありません。")).toBeVisible();
    expect(within(distribution()).queryByText(/NaN|%/)).not.toBeInTheDocument();
  });

  it("確定した現在問題の選択肢と回答数・割合を対応させる", () => {
    show({ projection: { ...projection, current_round: { ...round, state: "RoundBackupValidated", result_publication: "Published" } } });

    const rows = within(distribution()).getAllByRole("listitem");
    expect(rows[0]!).toHaveAccessibleName("A 富山市");
    expect(within(rows[0]!).getByText("1件")).toBeVisible();
    expect(within(rows[0]!).getByText("25%")).toBeVisible();
    expect(rows[1]!).toHaveAccessibleName("B 高岡市");
    expect(within(rows[1]!).getByText("3件")).toBeVisible();
    expect(within(rows[1]!).getByText("75%")).toBeVisible();
  });

  it("正解公開・最終指定・開催終了を待たず5位までの全員を表示する", () => {
    show();

    const table = screen.getByRole("table", { name: "順位・参加者ID・得点" });
    expect(screen.getByRole("region", { name: "ランキング TOP5" })).toBeVisible();
    expect(within(table).getAllByRole("rowheader").map((row) => row.textContent)).toEqual(["ONE", "TWO", "THR", "FOU", "FIV"]);
    expect(within(table).getAllByRole("cell", { name: "5位" })).toHaveLength(1);
    expect(within(table).getByRole("cell", { name: "10,000点" })).toBeVisible();
    expect(within(table).queryByText("SEV")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "次へ" }));
    expect(within(table).getByRole("rowheader", { name: "SIX" })).toBeVisible();
    expect(within(table).getByRole("cell", { name: "5位" })).toBeVisible();
    expect(screen.getByRole("button", { name: "次へ" })).toBeDisabled();
  });

  it("開催終了後もランキングを表示する", () => {
    show({ projection: { ...projection, event_state: "Completed" } });
    expect(screen.getByRole("table", { name: "順位・参加者ID・得点" })).toBeVisible();
  });

  it.each(["ReadyToSchedule", "Open", "AwaitingValidation", "Voided"] as const)("次問が%sでも同じ集計版の確定ランキングを保持する", (state) => {
    const { rerender } = show();
    rerender(<ModeratorResults responseTimes={null}
      projection={{ ...projection, current_round: { ...round, round_id: previousRoundId, state, result_publication: "Withheld" } }}
      ranking={ranking}
    />);

    const table = screen.getByRole("table", { name: "順位・参加者ID・得点" });
    expect(within(table).getByRole("rowheader", { name: "ONE" })).toBeVisible();
    expect(within(table).getByRole("cell", { name: "10,000点" })).toBeVisible();
  });

  it("現在問がなくても最新の確定ランキングを表示する", () => {
    show({ projection: { ...projection, current_round: null } });
    expect(screen.getByRole("table", { name: "順位・参加者ID・得点" })).toBeVisible();
  });

  it("集計が一度もなければランキング領域に未集計を表示する", () => {
    show({ projection: { ...projection, current_round: null, latest_ranking_snapshot_version: null }, ranking: null });

    const rankingRegion = screen.getByRole("region", { name: "ランキング TOP5" });
    expect(within(rankingRegion).getByText("ランキングはまだありません。")).toBeVisible();
    expect(within(rankingRegion).queryByRole("table")).not.toBeInTheDocument();
  });

  it.each([
    { ...ranking, snapshot_version: 5 },
    { ...ranking, snapshot_version: 7 },
    null,
  ])("集計版が一致しない、または未取得のランキングは確認中とする", (candidate) => {
    show({ ranking: candidate });

    expect(screen.getByText("ランキングを確認しています。")).toBeVisible();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("新しい集計版へ変わったら旧版のランキングを表示しない", () => {
    const { rerender } = show();
    rerender(<ModeratorResults responseTimes={null} projection={{ ...projection, latest_ranking_snapshot_version: 7 }} ranking={ranking} />);

    expect(screen.getByText("ランキングを確認しています。")).toBeVisible();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("最新の集計がなくなれば以前のランキングを表示しない", () => {
    show({ projection: { ...projection, latest_ranking_snapshot_version: null } });

    expect(screen.getByText("ランキングはまだありません。")).toBeVisible();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("ランキングの取得済み0人を未取得と区別する", () => {
    show({ ranking: { ...ranking, rows: [] } });

    expect(screen.getByText("ランキング対象者はいません。")).toBeVisible();
    expect(screen.queryByText("ランキングを確認しています。")).not.toBeInTheDocument();
  });

  it("ランキングの同率ID全員にページ切り替えで到達できる", () => {
    const labels = Array.from({ length: 40 }, (_, index) => `A${String.fromCharCode(65 + Math.floor(index / 26))}${String.fromCharCode(65 + index % 26)}`);
    show({
      projection: { ...projection, event_state: "Completed" },
      ranking: { ...ranking, rows: labels.map((public_label) => ({ overall_rank: 1, public_label, total_points: 10000, correct_count: 6 })) },
    });

    const rankingRegion = screen.getByRole("region", { name: "ランキング一覧" });
    const visited: (string | null)[] = [];
    for (let page = 0; page < 8; page += 1) {
      visited.push(...within(rankingRegion).getAllByRole("rowheader").map((item) => item.textContent));
      if (page < 7) fireEvent.click(screen.getByRole("button", { name: "次へ" }));
    }
    expect(visited).toEqual(labels);
    expect(screen.getByRole("button", { name: "次へ" })).toBeDisabled();
  });


  it("分布の上部に正答者の平均・最速・最遅・中央値をAPI値で表示する", () => {
    const group = { answer_count: 2, average_response_ms: 1750, median_response_ms: 1500,
      fastest_response_ms: 1000, slowest_response_ms: 2500, rows: [] };
    const data: OperatorResponseTimes = { selected_round_id: roundId, timing_basis: "client_monotonic_v1",
      rounds: [{ round_id: roundId, round_number: 3, is_practice: false, prompt: "今回の問題" }],
      all_answers: { ...group, average_response_ms: 9999 }, correct_answers: group };
    const { rerender } = show({ responseTimes: data });
    const summary = screen.getAllByRole("definition")[0]!.closest("dl")!;
    expect(within(summary).getAllByRole("term").map((node) => node.textContent)).toEqual(["平均", "最速", "最遅", "中央値"]);
    expect(within(summary).getByText("1.750秒")).toBeVisible();
    expect(within(summary).getByText("1.500秒")).toBeVisible();
    expect(screen.getByText("第2問・正答者2人の回答時間")).toBeVisible();
    expect(screen.queryByText("9.999秒")).not.toBeInTheDocument();
    rerender(<ModeratorResults projection={{ ...projection, current_round: { ...round, round_id: previousRoundId, state: "Open" } }}
      responseTimes={data} ranking={ranking} />);
    expect(screen.getByText("第2問・正答者2人の回答時間")).toBeVisible();
    expect(within(distribution()).getByText("集計完了をお待ちください。")).toBeVisible();
  });

  it("旧snapshotや別問題の統計を混在表示せず、nullの時間を0秒へ補完しない", () => {
    const group = { answer_count: 0, average_response_ms: null, median_response_ms: null,
      fastest_response_ms: null, slowest_response_ms: null, rows: [] };
    const data: OperatorResponseTimes = { selected_round_id: roundId, timing_basis: "client_monotonic_v1",
      rounds: [{ round_id: roundId, round_number: 3, is_practice: false, prompt: "今回の問題" }],
      all_answers: group, correct_answers: group };
    const { rerender } = show({ responseTimes: data });
    expect(screen.getAllByText("回答なし")).toHaveLength(4);
    expect(screen.queryByText("0.000秒")).not.toBeInTheDocument();
    rerender(<ModeratorResults projection={projection} ranking={ranking} responseTimes={{ ...data, selected_round_id: previousRoundId }} />);
    expect(screen.getByText("回答時間を確認しています。")).toBeVisible();
    expect(screen.queryByText("回答なし")).not.toBeInTheDocument();
    rerender(<ModeratorResults projection={{ ...projection, latest_ranking_snapshot_version: 7 }} ranking={ranking} responseTimes={data} />);
    expect(screen.queryByText("回答なし")).not.toBeInTheDocument();
  });

  it("同順位者の減少と新snapshotでページを有効な範囲へ戻す", () => {
    const { rerender } = show();
    fireEvent.click(screen.getByRole("button", { name: "次へ" }));
    rerender(<ModeratorResults projection={projection} responseTimes={null}
      ranking={{ ...ranking, rows: ranking.rows.slice(0, 2) }} />);
    expect(screen.getByRole("rowheader", { name: "ONE" })).toBeVisible();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    rerender(<ModeratorResults projection={{ ...projection, latest_ranking_snapshot_version: 7 }} responseTimes={null}
      ranking={{ ...ranking, snapshot_version: 7 }} />);
    expect(screen.getByRole("rowheader", { name: "ONE" })).toBeVisible();
    expect(screen.getByRole("button", { name: "前へ" })).toBeDisabled();
  });
});
