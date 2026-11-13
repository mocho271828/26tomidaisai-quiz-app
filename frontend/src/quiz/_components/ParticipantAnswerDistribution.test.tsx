import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { PublishedResult, Question } from "../../api/contracts";
import { ParticipantAnswerDistribution } from "./ParticipantAnswerDistribution";

const correctOptionId = "00000000-0000-4000-8000-000000000001";
const selectedOptionId = "00000000-0000-4000-8000-000000000002";
const question: Question = {
  question_id: "00000000-0000-4000-8000-000000000003",
  prompt: "日本でいちばん高い山は？",
  options: [
    { option_id: correctOptionId, label: "A", content: "富士山" },
    { option_id: selectedOptionId, label: "B", content: "北岳" },
  ],
};

const result: PublishedResult = {
  publication: "Published",
  correct_option_id: correctOptionId,
  distribution: {
    valid_answer_count: 0,
    options: question.options.map((option, index) => ({
      ...option,
      is_correct: index === 0,
      response_percentage: 0,
      valid_response_count: 0,
    })),
  },
};

describe("ParticipantAnswerDistribution", () => {
  it("0件でも全選択肢と0%を表示し正答と本人回答を区別する", () => {
    render(
      <ParticipantAnswerDistribution
        question={question}
        result={result}
        selectedOptionId={selectedOptionId}
      />,
    );

    expect(screen.getByRole("heading", { name: question.prompt })).toBeVisible();
    expect(screen.getAllByText("0%")).toHaveLength(2);
    expect(screen.getByText("正解").closest("li")).toHaveClass("result-correct");
    const selected = screen.getByText("あなたの回答").closest("li");
    expect(selected).toHaveTextContent("北岳");
    expect(selected).not.toHaveClass("result-correct", "result-incorrect-selected");
    expect(screen.getByRole("progressbar", { name: "A. 富士山 の回答割合" }))
      .toHaveAttribute("aria-valuenow", "0");
  });

  it("分布非公開なら割合や本人回答をDOMに含めない", () => {
    const { container } = render(
      <ParticipantAnswerDistribution
        question={question}
        result={{ ...result, distribution: null }}
        selectedOptionId={selectedOptionId}
      />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it("本人が正答を選んだ場合も正解と本人回答を同じ可視ラベルで伝える", () => {
    render(
      <ParticipantAnswerDistribution
        question={question}
        result={result}
        selectedOptionId={correctOptionId}
      />,
    );

    const correctLabel = screen.getByText("正解");
    expect(correctLabel).toBeVisible();
    expect(correctLabel.closest(".sr-only, [aria-hidden='true']")).toBeNull();
    const status = correctLabel.closest("small");
    expect(status).toHaveTextContent("正解 · あなたの回答");
    expect(screen.getByText("あなたの回答").closest("small")).toBe(status);
    expect(status?.closest("li")).toHaveTextContent("富士山");
  });
});
