import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type {
  ParticipantOutcome,
  ParticipantResult,
  PublishedResult,
  Question,
} from "../../api/contracts";
import {
  ParticipantOutcomeCard,
  ParticipantPendingOutcomeCard,
} from "./QuizResult";

const correctOptionId = "00000000-0000-4000-8000-000000000001";
const incorrectOptionId = "00000000-0000-4000-8000-000000000002";

const question: Question = {
  options: [
    { content: "4", label: "A", option_id: correctOptionId },
    { content: "5", label: "B", option_id: incorrectOptionId },
  ],
  prompt: "2 + 2 は？",
  question_id: "00000000-0000-4000-8000-000000000003",
};

const publishedResult: PublishedResult = {
  correct_option_id: correctOptionId,
  distribution: {
    valid_answer_count: 2,
    options: [
      {
        content: "4",
        is_correct: true,
        label: "A",
        option_id: correctOptionId,
        response_percentage: 50,
        valid_response_count: 1,
      },
      {
        content: "5",
        is_correct: false,
        label: "B",
        option_id: incorrectOptionId,
        response_percentage: 50,
        valid_response_count: 1,
      },
    ],
  },
  publication: "Published",
};

function participantResult(
  outcome: ParticipantOutcome,
): Extract<ParticipantResult, { publication: "Published" }> {
  return { ...publishedResult, outcome };
}

describe("QuizResult", () => {
  it.each([
    {
      className: "outcome-correct",
      heading: "正解！",
      outcome: {
        timing_basis: "server_received_v1",
        is_correct: true,
        scoring_policy_version: "v1",
        speed_cohort: "Correct",
        speed_cohort_size: 1,
        speed_rank: 1,
        status: "Scored",
        total_points: 950,
        base_points: 900, speed_points: 50, elapsed_ms: 5000,
      } satisfies ParticipantOutcome,
      score: "950",
      speedLabel: "正解者1人中 1番目の速さ",
    },
    {
      className: "outcome-incorrect",
      heading: "不正解",
      outcome: {
        timing_basis: "server_received_v1",
        is_correct: false,
        scoring_policy_version: "v1",
        speed_cohort: "AllValid",
        speed_cohort_size: 4,
        speed_rank: 2,
        status: "Scored",
        total_points: 0,
        base_points: 0, speed_points: 0, elapsed_ms: 2500,
      } satisfies ParticipantOutcome,
      score: "0",
      speedLabel: "有効回答者4人中 2番目の速さ",
    },
    {
      className: "outcome-unanswered",
      heading: "未回答",
      outcome: { status: "Unanswered" } satisfies ParticipantOutcome,
      score: null,
      speedLabel: null,
    },
  ])("$headingの本人結果を得点と選択肢で伝える", (fixture) => {
    render(
      <ParticipantOutcomeCard
        question={question}
        result={participantResult(fixture.outcome)}
      />,
    );

    const region = screen.getByRole("region", { name: `マイスコア・${fixture.heading}` });
    expect(region).toHaveClass(fixture.className);
    expect(within(region).getByText("正解").parentElement).toHaveTextContent("A 4");
    if (fixture.score !== null) {
      expect(within(region).getByText(fixture.score)).toBeVisible();
    }
    if (fixture.speedLabel !== null) {
      expect(within(region).getByText(fixture.speedLabel)).toBeVisible();
    }
    if (fixture.outcome.status === "Scored") {
      expect(within(region).getByText("正答").parentElement).toHaveTextContent(`正答${fixture.outcome.base_points} pt`);
      expect(within(region).getByText("速さ").parentElement).toHaveTextContent(`速さ${fixture.outcome.speed_points} pt`);
      expect(within(region).getByText("回答時間").parentElement).toHaveTextContent(`${(fixture.outcome.elapsed_ms / 1000).toFixed(1)} 秒`);
    }
    expect(within(region).queryByRole("img")).not.toBeInTheDocument();
  });

  it("本人順位非公開でも本人得点の内訳と時間を表示し、速度順位を含めない", () => {
    render(<ParticipantOutcomeCard question={question} result={participantResult({
      timing_basis: "server_received_v1",
      status: "Scored",
      is_correct: true,
      total_points: 500,
      base_points: 400, speed_points: 100, elapsed_ms: 0,
      scoring_policy_version: "v2",
      speed_cohort: "Correct",
      speed_rank: null,
      speed_cohort_size: null,
    })} />);

    expect(screen.getByText("500")).toBeVisible();
    expect(screen.getByText("正答").parentElement).toHaveTextContent("400 pt");
    expect(screen.getByText("速さ").parentElement).toHaveTextContent("100 pt");
    expect(screen.getByText("回答時間").parentElement).toHaveTextContent("0.0 秒");
    expect(screen.getByRole("region", { name: "マイスコア・正解！" })).toBeVisible();
    expect(screen.queryByText(/番目の速さ/)).not.toBeInTheDocument();
  });
});

describe("保存済み採点の詳細", () => {
  it.each([
    { elapsedMs: 0, expected: "0.0 秒" },
    { elapsedMs: 2399, expected: "2.3 秒" },
    { elapsedMs: 2400, expected: "2.4 秒" },
    { elapsedMs: 2451, expected: "2.4 秒" },
    { elapsedMs: null, expected: "記録なし" },
  ])("$elapsedMs msを$expectedと表示し得点を再計算しない", ({ elapsedMs, expected }) => {
    render(<ParticipantOutcomeCard question={question} result={participantResult({
      timing_basis: "server_received_v1",
      status: "Scored", is_correct: true, base_points: 600, speed_points: 31,
      total_points: 631, elapsed_ms: elapsedMs, scoring_policy_version: "stored-policy",
      speed_cohort: "Correct", speed_rank: null, speed_cohort_size: null,
    })} roundNumber={1} />);
    expect(screen.getByText("631")).toBeVisible();
    expect(screen.getByText("正答").parentElement).toHaveTextContent("600 pt");
    expect(screen.getByText("速さ").parentElement).toHaveTextContent("31 pt");
    expect(screen.getByText("回答時間").parentElement).toHaveTextContent(expected);
    expect(screen.getByText("開始から受付まで")).toBeVisible();
    expect(screen.getByText(/今回の得点は総合に加算されません/)).toBeVisible();
  });
});

describe("結果状態の表示", () => {
  it("本人結果未確定では未回答や得点を表示しない", () => {
    render(<ParticipantPendingOutcomeCard question={question} result={publishedResult} />);
    expect(screen.getByRole("status")).toHaveTextContent("結果待ち");
    expect(screen.getByText("確認中")).toBeVisible();
    expect(screen.queryByText("未回答")).not.toBeInTheDocument();
    expect(screen.queryByText("今回の得点")).not.toBeInTheDocument();
    expect(screen.queryByText("正答")).not.toBeInTheDocument();
    expect(screen.queryByText("速さ")).not.toBeInTheDocument();
    expect(screen.queryByText("回答時間")).not.toBeInTheDocument();
  });

  it("未回答では正答と未回答を示し総合得点は受信値を維持する", () => {
    render(<ParticipantOutcomeCard question={question} result={participantResult({ status: "Unanswered" })} roundNumber={2} selectedOptionId={correctOptionId} totalPoints={720} />);
    expect(screen.getByRole("heading", { name: "今回は未回答でした" })).toBeVisible();
    expect(screen.getByText("未回答")).toBeVisible();
    expect(screen.queryByText("正答")).not.toBeInTheDocument();
    expect(screen.queryByText("速さ")).not.toBeInTheDocument();
    expect(screen.queryByText("回答時間")).not.toBeInTheDocument();
    expect(screen.getByText("総合得点").parentElement).toHaveTextContent("720 pt");
  });

  it("練習結果は総合得点の代わりに加算対象外の説明を表示する", () => {
    render(<ParticipantOutcomeCard question={question} result={participantResult({ status: "Unanswered" })} roundNumber={1} totalPoints={720} />);
    expect(screen.getByText(/今回の得点は総合に加算されません/)).toBeVisible();
    expect(screen.queryByText("総合得点")).not.toBeInTheDocument();
  });
});
