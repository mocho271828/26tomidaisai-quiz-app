import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { ParticipantRanking } from "../../api/contracts";
import { ParticipantRankingCard } from "./ParticipantRankingCard";

const ranking: ParticipantRanking = { snapshot_version: 2, overall_rank: 12, total_points: 110, correct_count: 1, top_rows: null };
const topRows = [
  { public_label: "ONE", overall_rank: 1, total_points: 1000, correct_count: 1 },
  { public_label: "TWO", overall_rank: 2, total_points: 950, correct_count: 1 },
  { public_label: "TRE", overall_rank: 3, total_points: 900, correct_count: 1 },
  { public_label: "FOU", overall_rank: 3, total_points: 900, correct_count: 1 },
];

describe("ParticipantRankingCard", () => {
  it("本人だけ公開されている場合はTOP3をDOMに含めない", () => {
    render(<ParticipantRankingCard publicLabel="ABC" ranking={ranking} />);
    expect(screen.getByText("あなたの総合順位")).toBeVisible();
    expect(screen.getByText("位").parentElement).toHaveTextContent("12位");
    expect(screen.getByText("ABC")).toBeVisible();
    expect(screen.getByText("あなた").parentElement).toHaveTextContent("110 pt");
    expect(screen.queryByRole("heading", { name: "みんなのトップ3" })).not.toBeInTheDocument();
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
  });

  it("公開された3位以内は同点の全員をページ切り替えで表示できる", async () => {
    const user = userEvent.setup();
    render(<ParticipantRankingCard publicLabel="ABC" ranking={{ ...ranking, top_rows: topRows }} />);
    const top = screen.getByRole("region", { name: "みんなのトップ3" });
    expect(within(top).getAllByRole("listitem")).toHaveLength(3);
    expect(within(top).getByText("ONE").closest("li")).toHaveTextContent("1,000 pt");
    expect(screen.getByRole("button", { name: "前へ" })).toBeDisabled();
    expect(screen.getByText("1 / 2 ページ · 全4人")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "次へ" }));
    expect(within(top).getAllByRole("listitem")).toHaveLength(1);
    expect(within(top).getByText("FOU")).toBeVisible();
    expect(within(top).getByLabelText("3位")).toBeVisible();
    expect(screen.getByRole("button", { name: "次へ" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "前へ" }));
    expect(within(top).getByText("TRE")).toBeVisible();
  });

  it("再公開で件数が減った場合は有効なページへ戻る", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<ParticipantRankingCard publicLabel="ABC" ranking={{ ...ranking, top_rows: topRows }} />);
    await user.click(screen.getByRole("button", { name: "次へ" }));
    rerender(<ParticipantRankingCard publicLabel="ABC" ranking={{ ...ranking, top_rows: topRows.slice(0, 1) }} />);
    expect(screen.getByText("ONE")).toBeVisible();
    expect(screen.queryByText("FOU")).not.toBeInTheDocument();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  });

  it("公開済みでも該当者がいない場合は空状態を表示する", () => {
    render(<ParticipantRankingCard publicLabel="ABC" ranking={{ ...ranking, top_rows: [] }} />);
    expect(screen.getByRole("heading", { name: "みんなのトップ3" })).toBeVisible();
    expect(screen.getByText("公開できる順位はまだありません。")).toBeVisible();
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
  });
});
