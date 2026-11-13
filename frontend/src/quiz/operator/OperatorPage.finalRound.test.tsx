import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiResponseError } from "../../api/client";
import { OperatorPage } from "./OperatorPage";
import { mockDialogMethods } from "./confirmationDialogTestSupport";
import { finalRoundIds, finalRoundProjection } from "./final-round/finalRoundTestFixtures";

const api = vi.hoisted(() => ({ getOperatorSession: vi.fn(), getOperatorState: vi.fn(), getOperatorRanking: vi.fn(), mutateRound: vi.fn() }));
const finalApi = vi.hoisted(() => ({ getOperatorFinalRound: vi.fn(), setOperatorFinalRound: vi.fn() }));
vi.mock("../../api/operatorClient", () => api);
vi.mock("../../api/operatorFinalRoundClient", () => finalApi);
vi.mock("../_hooks/useRealtimeRefresh", () => ({ useRealtimeRefresh: () => "live" }));

function selected(version = 5) {
  return { event_id: finalRoundIds.event, event_version: version, final_round_id: finalRoundIds.round };
}

describe("運営による最終問題指定", () => {
  let restoreDialogMethods: () => void;
  afterEach(() => { cleanup(); restoreDialogMethods(); });
  beforeEach(() => {
    vi.resetAllMocks();
    restoreDialogMethods = mockDialogMethods();
    api.getOperatorSession.mockResolvedValue({ operator: { account_id: finalRoundIds.question, username: "operator", role: "Operator" } });
    api.getOperatorState.mockResolvedValue(finalRoundProjection());
    api.getOperatorRanking.mockResolvedValue(null);
    finalApi.getOperatorFinalRound.mockResolvedValue({ event_id: finalRoundIds.event, event_version: 5, final_round_id: null });
    finalApi.setOperatorFinalRound.mockResolvedValue({ event_version: 6 });
  });

  it.each(["Open", "AwaitingValidation", "ReadyToReveal"] as const)("%sでも進行時間に合わせて現在問を指定できる", async (state) => {
    const projection = finalRoundProjection();
    projection.current_round = { ...projection.current_round!, state };
    api.getOperatorState.mockResolvedValue(projection);
    render(<OperatorPage />);
    await waitFor(() => expect(screen.getByRole("button", { name: "この問題を最終問題にする" })).toBeEnabled());
    expect(screen.getByRole("heading", { name: "最終問題の指定" })).toBeVisible();
    expect(screen.getByText("進行時間に合わせて最終問題を指定してください。司会者用画面のランキング TOP5には、常に直近の確定結果を表示します。")).toBeVisible();
    expect(finalApi.setOperatorFinalRound).not.toHaveBeenCalled();
  });

  it("指定操作は他の進行操作と排他し、Event版・冪等キーだけを送る", async () => {
    let complete: (value: { event_version: number }) => void = () => undefined;
    finalApi.setOperatorFinalRound.mockReturnValueOnce(new Promise((resolve) => { complete = resolve; }));
    render(<OperatorPage />);
    await waitFor(() => expect(screen.getByRole("button", { name: "この問題を最終問題にする" })).toBeEnabled());
    const button = screen.getByRole("button", { name: "この問題を最終問題にする" });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(finalApi.setOperatorFinalRound).not.toHaveBeenCalled();
    const confirmButton = within(screen.getByRole("dialog", { name: "最終問題に指定しますか？" }))
      .getByRole("button", { name: "この問題を最終問題に指定" });
    fireEvent.click(confirmButton);
    fireEvent.click(confirmButton);
    expect(finalApi.setOperatorFinalRound).toHaveBeenCalledOnce();
    expect(finalApi.setOperatorFinalRound).toHaveBeenCalledWith(finalRoundIds.round, true, {
      expected_version: 5, idempotency_key: expect.any(String),
    }, expect.any(AbortSignal));
    expect(screen.getByRole("button", { name: "正解・結果を公開" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "正解・結果を公開" }));
    expect(api.mutateRound).not.toHaveBeenCalled();
    api.getOperatorState.mockResolvedValue({ ...finalRoundProjection(), event_version: 6 });
    finalApi.getOperatorFinalRound.mockResolvedValue(selected(6));
    await act(async () => complete({ event_version: 6 }));
    await waitFor(() => expect(screen.getByRole("button", { name: "指定を変更…" })).toBeEnabled());
    expect(screen.getByText("この問題が最終問題です。")).toBeVisible();
  });

  it("操作理由を入力せずに指定済みの現在問題を解除できる", async () => {
    finalApi.getOperatorFinalRound.mockResolvedValue(selected());
    render(<OperatorPage />);
    await screen.findByRole("button", { name: "指定を変更…" });
    expect(screen.queryByRole("textbox", { name: "監査記録へ残す理由（必須）" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "指定を変更…" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "指定を変更…" }));
    expect(finalApi.setOperatorFinalRound).not.toHaveBeenCalled();
    expect(within(screen.getByRole("dialog")).getByText("この問題の最終問題指定を解除します。")).toBeVisible();
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "最終問題の指定を解除" }));
    expect(finalApi.setOperatorFinalRound).toHaveBeenCalledWith(finalRoundIds.round, false,
      expect.objectContaining({ expected_version: 5 }), expect.any(AbortSignal));
    await act(async () => undefined);
  });

  it("指定情報を取得できない間は未指定と断定せず再確認だけ許可する", async () => {
    finalApi.getOperatorFinalRound.mockRejectedValueOnce(new Error("network"));
    render(<OperatorPage />);
    await screen.findByText("最終問題の指定を確認できませんでした。");
    const region = screen.getByRole("region", { name: "最終問題の指定" });
    expect(within(region).getByRole("button", { name: "この問題を最終問題にする" })).toBeDisabled();
    expect(within(region).queryByText("この問題は最終問題に指定されていません。")).not.toBeInTheDocument();
    fireEvent.click(within(region).getByRole("button", { name: "指定を再確認" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "この問題を最終問題にする" })).toBeEnabled());
  });

  it("最終指定GETの401では運営情報を消してログインへ戻す", async () => {
    finalApi.getOperatorFinalRound.mockRejectedValueOnce(new ApiResponseError(401, { code: "operator_session_invalid", message: "expired" }));
    render(<OperatorPage />);
    await screen.findByRole("button", { name: "ログイン" });
    expect(screen.queryByRole("heading", { name: "最終問題の指定" })).not.toBeInTheDocument();
  });
});
