import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ModeratorParticipantPreview } from "./ModeratorParticipantPreview";

describe("ModeratorParticipantPreview", () => {
  it("通常の参加者画面を同一originで埋め込み、参加・回答の反映を案内する", () => {
    render(<ModeratorParticipantPreview />);

    const frame = screen.getByTitle<HTMLIFrameElement>("参加者画面");
    expect(frame.tagName).toBe("IFRAME");
    expect(frame).toHaveAttribute("src", "/?moderator-preview=1");
    expect(new URL(frame.src).origin).toBe(window.location.origin);
    expect(frame).not.toHaveAttribute("sandbox");
    expect(screen.getByText("未参加なら自動登録します。回答は通常の参加者として集計されます。")).toBeVisible();
  });

  it("司会画面の再描画でiframeを作り直さず、参加者セッションと通信を維持する", () => {
    const { rerender } = render(<ModeratorParticipantPreview />);
    const frame = screen.getByTitle("参加者画面");

    rerender(<ModeratorParticipantPreview />);

    expect(screen.getByTitle("参加者画面")).toBe(frame);
    expect(screen.getByRole("region", { name: "参加者画面の表示内容" })).not.toHaveAttribute("tabindex");
  });
});
