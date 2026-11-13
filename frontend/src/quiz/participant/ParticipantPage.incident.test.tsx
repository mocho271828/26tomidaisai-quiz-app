import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ParticipantState, RoundState, TimeResponse } from "../../api/contracts";
import { ParticipantPage } from "./ParticipantPage";

const audienceApi = vi.hoisted(() => ({
  getAnswerStatus: vi.fn(),
  getParticipantIdCandidate: vi.fn(),
  getParticipantSession: vi.fn(),
  getServerTime: vi.fn(),
  heartbeatParticipant: vi.fn(),
  joinParticipant: vi.fn(),
  recordReadiness: vi.fn(),
  submitAnswer: vi.fn(),
}));

vi.mock("../../api/audienceClient", () => audienceApi);

class IncidentWebSocket extends EventTarget {
  static readonly OPEN = 1;
  static readonly CLOSED = 3;
  static instances: IncidentWebSocket[] = [];
  readyState = 0;

  constructor() {
    super();
    IncidentWebSocket.instances.push(this);
  }

  close(): void {
    if (this.readyState === IncidentWebSocket.CLOSED) return;
    this.readyState = IncidentWebSocket.CLOSED;
    this.dispatchEvent(new CloseEvent("close", { code: 1013 }));
  }
}

const startedAt = Date.parse("2026-09-30T02:00:00.000Z");
const questionPrompt = "ローカル障害調査用の架空問題";

function participantState(state: RoundState): ParticipantState {
  const isPreparing = state === "PreparingAssets";
  return {
    award_ceremony: null,
    projected_at: new Date(startedAt + (state === "Open" ? 3_000 : 0)).toISOString(),
    event: {
      timing_basis: "client_monotonic_v1", arrival_grace_ms: 2000, clock_tolerance_ms: 500, client_ahead_tolerance_ms: 2500,
      event_id: "61000000-0000-4000-8000-000000000001",
      event_title: "障害の局所再現",
      event_state: "InProgress",
      event_version: 2,
      start_delay_seconds: 3,
    },
    participant: { public_label: "ABC", is_ranking_eligible: true },
    current_round: {
      round_id: "61000000-0000-4000-8000-000000000002",
      round_number: 1,
      version: 3,
      state_version: state === "Open" ? 13 : 12,
      state,
      open_at: isPreparing ? null : new Date(startedAt + 3_000).toISOString(),
      standard_close_at: isPreparing ? null : new Date(startedAt + 13_000).toISOString(),
      question: {
        question_id: "61000000-0000-4000-8000-000000000003",
        prompt: questionPrompt,
        options: [
          { option_id: "61000000-0000-4000-8000-000000000004", label: "A", content: "選択肢" },
        ],
      },
    },
    latest_result: null,
    result_visibility: { distribution: false, personal_ranking: false, overall_ranking: false },
    latest_ranking_snapshot_version: null,
    personal_result_delivery: { status: "none", revision: null },
  };
}

async function advance(milliseconds: number): Promise<void> {
  await act(async () => vi.advanceTimersByTimeAsync(milliseconds));
}

function delayClockResponse(milliseconds: number): void {
  audienceApi.getServerTime.mockImplementation(() => new Promise<TimeResponse>((resolve) => {
    window.setTimeout(() => resolve({
      shared_sample_age_ms: 0, server_time: new Date(Date.now() - milliseconds / 2).toISOString(),
    }), milliseconds);
  }));
}

function expectClockWaiting(): void {
  expect(screen.getByRole("heading", { name: "しばらく お待ちください" })).toBeVisible();
  expect(screen.queryByText(questionPrompt)).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "A 選択肢" })).not.toBeInTheDocument();
}

// 旧版の症状はincident文書に残し、ここでは同じ境界条件でP0の回復を検証する。
// 実ブラウザ/TLS容量試験ではなく、React画面と通信境界の回帰test。
describe("2026-09-30 incident: P0回帰", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(startedAt);
    vi.resetAllMocks();
    window.sessionStorage.clear();
    IncidentWebSocket.instances = [];
    vi.stubGlobal("WebSocket", IncidentWebSocket);
    audienceApi.getServerTime.mockImplementation(async () => ({ shared_sample_age_ms: 0, server_time: new Date().toISOString() }));
    audienceApi.getParticipantSession.mockResolvedValueOnce(participantState("Scheduled"))
      .mockReturnValue(new Promise<ParticipantState>(() => undefined));
  });

  afterEach(() => {
    cleanup();
    vi.clearAllTimers();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it.each([1, 86])("%i画面でpolling・復帰が重なっても未完了GETは各1件でreadiness/heartbeatは0件", async (participants) => {
    audienceApi.getParticipantSession.mockReset();
    for (let index = 0; index < participants; index += 1) {
      audienceApi.getParticipantSession.mockResolvedValueOnce(participantState("PreparingAssets"));
    }
    audienceApi.getParticipantSession.mockReturnValue(new Promise<ParticipantState>(() => undefined));
    render(<>{Array.from({ length: participants }, (_, index) => <ParticipantPage key={index} />)}</>);
    await advance(0);
    expect(audienceApi.getParticipantSession).toHaveBeenCalledTimes(participants);
    await act(async () => IncidentWebSocket.instances.forEach((socket) => socket.close()));
    document.dispatchEvent(new Event("visibilitychange"));
    await advance(9_500);

    expect(audienceApi.getParticipantSession).toHaveBeenCalledTimes(participants * 2);
    expect(audienceApi.recordReadiness).not.toHaveBeenCalled();
    expect(audienceApi.heartbeatParticipant).not.toHaveBeenCalled();
  });

  it.each(["Scheduled", "Open"] as const)("%sと問題を受信できても最短RTT 600msなら回答窓中に問題を表示できない", async (state) => {
    // Openのprojectionは実際の開始後に受信する条件にする。
    vi.setSystemTime(startedAt + 3_000);
    audienceApi.getParticipantSession.mockResolvedValue(participantState(state));
    delayClockResponse(600);
    render(<ParticipantPage />);
    await advance(1_800);

    expect(audienceApi.getServerTime).toHaveBeenCalledTimes(3);
    expectClockWaiting();
    await advance(8_100);
    expect(Date.now()).toBe(startedAt + 12_900);
    expectClockWaiting();
  });

  it("初回RTT600msから回復後は通常周期を待たず再同期し10秒回答窓へ復帰する", async () => {
    delayClockResponse(600);
    render(<ParticipantPage />);
    await advance(1_800);
    expectClockWaiting();
    audienceApi.getServerTime.mockImplementation(async () => ({ shared_sample_age_ms: 0, server_time: new Date().toISOString() }));

    await advance(1_200);
    expect(audienceApi.getServerTime).toHaveBeenCalledTimes(6);
    expect(screen.getByRole("heading", { name: questionPrompt })).toBeVisible();
    expect(screen.getByRole("button", { name: "A 選択肢" })).toBeEnabled();
    expect(audienceApi.getParticipantSession).toHaveBeenCalledOnce();
  });

  it("RTT 500msの対照では状態GET更新なしでも開始時刻に選択肢が有効になる", async () => {
    delayClockResponse(500);
    render(<ParticipantPage />);
    await advance(1_500);
    expect(screen.getByRole("timer", { name: "回答開始まで残り1.5秒" })).toBeVisible();
    await advance(1_500);

    expect(screen.getByRole("heading", { name: questionPrompt })).toBeVisible();
    expect(screen.getByRole("button", { name: "A 選択肢" })).toBeEnabled();
    expect(audienceApi.getParticipantSession).toHaveBeenCalledOnce();
  });
});
