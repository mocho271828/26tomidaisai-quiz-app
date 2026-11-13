import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { OperatorState } from "../../api/operatorContracts";
import { OperatorQuestionSelector } from "./OperatorQuestionSelector";

const questions: OperatorState["available_questions"] = [
  {
    question_id: "70000000-0000-4000-8000-000000000001",
    version: 1,
    display_order: 1,
    prompt: "現在の問題",
    options: [
      {
        option_id: "70000000-0000-4000-8000-000000000011",
        label: "A",
        content: "現在の選択肢",
        is_correct: true,
      },
    ],
  },
  {
    question_id: "70000000-0000-4000-8000-000000000002",
    version: 2,
    display_order: 2,
    prompt: "次の問題",
    options: [
      {
        option_id: "70000000-0000-4000-8000-000000000012",
        label: "A",
        content: "次の選択肢",
        is_correct: true,
      },
    ],
  },
  {
    question_id: "70000000-0000-4000-8000-000000000003",
    version: 3,
    display_order: 3,
    prompt: "後の問題",
    options: [
      {
        option_id: "70000000-0000-4000-8000-000000000013",
        label: "A",
        content: "後の選択肢",
        is_correct: true,
      },
    ],
  },
];

describe("OperatorQuestionSelector", () => {
  it("preview領域を候補と関連付け、別Roundでは次問へ戻す", async () => {
    const user = userEvent.setup();
    const onQuestionChange = vi.fn((): Promise<void> => Promise.resolve());
    const props = {
      canSelectQuestion: true,
      currentQuestionId: questions[0]!.question_id,
      isBusy: false,
      mode: "switch-current-round" as const,
      onQuestionChange,
      questions,
    };
    const { rerender } = render(
      <OperatorQuestionSelector key="round-a" {...props} />,
    );

    expect(screen.getByText("候補を確認").closest("details")).not.toHaveAttribute("open");
    expect(screen.getByRole("region", { name: /登録問題 2.*次の問題/ })).not.toBeVisible();
    expect(screen.getByRole("button", { name: "登録問題 2へ切り替える" })).not.toBeVisible();
    await user.click(screen.getByText("候補を確認"));

    const nextButton = screen.getByRole("button", {
      name: /登録問題 2.*次の問題.*プレビュー中/,
    });
    const nextPreview = screen.getByRole("region", {
      name: /登録問題 2.*次の問題/,
    });
    expect(nextButton).toHaveAttribute("aria-controls", nextPreview.id);
    expect(nextPreview).toHaveAttribute("aria-live", "polite");
    expect(nextPreview).toHaveAttribute("aria-atomic", "true");

    await user.click(
      screen.getByRole("button", { name: /登録問題 3.*プレビューする/ }),
    );
    expect(
      screen.getByRole("region", { name: /登録問題 3.*後の問題/ }),
    ).toBeVisible();
    expect(onQuestionChange).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "登録問題 3へ切り替える" })).toBeVisible();
    await user.click(screen.getByText("候補を確認"));
    expect(screen.getByRole("button", { name: "登録問題 3へ切り替える" })).not.toBeVisible();

    rerender(<OperatorQuestionSelector key="round-b" {...props} />);

    expect(screen.getByText("候補を確認").closest("details")).not.toHaveAttribute("open");
    await user.click(screen.getByText("候補を確認"));

    expect(
      screen.getByRole("region", { name: /登録問題 2.*次の問題/ }),
    ).toBeVisible();
  });

  it("新Roundの進行は候補を閉じても表示し、選んだ問題の確定時だけbackup検証へ進む", async () => {
    const user = userEvent.setup();
    const onQuestionChange = vi.fn((): Promise<void> => Promise.resolve());

    render(
      <OperatorQuestionSelector
        canSelectQuestion
        currentQuestionId={questions[0]!.question_id}
        isBusy={false}
        mode="advance-round"
        onQuestionChange={onQuestionChange}
        questions={questions}
      />,
    );

    expect(screen.getByText("候補を確認").closest("details")).not.toHaveAttribute("open");
    const defaultAdvance = screen.getByRole("button", { name: "backupを検証して登録問題 2へ進む" });
    expect(defaultAdvance).toBeVisible();
    expect(defaultAdvance).toBeEnabled();
    await user.click(screen.getByText("候補を確認"));

    const switcher = screen.getByRole("group", {
      name: "次のRoundで出題する問題",
    });
    expect(
      screen.getByRole("button", { name: /登録問題 1.*出題済み/ }),
    ).toBeDisabled();

    await user.click(
      within(switcher).getByRole("button", {
        name: /登録問題 3.*プレビューする/,
      }),
    );
    expect(onQuestionChange).not.toHaveBeenCalled();

    await user.click(screen.getByText("候補を確認"));
    expect(screen.getByText("候補を確認").closest("details")).not.toHaveAttribute("open");
    expect(screen.getByRole("region", { name: /登録問題 3.*後の問題/ })).not.toBeVisible();
    const selectedAdvance = screen.getByRole("button", { name: "backupを検証して登録問題 3へ進む" });
    expect(selectedAdvance).toBeVisible();
    expect(selectedAdvance).toBeEnabled();
    await user.click(selectedAdvance);
    expect(onQuestionChange).toHaveBeenCalledOnce();
    expect(onQuestionChange).toHaveBeenCalledWith(questions[2]!.question_id);
  });
});
