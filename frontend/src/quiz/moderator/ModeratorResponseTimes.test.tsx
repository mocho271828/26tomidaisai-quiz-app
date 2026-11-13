import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { OperatorLatestRanking, OperatorState } from "../../api/operatorContracts";
import type { OperatorResponseTimes } from "../../api/operatorResponseTimeContracts";
import { ModeratorResponseTimes } from "./ModeratorResponseTimes";

const roundId = "7b000000-0000-4000-8000-000000000001";
const previousRoundId = "7b000000-0000-4000-8000-000000000002";
const round: NonNullable<OperatorState["current_round"]> = {
  round_id: roundId, version: 3, state_version: 4, state: "ReadyToReveal",
  result_publication: "Withheld", can_publish_results: true, result_failure_code: null,
  open_at: "2026-10-22T03:00:00Z", standard_close_at: "2026-10-22T03:00:10Z",
  question: { question_id: "7b000000-0000-4000-8000-000000000003", version: 1, prompt: "今回の問題", options: [] },
  statistics: { answer_count: 3, average_response_ms: 1500, options: [] },
};
const control = { mode: "manual" as const, initially_visible: false, changes: [] };
const projection: OperatorState = {
    award_ceremony_phase: null,
  event_id: "7b000000-0000-4000-8000-000000000004", event_version: 5, event_title: "富大祭",
  event_state: "InProgress", projected_at: "2026-10-22T03:00:15Z", start_delay_seconds: 3,
  participant_count: 3, available_questions: [], used_question_ids: [], latest_ranking_snapshot_version: 2,
  result_visibility: { distribution: false, personal_ranking: false, overall_ranking: false },
  visibility_controls: { distribution: control, personal_ranking: control, overall_ranking: control },
  current_round: round,
};
const emptyGroup: OperatorResponseTimes["all_answers"] = {
  answer_count: 0, average_response_ms: null, median_response_ms: null,
  fastest_response_ms: null, slowest_response_ms: null, rows: [],
};
const data: OperatorResponseTimes = {
  selected_round_id: roundId, timing_basis: "client_monotonic_v1",
  rounds: [
    { round_id: previousRoundId, round_number: 1, is_practice: true, prompt: "前の問題" },
    { round_id: roundId, round_number: 2, is_practice: false, prompt: "今回の問題" },
  ],
  all_answers: {
    answer_count: 3, average_response_ms: 1500, median_response_ms: 1000,
    fastest_response_ms: 1000, slowest_response_ms: 2500,
    rows: [
      { rank: 1, public_label: "AAA", response_ms: 1000, is_correct: false },
      { rank: 1, public_label: "AAB", response_ms: 1000, is_correct: true },
      { rank: 3, public_label: "ZZZ", response_ms: 2500, is_correct: true },
    ],
  },
  correct_answers: {
    answer_count: 2, average_response_ms: 1750, median_response_ms: 1750,
    fastest_response_ms: 1000, slowest_response_ms: 2500,
    rows: [
      { rank: 1, public_label: "AAB", response_ms: 1000, is_correct: true },
      { rank: 2, public_label: "ZZZ", response_ms: 2500, is_correct: true },
    ],
  },
};

const ranking: OperatorLatestRanking = {
  round_id: roundId, snapshot_version: 2,
  rows: [
    { public_label: "AAB", overall_rank: 5, total_points: 1600, correct_count: 3 },
    { public_label: "ZZZ", overall_rank: 1, total_points: 2400, correct_count: 4 },
  ],
};

function show(overrides: Partial<Parameters<typeof ModeratorResponseTimes>[0]> = {}) {
  return render(<ModeratorResponseTimes projection={projection} responseTimes={data} ranking={ranking} {...overrides} />);
}

function winner(): HTMLElement {
  return screen.getByRole("region", { name: "今回の1位 最速の正答者" });
}

describe("ModeratorResponseTimes", () => {
  it("公開前から最速正答者のID・正式回答時間・総合得点を表示する", () => {
    show();
    expect(within(winner()).getByText("AAB")).toBeVisible();
    expect(within(winner()).getByText("1.000秒")).toBeVisible();
    expect(within(winner()).getByText("1,600点")).toBeVisible();
    expect(within(winner()).queryByText("AAA")).not.toBeInTheDocument();
    expect(within(winner()).queryByText("ZZZ")).not.toBeInTheDocument();
    expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  });

  it("同率1位の全員をページ切り替えで確認でき、総合1位と取り違えない", () => {
    show({ responseTimes: { ...data, correct_answers: {
      ...data.correct_answers, rows: data.correct_answers.rows.map((row) => ({ ...row, rank: 1, response_ms: 1000 })),
    } } });
    expect(within(winner()).getByText("AAB")).toBeVisible();
    expect(screen.getByRole("button", { name: "前へ" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "次へ" }));
    expect(within(winner()).getByText("ZZZ")).toBeVisible();
    expect(within(winner()).getByText("2,400点")).toBeVisible();
    expect(within(winner()).getByText("1.000秒")).toBeVisible();
    expect(screen.getByRole("button", { name: "次へ" })).toBeDisabled();
    expect(screen.getByText("2 / 2 ページ · 全2人")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "前へ" }));
    expect(within(winner()).getByText("AAB")).toBeVisible();
  });

  it.each(["PreparingAssets", "Scheduled", "Open", "AwaitingValidation", "Voided"] as const)(
    "%sでは以前の1位を今回の結果として表示しない", (state) => {
      show({ projection: { ...projection, current_round: { ...round, state } } });
      expect(screen.getByText("集計完了をお待ちください。")).toBeVisible();
      expect(within(winner()).queryByText("AAB")).not.toBeInTheDocument();
    },
  );

  it.each([
    { ranking: { ...ranking, snapshot_version: 1 } },
    { ranking: { ...ranking, round_id: previousRoundId } },
    { responseTimes: { ...data, selected_round_id: previousRoundId } },
    { ranking: null },
    { responseTimes: null },
  ])("別問題・旧snapshot・読込中は今回の1位として表示しない", (overrides) => {
    show(overrides);
    expect(screen.getByText("今回の結果を確認しています。")).toBeVisible();
    expect(within(winner()).queryByText("AAB")).not.toBeInTheDocument();
  });

  it("初回の問題待ちと正答者なしを区別する", () => {
    const { rerender } = show({ projection: { ...projection, current_round: null } });
    expect(screen.getByText("問題の開始をお待ちください。")).toBeVisible();
    rerender(<ModeratorResponseTimes projection={projection} ranking={ranking}
      responseTimes={{ ...data, all_answers: emptyGroup, correct_answers: emptyGroup }} />);
    expect(screen.getByText("正答者はいません。")).toBeVisible();
    expect(screen.queryByText("0.000秒")).not.toBeInTheDocument();
  });

  it("得点の欠測を0点に補完せず、保存済み0点と0msは表示する", () => {
    const { rerender } = show({ ranking: { ...ranking, rows: [] } });
    expect(screen.getByText("記録なし")).toBeVisible();
    expect(screen.queryByText("0点")).not.toBeInTheDocument();
    rerender(<ModeratorResponseTimes projection={projection}
      ranking={{ ...ranking, rows: ranking.rows.map((row) => ({ ...row, total_points: 0 })) }}
      responseTimes={{ ...data, correct_answers: { ...data.correct_answers,
        rows: data.correct_answers.rows.map((row) => ({ ...row, response_ms: 0 })),
      } }} />);
    expect(screen.getByText("0点")).toBeVisible();
    expect(screen.getByText("0.000秒")).toBeVisible();
  });

  it("人数減少で存在しない同率者ページを残さず、新snapshotで先頭へ戻る", () => {
    const tied = { ...data, correct_answers: { ...data.correct_answers,
      rows: data.correct_answers.rows.map((row) => ({ ...row, rank: 1 })),
    } };
    const { rerender } = show({ responseTimes: tied });
    fireEvent.click(screen.getByRole("button", { name: "次へ" }));
    rerender(<ModeratorResponseTimes projection={projection} responseTimes={data} ranking={ranking} />);
    expect(screen.getByText("AAB")).toBeVisible();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    rerender(<ModeratorResponseTimes projection={{ ...projection, latest_ranking_snapshot_version: 3 }}
      responseTimes={tied} ranking={{ ...ranking, snapshot_version: 3 }} />);
    expect(screen.getByText("AAB")).toBeVisible();
    expect(screen.getByRole("button", { name: "前へ" })).toBeDisabled();
  });
});
