import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TommyMascot } from "./TommyMascot";

describe("TommyMascot", () => {
  it("labelがない装飾用途では支援技術から隠す", () => {
    const { container } = render(
      <TommyMascot size="small" variant="idle" />,
    );

    const mascot = container.firstElementChild;

    expect(mascot).toHaveAttribute("aria-hidden", "true");
    expect(mascot).not.toHaveAttribute("role");
    expect(mascot).toHaveClass(
      "tommy-mascot",
      "tommy-mascot--idle",
      "tommy-mascot--small",
    );
  });

  it("labelがある情報用途では画像の役割と名前を公開する", () => {
    render(
      <TommyMascot
        label="回答を考える案にゃい猫トミー"
        size="large"
        variant="thinking"
      />,
    );

    expect(
      screen.getByRole("img", { name: "回答を考える案にゃい猫トミー" }),
    ).toHaveClass(
      "tommy-mascot",
      "tommy-mascot--thinking",
      "tommy-mascot--large",
    );
  });

  it("空のlabelは名前なし画像にせず装飾用途として扱う", () => {
    const { container } = render(
      <TommyMascot label="  " size="medium" variant="waiting" />,
    );

    const mascot = container.firstElementChild;

    expect(mascot).toHaveAttribute("aria-hidden", "true");
    expect(mascot).not.toHaveAttribute("role");
  });
});
