import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { OperatorState } from "../../api/operatorContracts";
import { OperatorQuestionPanel } from "./OperatorQuestionPanel";
import { mockDialogMethods } from "./confirmationDialogTestSupport";
import { finalRoundIds, finalRoundProjection } from "./final-round/finalRoundTestFixtures";

type PanelProps = ComponentProps<typeof OperatorQuestionPanel>;

function panelProps(): PanelProps {
  const projection = finalRoundProjection();
  projection.current_round = { ...projection.current_round!, state: "Reveal", result_publication: "Published" };
  projection.available_questions = [
    { ...projection.current_round.question, display_order: 3 },
    { ...projection.current_round.question, question_id: "7a000000-0000-4000-8000-000000000004", prompt: "次の問題", display_order: 4 },
  ];
  projection.used_question_ids = [finalRoundIds.question];
  return {
    projection,
    awardCeremony: { isSynchronizing: false, retry: vi.fn().mockResolvedValue(true), onChange: vi.fn() },
    finalRound: {
      value: { event_id: finalRoundIds.event, event_version: 5, final_round_id: null },
      status: "ready", retry: vi.fn(), onChange: vi.fn().mockResolvedValue(undefined),
    },
    isBusy: false,
    onAdvanceRound: vi.fn().mockResolvedValue(undefined),
    onCompleteEvent: vi.fn().mockResolvedValue(undefined),
    onQuestionChange: vi.fn().mockResolvedValue(undefined),
    onRetryResults: vi.fn().mockResolvedValue(undefined),
    onRoundOperation: vi.fn().mockResolvedValue(undefined),
  };
}

const progressChanges: { name: string; update: (projection: OperatorState) => OperatorState }[] = [
  { name: "イベントの版", update: (projection) => ({ ...projection, event_version: projection.event_version + 1 }) },
  { name: "問題の版", update: (projection) => ({ ...projection, current_round: { ...projection.current_round!, version: 4 } }) },
  { name: "進行状態", update: (projection) => ({ ...projection, current_round: { ...projection.current_round!, state: "RoundBackupFailed" } }) },
  { name: "対象問題", update: (projection) => ({
    ...projection,
    current_round: {
      ...projection.current_round!, round_id: "7a000000-0000-4000-8000-000000000005",
      question: { ...projection.current_round!.question, question_id: "7a000000-0000-4000-8000-000000000004", prompt: "次の問題" },
    },
  }) },
];

describe("運営の重要操作の確認", () => {
  let restoreDialogMethods: () => void;
  beforeEach(() => { restoreDialogMethods = mockDialogMethods(); });
  afterEach(() => { cleanup(); restoreDialogMethods(); });

  it("最終問題は対象を確認し、初期フォーカスのキャンセルをEnterで選ぶと指定しない", async () => {
    const user = userEvent.setup();
    const props = panelProps();
    render(<OperatorQuestionPanel {...props} />);
    const trigger = screen.getByRole("button", { name: "この問題を最終問題にする" });
    await user.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "最終問題に指定しますか？" });
    expect(within(dialog).getByText("登録問題 3：現在の問題")).toBeVisible();
    expect(within(dialog).getByText("この問題を最終問題として記録します。")).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "キャンセル" })).toHaveFocus();
    expect(props.finalRound.onChange).not.toHaveBeenCalled();
    await user.keyboard("{Enter}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(props.finalRound.onChange).not.toHaveBeenCalled();
    expect(trigger).toHaveFocus();
  });

  it("最終問題の確認でネイティブのcancelイベントを受けた場合も指定しない", () => {
    const props = panelProps();
    render(<OperatorQuestionPanel {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "この問題を最終問題にする" }));
    fireEvent(screen.getByRole("dialog"), new Event("cancel", { cancelable: true }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(props.finalRound.onChange).not.toHaveBeenCalled();
  });

  it("最終問題は確認した操作だけを送り、同じ確定ボタンの連打では重複しない", () => {
    const props = panelProps();
    render(<OperatorQuestionPanel {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "この問題を最終問題にする" }));
    const confirm = within(screen.getByRole("dialog")).getByRole("button", { name: "この問題を最終問題に指定" });
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    expect(props.finalRound.onChange).toHaveBeenCalledExactlyOnceWith(true);
  });

  it("終了はチェック必須で、チェックを外す・キャンセルする・開き直すと確定できない", () => {
    const props = panelProps();
    render(<OperatorQuestionPanel {...props} />);
    const trigger = screen.getByRole("button", { name: "イベントを終了…" });
    fireEvent.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "イベントを終了しますか？" });
    const checkbox = within(dialog).getByRole("checkbox", { name: "終了後は次の問題へ進めないことを確認しました" });
    const confirm = within(dialog).getByRole("button", { name: "イベントを終了する" });
    expect(within(dialog).getByText("登録問題 3：現在の問題")).toBeVisible();
    expect(within(dialog).getByText("未出題の問題は残り1問です。")).toBeVisible();
    expect(within(dialog).getByText("現在の問題は最終問題に指定されていません。")).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "キャンセル" })).toHaveFocus();
    expect(checkbox).not.toBeChecked();
    expect(confirm).toBeDisabled();
    fireEvent.click(confirm);
    expect(props.onCompleteEvent).not.toHaveBeenCalled();
    fireEvent.click(checkbox);
    expect(confirm).toBeEnabled();
    fireEvent.click(checkbox);
    expect(confirm).toBeDisabled();
    fireEvent.click(checkbox);
    fireEvent.click(within(dialog).getByRole("button", { name: "キャンセル" }));
    expect(props.onCompleteEvent).not.toHaveBeenCalled();
    fireEvent.click(trigger);
    const reopened = screen.getByRole("dialog");
    expect(within(reopened).getByRole("checkbox")).not.toBeChecked();
    expect(within(reopened).getByRole("button", { name: "イベントを終了する" })).toBeDisabled();
  });

  it("終了のネイティブcancel後もチェックをリセットし、再確認した操作だけを送る", () => {
    const props = panelProps();
    render(<OperatorQuestionPanel {...props} />);
    const trigger = screen.getByRole("button", { name: "イベントを終了…" });
    fireEvent.click(trigger);
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("checkbox"));
    fireEvent(screen.getByRole("dialog"), new Event("cancel", { cancelable: true }));
    expect(props.onCompleteEvent).not.toHaveBeenCalled();
    fireEvent.click(trigger);
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("checkbox")).not.toBeChecked();
    fireEvent.click(within(dialog).getByRole("checkbox"));
    const confirm = within(dialog).getByRole("button", { name: "イベントを終了する" });
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    expect(props.onCompleteEvent).toHaveBeenCalledOnce();
  });

  it.each(progressChanges)("確認中に$nameが変わると最終問題の確認を破棄する", ({ update }) => {
    const props = panelProps();
    const view = render(<OperatorQuestionPanel {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "この問題を最終問題にする" }));
    expect(screen.getByRole("dialog")).toBeVisible();
    view.rerender(<OperatorQuestionPanel {...props} projection={update(props.projection)} />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(props.finalRound.onChange).not.toHaveBeenCalled();
  });

  it.each(progressChanges)("確認中に$nameが変わると終了の確認とチェックを破棄する", ({ update }) => {
    const props = panelProps();
    const view = render(<OperatorQuestionPanel {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "イベントを終了…" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("checkbox"));
    view.rerender(<OperatorQuestionPanel {...props} projection={update(props.projection)} />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(props.onCompleteEvent).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "イベントを終了…" }));
    expect(within(screen.getByRole("dialog")).getByRole("checkbox")).not.toBeChecked();
  });
});
