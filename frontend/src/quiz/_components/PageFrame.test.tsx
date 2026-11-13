import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PageFrame } from "./PageFrame";

describe("PageFrame", () => {
  it("standard layoutでは画面見出しを表示する", () => {
    render(
      <PageFrame audience="参加者" title="富大祭クイズ">
        <p>本文</p>
      </PageFrame>,
    );

    expect(
      screen.getByRole("heading", { level: 1, name: "富大祭クイズ" }),
    ).toBeVisible();
    expect(screen.queryByRole("link", { name: "画面選択" }))
      .not.toBeInTheDocument();
  });

  it("answering layoutでも見出しを1つ保つ", () => {
    render(
      <PageFrame audience="参加者" layout="answering" title="富大祭クイズ">
        <p>回答画面</p>
      </PageFrame>,
    );

    expect(
      screen.getAllByRole("heading", {
        hidden: true,
        level: 1,
        name: "富大祭クイズ",
      }),
    ).toHaveLength(1);
    expect(screen.queryByRole("link", { name: "画面選択" }))
      .not.toBeInTheDocument();
  });

  it("operator-wide layoutでは見出しを保ち運営PC用の幅を使う", () => {
    render(
      <PageFrame audience="運営" layout="operator-wide" title="進行コントロール">
        <p>運営本文</p>
      </PageFrame>,
    );

    const main = screen.getByRole("main");
    expect(main).toHaveClass("app-shell-operator-wide");
    expect(main.querySelector(".app-content")).toHaveClass(
      "app-content-operator-wide",
    );
    expect(
      screen.getByRole("heading", { level: 1, name: "進行コントロール" }),
    ).toBeVisible();
    expect(screen.queryByRole("link", { name: "画面選択" }))
      .not.toBeInTheDocument();
  });
});
