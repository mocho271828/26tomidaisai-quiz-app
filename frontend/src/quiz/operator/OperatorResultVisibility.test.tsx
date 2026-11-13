import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { OperatorState } from "../../api/operatorContracts";
import { OperatorResultVisibility } from "./OperatorResultVisibility";

const projection: Pick<OperatorState, "result_visibility" | "visibility_controls"> = {
  result_visibility: {
    distribution: true,
    personal_ranking: false,
    overall_ranking: false,
  },
  visibility_controls: {
    distribution: { mode: "manual", initially_visible: true, changes: [] },
    personal_ranking: { mode: "manual", initially_visible: false, changes: [] },
    overall_ranking: {
      mode: "automatic",
      initially_visible: false,
      changes: [
        { after_round: 1, visible: true },
        { after_round: 3, visible: false },
        { after_round: 5, visible: true },
      ],
    },
  },
};

describe("OperatorResultVisibility", () => {
  it.each([
    ["みんなの回答", "distribution", false],
    ["自分の順位・みんなの順位", "rankings", true],
  ] as const)("%sの表示を変更する", async (label, target, visible) => {
    const user = userEvent.setup();
    const onChange = vi.fn(async () => undefined);
    render(<OperatorResultVisibility isBusy={false} onChange={onChange} projection={projection} />);

    const toggle = screen.getByRole("button", { name: label, pressed: !visible });
    expect(within(toggle).getByText(visible ? "非公開" : "公開")).toBeVisible();
    await user.click(toggle);

    expect(onChange).toHaveBeenCalledExactlyOnceWith(target, visible);
    expect(screen.getAllByRole("button")).toHaveLength(2);
  });

  it("自動で非表示と再表示する予定を示し、手動変更も許可する", () => {
    render(<OperatorResultVisibility isBusy={false} onChange={vi.fn()} projection={projection} />);

    expect(screen.getByText("全体順位：第1問（練習）終了後：公開 ／ 第2問終了後：非公開 ／ 第4問終了後：公開")).toBeVisible();
    expect(screen.getByText("手動の変更は、次の自動切替まで有効です。")).toBeVisible();
    expect(screen.getByText("自動切替")).toBeVisible();
    expect(screen.getAllByText("手動切替")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "自分の順位・みんなの順位" })).toBeEnabled();
  });

  it("本人順位と全体順位の異なる自動切替予定を対象名付きで示す", () => {
    render(<OperatorResultVisibility isBusy={false} onChange={vi.fn()} projection={{
      ...projection,
      visibility_controls: {
        ...projection.visibility_controls,
        personal_ranking: {
          mode: "automatic", initially_visible: false,
          changes: [{ after_round: 2, visible: true }],
        },
      },
    }} />);

    expect(screen.getByText("本人順位：第1問終了後：公開")).toBeVisible();
    expect(screen.getByText(/^全体順位：/)).toBeVisible();
  });

  it.each([
    [false, false, false, "非公開", true],
    [true, true, true, "公開", false],
    [true, false, "mixed", "設定不一致", true],
    [false, true, "mixed", "設定不一致", true],
  ] as const)("順位設定 %s / %s を表示し、1回の操作で揃える", async (
    personal, overall, pressed, status, nextVisible,
  ) => {
    const user = userEvent.setup();
    const onChange = vi.fn(async () => undefined);
    render(<OperatorResultVisibility isBusy={false} onChange={onChange} projection={{
      ...projection,
      result_visibility: { distribution: true, personal_ranking: personal, overall_ranking: overall },
    }} />);
    const button = screen.getByRole("button", { name: "自分の順位・みんなの順位" });

    expect(button).toHaveAttribute("aria-pressed", String(pressed));
    expect(within(button).getByText(status)).toBeVisible();
    if (pressed === "mixed") {
      expect(button).toHaveAccessibleDescription(/押すと両方を公開します/);
    }
    await user.click(button);
    expect(onChange).toHaveBeenCalledExactlyOnceWith("rankings", nextVisible);
  });

  it("実行中は変更ボタンを無効にする", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<OperatorResultVisibility isBusy onChange={onChange} projection={projection} />);
    for (const button of screen.getAllByRole("button")) {
      expect(button).toBeDisabled();
      await user.click(button);
    }
    expect(onChange).not.toHaveBeenCalled();
  });

  it.each(["{Enter}", " "])("キーボードの%sで公開状態を切り替える", async (key) => {
    const user = userEvent.setup();
    const onChange = vi.fn(async () => undefined);
    render(<OperatorResultVisibility isBusy={false} onChange={onChange} projection={projection} />);

    await user.tab();
    expect(screen.getByRole("button", { name: "みんなの回答" })).toHaveFocus();
    await user.keyboard(key);

    expect(onChange).toHaveBeenCalledExactlyOnceWith("distribution", false);
    onChange.mockClear();
    await user.tab();
    expect(screen.getByRole("button", { name: "自分の順位・みんなの順位" })).toHaveFocus();
    await user.keyboard(key);
    expect(onChange).toHaveBeenCalledExactlyOnceWith("rankings", true);
  });

  it("参加者への公開範囲を説明し、取得した状態の更新で公開表示を切り替える", () => {
    const props = { isBusy: false, onChange: vi.fn() };
    const { rerender } = render(<OperatorResultVisibility {...props} projection={projection} />);
    const toggle = screen.getByRole("button", { name: "自分の順位・みんなの順位", pressed: false });

    expect(toggle).toHaveAccessibleDescription("本人の総合順位・回答速度順位と、みんなの順位（TOP3）をまとめて切り替えます。");
    expect(screen.queryByText(/会場画面/)).not.toBeInTheDocument();

    rerender(<OperatorResultVisibility {...props} projection={{
      ...projection,
      result_visibility: { ...projection.result_visibility, personal_ranking: true, overall_ranking: true },
    }} />);

    expect(screen.getByRole("button", { name: "自分の順位・みんなの順位", pressed: true })).toBe(toggle);
    expect(within(toggle).getByText("公開")).toBeVisible();
  });
});
