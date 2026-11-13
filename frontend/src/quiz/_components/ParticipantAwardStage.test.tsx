import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { AwardCeremony } from "../../api/contracts";
import { ParticipantAwardStage } from "./ParticipantAwardStage";

const ceremony: AwardCeremony = {
  phase: "Announced",
  top_rows: ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"].map((public_label, index) => ({
    public_label, overall_rank: Math.min(index + 1, 5),
  })),
};

describe("ParticipantAwardStage", () => {
  it("発表待ちでは順位表や終了リンクを先に表示しない", () => {
    render(<ParticipantAwardStage ceremony={{ phase: "Waiting", top_rows: null }} publicLabel="AAA" />);
    expect(screen.getByRole("heading", { name: "最終結果発表!!" })).toBeVisible();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it.each([["AAA", 1], ["EEE", 5], ["FFF", 5]] as const)("%s本人は配信済みの%d位と舞台への呼び出しを表示する", (publicLabel, rank) => {
    render(<ParticipantAwardStage ceremony={ceremony} publicLabel={publicLabel} />);
    expect(screen.getByRole("heading", { name: new RegExp(`あなたは.*${rank}.*位!!`, "u") })).toBeVisible();
    expect(screen.getByText(/ステージ上に/)).toHaveTextContent("ステージ上にお越しください!!");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("対象外の参加者は同率5位の全員へページ切り替えで到達できる", async () => {
    const user = userEvent.setup();
    render(<ParticipantAwardStage ceremony={ceremony} publicLabel="ZZZ" />);
    expect(screen.getByRole("heading", { name: "最終ランキング" })).toBeVisible();
    expect(within(screen.getByRole("table")).getAllByRole("row")).toHaveLength(6);
    expect(screen.getByText("EEE")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "次へ" }));
    expect(screen.getByText("FFF")).toBeVisible();
    expect(screen.getByText("5位")).toBeVisible();
    expect(screen.getByText("2 / 2 ページ · 全6人")).toBeVisible();
    expect(screen.getByRole("button", { name: "次へ" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "前へ" }));
    expect(screen.getByText("AAA")).toBeVisible();
  });

  it("共通プレビューは個人順位を合成せず、対象者0人も明示する", () => {
    const { rerender } = render(<ParticipantAwardStage ceremony={ceremony} />);
    expect(screen.getByRole("table")).toBeVisible();
    expect(screen.queryByText(/あなたは/)).not.toBeInTheDocument();
    rerender(<ParticipantAwardStage ceremony={{ phase: "Announced", top_rows: [] }} />);
    expect(screen.getByText("ランキング対象者はいません。")).toBeVisible();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });
});
