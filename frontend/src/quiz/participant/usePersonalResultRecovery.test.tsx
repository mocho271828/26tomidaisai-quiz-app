import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiResponseError, ApiTransportError } from "../../api/client";
import type { PersonalResultResponse } from "../../api/contracts";
import { personalResultRevision, type ParticipantViewState } from "./participantState";
import { usePersonalResultRecovery } from "./usePersonalResultRecovery";
import { cancellationReason } from "../../api/cancellation";

const api = vi.hoisted(() => ({ getParticipantResult: vi.fn() }));
vi.mock("../../api/audienceClient", () => api);
const id = "71000000-0000-4000-8000-000000000001";

function state(status: "pending" | "ready" | "unavailable" = "ready"): ParticipantViewState {
  const value: ParticipantViewState = {
    award_ceremony: null,
    projected_at: "2026-10-01T00:00:00Z",
    event: {
    timing_basis: "client_monotonic_v1", arrival_grace_ms: 2000, clock_tolerance_ms: 500, client_ahead_tolerance_ms: 2500,  event_id: id, event_version: 2, event_state: "InProgress", event_title: "Quiz", start_delay_seconds: 3 },
    participant: { public_label: "ABC", is_ranking_eligible: true },
    current_round: null, latest_result: null,
    latest_ranking_snapshot_version: 1,
    result_visibility: { distribution: false, personal_ranking: true, overall_ranking: false },
    personal_result_delivery: { status: "none", revision: null },
  };
  return { ...value, personal_result_delivery: { status, revision: personalResultRevision(value) } };
}
function response(value = state()): PersonalResultResponse {
  return { delivery: { status: "ready", revision: personalResultRevision(value) }, result: {
    selected_option_id: null, outcome: null,
    ranking: { snapshot_version: 1, overall_rank: 1, total_points: 100, correct_count: 1, top_rows: null },
  } };
}
function visibility(value: DocumentVisibilityState): void {
  vi.spyOn(document, "visibilityState", "get").mockReturnValue(value);
  document.dispatchEvent(new Event("visibilitychange"));
}

describe("personal result recovery", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    vi.spyOn(Math, "random").mockReturnValue(0.5);
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  });
  afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

  it("deferred/鮮度不明の間は表示待ちが続いてもGETしない", async () => {
    const onResult = vi.fn();
    const view = renderHook(({ value }) => usePersonalResultRecovery({ state: value, onResult, onAuthenticationLost: vi.fn() }),
      { initialProps: { value: state("pending") } });
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    view.rerender({ value: state("unavailable") });
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(api.getParticipantResult).not.toHaveBeenCalled();
    expect(onResult).not.toHaveBeenCalled();
  });

  it("ready後に本人版が未達の端末だけ5〜6秒後に補完し、同じ通知で期限を伸ばさない", async () => {
    api.getParticipantResult.mockResolvedValue(response());
    const onResult = vi.fn();
    const view = renderHook(({ value }) => usePersonalResultRecovery({ state: value, onResult, onAuthenticationLost: vi.fn() }),
      { initialProps: { value: state("pending") } });
    view.rerender({ value: state() });
    await act(async () => vi.advanceTimersByTimeAsync(5_000));
    view.rerender({ value: state() });
    expect(api.getParticipantResult).not.toHaveBeenCalled();
    await act(async () => vi.advanceTimersByTimeAsync(500));
    expect(api.getParticipantResult).toHaveBeenCalledOnce();
    expect(onResult).toHaveBeenCalledWith(response());
    view.rerender({ value: { ...state(), received_result_revision: personalResultRevision(state()) } });
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(api.getParticipantResult).toHaveBeenCalledOnce();
  });

  it("一時失敗の補完を3回までに制限し、heartbeatで上限を戻さない", async () => {
    api.getParticipantResult.mockRejectedValue(new ApiTransportError(new Error("offline")));
    const view = renderHook(({ value }) => usePersonalResultRecovery({ state: value, onResult: vi.fn(), onAuthenticationLost: vi.fn() }),
      { initialProps: { value: state() } });
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(api.getParticipantResult).toHaveBeenCalledTimes(3);
    view.rerender({ value: state() });
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(api.getParticipantResult).toHaveBeenCalledTimes(3);
  });

  it.each(["pending", "unavailable", "received", "hidden", "unmount"] as const)("%sでtimerを取り消す", async (reason) => {
    const view = renderHook(({ value }) => usePersonalResultRecovery({ state: value, onResult: vi.fn(), onAuthenticationLost: vi.fn() }),
      { initialProps: { value: state() } });
    await act(async () => vi.advanceTimersByTimeAsync(4_000));
    if (reason === "pending" || reason === "unavailable") view.rerender({ value: state(reason) });
    if (reason === "received") view.rerender({ value: { ...state(), received_result_revision: personalResultRevision(state()) } });
    if (reason === "hidden") await act(async () => visibility("hidden"));
    if (reason === "unmount") view.unmount();
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(api.getParticipantResult).not.toHaveBeenCalled();
  });

  it("readyからunavailableになったら進行中の補完GETをabortし遅着結果を適用しない", async () => {
    let finish: (result: PersonalResultResponse) => void = () => undefined;
    api.getParticipantResult.mockReturnValue(new Promise<PersonalResultResponse>((resolve) => { finish = resolve; }));
    const onResult = vi.fn();
    const view = renderHook(({ value }) => usePersonalResultRecovery({ state: value, onResult, onAuthenticationLost: vi.fn() }),
      { initialProps: { value: state() } });
    await act(async () => vi.advanceTimersByTimeAsync(5_500));
    const signal = api.getParticipantResult.mock.calls[0]?.[0] as AbortSignal;
    expect(signal.aborted).toBe(false);
    view.rerender({ value: state("unavailable") });
    expect(signal.aborted).toBe(true);
    await act(async () => finish(response()));
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(onResult).not.toHaveBeenCalled();
    expect(api.getParticipantResult).toHaveBeenCalledOnce();
  });

  it.each(["received", "hidden", "unmount", "deadline"] as const)("進行中補完の%sを期限超過と正常取消に分ける", async (reason) => {
    api.getParticipantResult.mockReturnValue(new Promise(() => undefined));
    const view = renderHook(({ value }) => usePersonalResultRecovery({ state: value, onResult: vi.fn(), onAuthenticationLost: vi.fn() }),
      { initialProps: { value: state() } });
    await act(async () => vi.advanceTimersByTimeAsync(5_500));
    const signal = api.getParticipantResult.mock.calls[0]?.[0] as AbortSignal;
    if (reason === "received") view.rerender({ value: { ...state(), received_result_revision: personalResultRevision(state()) } });
    if (reason === "hidden") await act(async () => visibility("hidden"));
    if (reason === "unmount") view.unmount();
    if (reason === "deadline") await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(cancellationReason(signal)).toBe(reason === "received" ? "result_received" : reason);
  });

  it.each(["auth_invalidated", "auth_without_state_change", "revision_changed", "state_missing", "unavailable"] as const)(
    "%sの取消をcommit済み状態に基づいて分類し、不明な取消を正常扱いにしない", async (reason) => {
      api.getParticipantResult.mockReturnValue(new Promise(() => undefined));
      const initialProps: { value: ParticipantViewState | null; authenticationInvalidated: boolean } = {
        value: state(), authenticationInvalidated: false,
      };
      const view = renderHook(({ value, authenticationInvalidated }: {
        value: ParticipantViewState | null; authenticationInvalidated: boolean;
      }) => usePersonalResultRecovery({ state: value, authenticationInvalidated,
        onResult: vi.fn(), onAuthenticationLost: vi.fn() }),
      { initialProps });
      await act(async () => vi.advanceTimersByTimeAsync(5_500));
      const signal = api.getParticipantResult.mock.calls[0]?.[0] as AbortSignal;
      let next: ParticipantViewState | null = state();
      if (reason === "auth_invalidated" || reason === "state_missing") next = null;
      if (reason === "unavailable") next = state("unavailable");
      if (reason === "revision_changed" && next !== null) next.event = { ...next.event, event_version: 3 };
      view.rerender({ value: next,
        authenticationInvalidated: reason === "auth_invalidated" || reason === "auth_without_state_change" });
      expect(signal.aborted).toBe(true);
      expect(cancellationReason(signal)).toBe(reason === "auth_without_state_change" ? "auth_invalidated"
        : reason === "state_missing" || reason === "unavailable" ? undefined : reason);
      await act(async () => vi.advanceTimersByTimeAsync(60_000));
      expect(api.getParticipantResult).toHaveBeenCalledOnce();
    },
  );

  it("古い結果GETは非公開化後の版と復帰後の世代へ適用しない", async () => {
    let finish: (result: PersonalResultResponse) => void = () => undefined;
    api.getParticipantResult.mockReturnValue(new Promise<PersonalResultResponse>((resolve) => { finish = resolve; }));
    const onResult = vi.fn();
    const view = renderHook(({ value }) => usePersonalResultRecovery({ state: value, onResult, onAuthenticationLost: vi.fn() }),
      { initialProps: { value: state() } });
    await act(async () => vi.advanceTimersByTimeAsync(5_500));
    await act(async () => { visibility("hidden"); visibility("visible"); finish(response()); });
    expect(onResult).not.toHaveBeenCalled();
    const hidden = state("pending");
    hidden.event = { ...hidden.event, event_version: 3 };
    hidden.result_visibility = { distribution: false, personal_ranking: false, overall_ranking: false };
    hidden.personal_result_delivery = { status: "pending", revision: personalResultRevision(hidden) };
    view.rerender({ value: hidden });
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(onResult).not.toHaveBeenCalled();
    expect(api.getParticipantResult).toHaveBeenCalledOnce();
  });

  it("401では失効を通知し、結果補完の再試行を止める", async () => {
    api.getParticipantResult.mockRejectedValue(new ApiResponseError(401, { code: "participant_authentication_required", message: "Expired" }));
    const lost = vi.fn();
    renderHook(() => usePersonalResultRecovery({ state: state(), onResult: vi.fn(), onAuthenticationLost: lost }));
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(lost).toHaveBeenCalledOnce();
    expect(api.getParticipantResult).toHaveBeenCalledOnce();
  });
});
