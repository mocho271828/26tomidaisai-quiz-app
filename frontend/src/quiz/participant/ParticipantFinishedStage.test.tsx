import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ParticipantRanking } from "../../api/contracts";
import { ParticipantFinishedStage } from "./ParticipantFinishedStage";

const ranking: ParticipantRanking = { snapshot_version: 2, overall_rank: 12, total_points: 2380, correct_count: 5, top_rows: null };

describe("ParticipantFinishedStage", () => {
  it("公開済みの総合得点と最終順位を表示する", () => {
    render(<ParticipantFinishedStage ranking={ranking} isRankingPublic />);
    expect(screen.getByRole("heading", { name: "最終結果" })).toBeVisible();
    expect(screen.getByText("総合得点").parentElement).toHaveTextContent("2,380pt");
    expect(screen.getByText("最終順位").parentElement).toHaveTextContent("12位");
  });

  it("本人結果を受信するまでは架空のゼロ得点を表示しない", () => {
    render(<ParticipantFinishedStage isRankingPublic />);
    expect(screen.getByRole("status")).toHaveTextContent("結果待ち");
    expect(screen.getAllByText("—")).toHaveLength(2);
  });

  it("非公開時は受信済みの得点と順位もDOMに含めない", () => {
    render(<ParticipantFinishedStage ranking={ranking} isRankingPublic={false} />);
    expect(screen.getAllByText("非公開")).toHaveLength(2);
    expect(screen.queryByText("2,380")).not.toBeInTheDocument();
    expect(screen.queryByText("12")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
