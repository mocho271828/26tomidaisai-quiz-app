import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";

vi.mock("./quiz/participant/ParticipantPage", () => ({
  ParticipantPage: ({ autoJoin }: { autoJoin?: boolean }) => <h1 data-auto-join={autoJoin}>参加者ページ</h1>,
}));
vi.mock("./quiz/operator/OperatorPage", () => ({
  OperatorPage: () => <h1>運営ページ</h1>,
}));
vi.mock("./quiz/moderator/ModeratorPage", () => ({
  ModeratorPage: () => <h1>司会者ページ</h1>,
}));
describe("App", () => {
  afterEach(() => vi.unstubAllGlobals());

  it.each([
    ["/", "参加者ページ"],
    ["/operator", "運営ページ"],
    ["/operator/", "運営ページ"],
    ["/moderator", "司会者ページ"],
    ["/moderator/", "司会者ページ"],
  ])("%sを対応する画面へ割り当てる", (path, heading) => {
    window.history.replaceState({}, "", path);
    render(<App />);

    expect(screen.getByRole("heading", { name: heading })).toBeVisible();
  });

  it.each([
    [null, "/?moderator-preview=1", false],
    ["/operator", "/?moderator-preview=1", false],
    ["/", "/?moderator-preview=1", false],
    ["/moderator", "/", false],
    ["/moderator", "/?moderator-preview=unknown", false],
    ["/moderator", "/?moderator-preview=1", true],
    ["/moderator/", "/?moderator-preview=1", true],
  ])("埋込元=%s, %sの自動参加を%sに制限する", (embeddingPath, path, expectedAutoJoin) => {
    vi.stubGlobal("frameElement", embeddingPath === null ? null : {
      ownerDocument: { location: { pathname: embeddingPath } },
    });
    window.history.replaceState({}, "", path);

    render(<App />);

    expect(screen.getByRole("heading", { name: "参加者ページ" }))
      .toHaveAttribute("data-auto-join", String(expectedAutoJoin));
    expect(screen.getByRole("heading", { name: "参加者ページ" }).closest(".moderator-embedded-participant") !== null)
      .toBe(expectedAutoJoin);
  });

  it("cross-origin親でframeElementを取得できない場合は自動参加しない", () => {
    vi.stubGlobal("parent", {});
    vi.stubGlobal("frameElement", null);
    window.history.replaceState({}, "", "/?moderator-preview=1");

    render(<App />);

    expect(screen.getByRole("heading", { name: "参加者ページ" })).toHaveAttribute("data-auto-join", "false");
  });

  it.each(["/participant", "/public", "/result", "/result/", "/unknown"])(
    "%sは404と参加者トップへの導線を表示する",
    (path) => {
      window.history.replaceState({}, "", path);
      render(<App />);

      expect(screen.getByRole("heading", { name: "ページが見つかりません" })).toBeVisible();
      expect(screen.getByRole("alert")).toHaveTextContent("このURLの画面はありません。");
      expect(
        screen.getByRole("link", { name: "クイズに参加する" }),
      ).toHaveAttribute("href", "/");
    },
  );
});
