import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiResponseError } from "../../api/client";
import type { OperatorState } from "../../api/operatorContracts";
import { OperatorPage } from "./OperatorPage";
import { mockDialogMethods } from "./confirmationDialogTestSupport";
import { finalRoundIds, finalRoundProjection } from "./final-round/finalRoundTestFixtures";
import type { RealtimeRefreshOptions } from "../_hooks/useRealtimeRefresh";

const api = vi.hoisted(() => ({ getOperatorSession: vi.fn(), getOperatorState: vi.fn(), getOperatorRanking: vi.fn() }));
const finalApi = vi.hoisted(() => ({ getOperatorFinalRound: vi.fn() }));
const awardApi = vi.hoisted(() => ({ setAwardCeremony: vi.fn() }));
const realtime = vi.hoisted(() => ({ options: null as RealtimeRefreshOptions | null }));
vi.mock("../../api/operatorClient", () => api);
vi.mock("../../api/operatorFinalRoundClient", () => finalApi);
vi.mock("../../api/operatorAwardCeremonyClient", () => awardApi);
vi.mock("../_hooks/useRealtimeRefresh", () => ({ useRealtimeRefresh: (options: RealtimeRefreshOptions) => { realtime.options = options; return "live"; } }));

function completed(phase: OperatorState["award_ceremony_phase"] = "Waiting", version = 6): OperatorState {
  const state = finalRoundProjection();
  return { ...state, event_state: "Completed", event_version: version, award_ceremony_phase: phase,
    current_round: { ...state.current_round!, state: "RoundBackupValidated", result_publication: "Published", can_publish_results: false } };
}

function confirmAnnouncement(): void {
  fireEvent.click(screen.getByRole("button", { name: "表彰対象者を呼び出す…" }));
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "呼び出し・最終ランキングを一斉公開" }));
}

describe("運営の表彰進行", () => {
  let restoreDialogMethods: () => void;
  beforeEach(() => {
    vi.resetAllMocks();
    restoreDialogMethods = mockDialogMethods();
    api.getOperatorSession.mockResolvedValue({ operator: { account_id: finalRoundIds.question, username: "operator", role: "Operator" } });
    api.getOperatorState.mockResolvedValue(completed());
    api.getOperatorRanking.mockResolvedValue(null);
    finalApi.getOperatorFinalRound.mockResolvedValue({ event_id: finalRoundIds.event, event_version: 6, final_round_id: finalRoundIds.round });
  });
  afterEach(() => { cleanup(); restoreDialogMethods(); });

  it("終了待機→呼び出し→終了案内を確認付きで進める", async () => {
    render(<OperatorPage />);
    await screen.findByRole("button", { name: "表彰対象者を呼び出す…" });
    expect(screen.queryByRole("button", { name: "アンケート・ホームページ案内へ…" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "表彰対象者を呼び出す…" }));
    expect(screen.getByText(/同率5位も全員が対象/)).toBeVisible();
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "キャンセル" }));
    expect(awardApi.setAwardCeremony).not.toHaveBeenCalled();

    api.getOperatorState.mockResolvedValue(completed("Announced", 7));
    awardApi.setAwardCeremony.mockResolvedValue({ event_version: 7 });
    confirmAnnouncement();
    await screen.findByRole("button", { name: "アンケート・ホームページ案内へ…" });
    expect(awardApi.setAwardCeremony).toHaveBeenCalledWith("Announced", {
      expected_version: 6, idempotency_key: expect.any(String),
    }, expect.any(AbortSignal));

    api.getOperatorState.mockResolvedValue(completed("Finished", 8));
    awardApi.setAwardCeremony.mockResolvedValue({ event_version: 8 });
    fireEvent.click(screen.getByRole("button", { name: "アンケート・ホームページ案内へ…" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "アンケート・ホームページ案内を表示" }));
    await screen.findByText("アンケート・ホームページへの案内を表示中です。");
    expect(awardApi.setAwardCeremony).toHaveBeenLastCalledWith("Finished", expect.objectContaining({ expected_version: 7 }), expect.any(AbortSignal));
    expect(screen.queryByRole("button", { name: "表彰対象者を呼び出す…" })).not.toBeInTheDocument();
  });

  it("終了前は操作を出さず、終了後は操作理由を入力せずに表彰を進められる", async () => {
    api.getOperatorState.mockResolvedValue(finalRoundProjection());
    render(<OperatorPage />);
    await screen.findByRole("heading", { name: "集計完了・未公開" });
    expect(screen.queryByRole("heading", { name: "最終結果発表・表彰" })).not.toBeInTheDocument();
    api.getOperatorState.mockResolvedValue(completed());
    await act(async () => { await realtime.options?.refresh(); });
    expect(screen.queryByRole("textbox", { name: "監査記録へ残す理由（必須）" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "表彰対象者を呼び出す…" })).toBeEnabled();
    api.getOperatorState.mockResolvedValue(completed("Announced", 7));
    awardApi.setAwardCeremony.mockResolvedValue({ event_version: 7 });
    confirmAnnouncement();
    expect(awardApi.setAwardCeremony).toHaveBeenCalledWith("Announced", {
      expected_version: 6, idempotency_key: expect.any(String),
    }, expect.any(AbortSignal));
    await screen.findByRole("button", { name: "アンケート・ホームページ案内へ…" });
  });

  it("確認中に版が変わったら確認を閉じる", async () => {
    render(<OperatorPage />);
    fireEvent.click(await screen.findByRole("button", { name: "表彰対象者を呼び出す…" }));
    api.getOperatorState.mockResolvedValue(completed("Waiting", 7));
    await act(async () => { await realtime.options?.refresh(); });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(awardApi.setAwardCeremony).not.toHaveBeenCalled();
  });

  it("二重送信を防ぎ、成功後も新しい版の取得まで操作を保留する", async () => {
    let resolve: (value: { event_version: number }) => void = () => undefined;
    awardApi.setAwardCeremony.mockReturnValue(new Promise((done) => { resolve = done; }));
    render(<OperatorPage />);
    await screen.findByRole("button", { name: "表彰対象者を呼び出す…" });
    confirmAnnouncement();
    expect(screen.getByRole("button", { name: "表彰対象者を呼び出す…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "ログアウト" })).toBeDisabled();
    expect(awardApi.setAwardCeremony).toHaveBeenCalledOnce();
    await act(async () => { resolve({ event_version: 7 }); });
    await screen.findByRole("button", { name: "表示状態を再確認" });
    expect(screen.queryByRole("button", { name: "表彰対象者を呼び出す…" })).not.toBeInTheDocument();
    api.getOperatorState.mockResolvedValue(completed("Announced", 7));
    fireEvent.click(screen.getByRole("button", { name: "表示状態を再確認" }));
    await screen.findByRole("button", { name: "アンケート・ホームページ案内へ…" });
  });

  it("結果不明の再試行には同じ冪等キーを使う", async () => {
    awardApi.setAwardCeremony.mockRejectedValue(new ApiResponseError(503, { code: "unavailable", message: "再試行してください" }));
    render(<OperatorPage />);
    await screen.findByRole("button", { name: "表彰対象者を呼び出す…" });
    confirmAnnouncement();
    await screen.findByText("再試行してください");
    await waitFor(() => expect(screen.getByRole("button", { name: "表彰対象者を呼び出す…" })).toBeEnabled());
    const firstMutation: unknown = awardApi.setAwardCeremony.mock.calls[0]![1];
    confirmAnnouncement();
    await waitFor(() => expect(awardApi.setAwardCeremony).toHaveBeenCalledTimes(2));
    expect(awardApi.setAwardCeremony.mock.calls[1]![1]).toEqual(firstMutation);
    await act(async () => undefined);
  });

  it("認証失効時は表彰操作を消してログインへ戻す", async () => {
    awardApi.setAwardCeremony.mockRejectedValue(new ApiResponseError(401, { code: "operator_session_invalid", message: "expired" }));
    render(<OperatorPage />);
    await screen.findByRole("button", { name: "表彰対象者を呼び出す…" });
    confirmAnnouncement();
    await screen.findByRole("button", { name: "ログイン" });
    expect(screen.queryByRole("heading", { name: "最終結果発表・表彰" })).not.toBeInTheDocument();
  });
});
