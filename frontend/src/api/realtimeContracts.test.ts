import { describe, expect, it } from "vitest";

import {
  participantCommonStateSchema,
  participantStateMessageSchema,
  pollingRequiredMessageSchema,
  realtimeServerMessageSchema,
} from "./realtimeContracts";

const eventId = "00000000-0000-4000-8000-000000000001";
const common = {
    award_ceremony: null,
  projected_at: "2026-10-01T00:00:00Z",
  event: {
  timing_basis: "client_monotonic_v1", arrival_grace_ms: 2000, clock_tolerance_ms: 500, client_ahead_tolerance_ms: 2500,  event_id: eventId, event_title: "Quiz", event_state: "LobbyOpen", event_version: 1, start_delay_seconds: 3 },
  current_round: null,
  latest_result: null,
  result_visibility: { distribution: false, personal_ranking: false, overall_ranking: false },
  latest_ranking_snapshot_version: null,
  personal_result_delivery: { status: "none", revision: null },
} as const;
const snapshot = {
  type: "participant.state", audience: "participant", event_id: eventId,
  stream_id: "00000000-0000-4000-8000-000000000002", sequence: 1, state: common,
} as const;

describe("realtime contracts", () => {
  it("共通snapshotと同じ接続のheartbeat/controlを検証する", () => {
    expect(participantStateMessageSchema.safeParse(snapshot).success).toBe(true);
    for (const type of ["heartbeat", "session.changed"]) {
      const envelope = {
        audience: snapshot.audience, event_id: snapshot.event_id,
        stream_id: snapshot.stream_id, sequence: snapshot.sequence,
      };
      expect(realtimeServerMessageSchema.safeParse({ ...envelope, type, sequence: 2 }).success).toBe(true);
    }
  });

  it("本人データ、旧invalidation、別audience、異なる開催、余分なfieldを拒否する", () => {
    for (const field of ["participant", "answer", "ranking", "outcome", "available_questions"]) {
      expect(participantCommonStateSchema.safeParse({ ...common, [field]: {} }).success).toBe(false);
    }
    expect(participantStateMessageSchema.safeParse({ ...snapshot, event_id: snapshot.stream_id }).success).toBe(false);
    expect(participantStateMessageSchema.safeParse({ ...snapshot, audience: "operator" }).success).toBe(false);
    expect(participantStateMessageSchema.safeParse({ ...snapshot, sequence: 0 }).success).toBe(false);
    expect(participantStateMessageSchema.safeParse({ ...snapshot, stream_id: "00000000-0000-1000-8000-000000000002" }).success).toBe(false);
    expect(realtimeServerMessageSchema.safeParse({ type: "state.invalidated", audience: "participant", event_id: eventId }).success).toBe(false);
    expect(realtimeServerMessageSchema.safeParse({ ...snapshot, audience: "public" }).success).toBe(false);
  });

  it("公開結果内にも本人選択/本人得点を混ぜない", () => {
    const result = {
      round_id: snapshot.stream_id, round_number: 2, version: 3, state_version: 12, state: "Reveal",
      open_at: null, standard_close_at: null,
      question: { question_id: snapshot.stream_id, prompt: "問題", options: [] },
      result: { publication: "Published", correct_option_id: snapshot.stream_id, distribution: null },
    };
    expect(participantCommonStateSchema.safeParse({ ...common, latest_result: result }).success).toBe(true);
    expect(participantCommonStateSchema.safeParse({ ...common, latest_result: { ...result, selected_option_id: null } }).success).toBe(false);
    expect(participantCommonStateSchema.safeParse({ ...common, latest_result: { ...result, result: { ...result.result, outcome: { status: "Unanswered" } } } }).success).toBe(false);
  });

  it("障害時だけ5秒pollingへの縮退を受理する", () => {
    const fallback = { type: "polling.required", reason: "realtime_unavailable", poll_after_ms: 5_000 } as const;
    expect(pollingRequiredMessageSchema.safeParse(fallback).success).toBe(true);
    expect(realtimeServerMessageSchema.safeParse(fallback).success).toBe(true);
    expect(pollingRequiredMessageSchema.safeParse({ ...fallback, poll_after_ms: 1_000 }).success).toBe(false);
  });
});
