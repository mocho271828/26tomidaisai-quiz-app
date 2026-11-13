import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ResultTabs } from "./ResultTabs";

describe("ResultTabs", () => {
  it.each([
    { round: 1, label: "第1問 · 練習" },
    { round: 2, label: "第1問" },
    { round: 3, label: "第2問" },
    { round: 6, label: "第5問" },
    { round: 9, label: "第8問" },
  ])("表示番号と区別して実出題$roundを観測できる", ({ round, label }) => {
    render(<ResultTabs answer={<p>本人の得点</p>} roundNumber={round} />);
    expect(screen.getByText(label)).toHaveAttribute("data-round-number", String(round));
  });

  it("参加者向け文言でタブを表示しキーボードで最後と先頭へ切り替える", async () => {
    const user = userEvent.setup();
    render(
      <ResultTabs
        answer={<p>本人の得点</p>}
        distribution={<p>全員の回答割合</p>}
        ranking={<p>現在の順位</p>}
      />,
    );

    const scoreTab = screen.getByRole("tab", { name: "マイスコア" });
    expect(scoreTab).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "みんなの回答" })).toBeVisible();
    scoreTab.focus();
    await user.keyboard("{End}");
    expect(screen.getByRole("tab", { name: "ランキング" })).toHaveFocus();
    expect(screen.getByText("現在の順位")).toBeVisible();
    await user.keyboard("{Home}");
    expect(scoreTab).toHaveFocus();
    expect(screen.getByText("本人の得点")).toBeVisible();
  });

  it("正解から回答分布と公開可能なランキングへ切り替える", async () => {
    const user = userEvent.setup();
    render(
      <ResultTabs
        answer={<p>正答内容</p>}
        distribution={<p>回答分布内容</p>}
        ranking={<p>ランキング内容</p>}
      />,
    );

    expect(screen.getByRole("tab", { name: "マイスコア" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByText("正答内容")).toBeVisible();
    expect(screen.getByRole("tab", { name: "みんなの回答" })).toHaveAttribute(
      "aria-controls",
      screen.getByText("回答分布内容").parentElement?.id,
    );
    expect(screen.getByText("回答分布内容").parentElement).not.toBeVisible();

    await user.click(screen.getByRole("tab", { name: "みんなの回答" }));
    expect(screen.getByText("正答内容").parentElement).not.toBeVisible();
    expect(screen.getByText("回答分布内容")).toBeVisible();

    await user.click(screen.getByRole("tab", { name: "ランキング" }));
    expect(screen.getByText("ランキング内容")).toBeVisible();
  });

  it("ランキングが非公開ならランキングタブをDOMへ出さない", () => {
    render(
      <ResultTabs
        answer={<p>正答内容</p>}
        distribution={<p>回答分布内容</p>}
      />,
    );

    expect(
      screen.queryByRole("tab", { name: "ランキング" }),
    ).not.toBeInTheDocument();
  });

  it("矢印キーで隣のタブへfocusと表示を移す", async () => {
    const user = userEvent.setup();
    render(
      <ResultTabs
        answer={<p>正答内容</p>}
        distribution={<p>回答分布内容</p>}
        ranking={<p>ランキング内容</p>}
      />,
    );
    const answerTab = screen.getByRole("tab", { name: "マイスコア" });
    answerTab.focus();

    await user.keyboard("{ArrowRight}");

    expect(screen.getByRole("tab", { name: "みんなの回答" })).toHaveFocus();
    expect(screen.getByText("回答分布内容")).toBeVisible();
  });

  it("選択中の分布を非表示にしたら正解へ戻り、再公開でタブを復元する", async () => {
    const user = userEvent.setup();
    const { rerender } = render(
      <ResultTabs answer={<p>正答内容</p>} distribution={<p>回答分布内容</p>} ranking={<p>順位内容</p>} />,
    );
    await user.click(screen.getByRole("tab", { name: "みんなの回答" }));

    rerender(<ResultTabs answer={<p>正答内容</p>} ranking={<p>順位内容</p>} />);
    expect(screen.queryByRole("tab", { name: "みんなの回答" })).not.toBeInTheDocument();
    expect(screen.queryByText("回答分布内容")).not.toBeInTheDocument();
    expect(screen.getByText("正答内容")).toBeVisible();
    expect(screen.getByRole("tab", { name: "ランキング" })).toBeVisible();

    rerender(<ResultTabs answer={<p>正答内容</p>} distribution={<p>回答分布内容</p>} />);
    expect(screen.getByRole("tab", { name: "みんなの回答" })).toBeVisible();
    expect(screen.queryByRole("tab", { name: "ランキング" })).not.toBeInTheDocument();
  });
});
