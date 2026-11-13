import { describe, expect, it } from "vitest";

import { personalResultResponseSchema, scoredParticipantOutcomeSchema, timeResponseSchema } from "./contracts";
import { realtimeServerMessageSchema } from "./realtimeContracts";

const eventId = "71000000-0000-4000-8000-000000000001";
const roundId = "71000000-0000-4000-8000-000000000002";
function response() {
  return {
    delivery: { status: "ready", revision: {
      event_id: eventId, event_version: 4, result_round_id: roundId, result_round_version: 3,
      ranking_snapshot_version: 2,
      result_visibility: { distribution: false, personal_ranking: true, overall_ranking: false },
    } },
    result: { selected_option_id: null,
      outcome: {
        timing_basis: "server_received_v1",  status: "Scored", is_correct: true, total_points: 450, scoring_policy_version: "v1",
        base_points: 400, speed_points: 50, elapsed_ms: 5000,
        speed_cohort: "Correct", speed_rank: 1, speed_cohort_size: 2 },
      ranking: { snapshot_version: 2, overall_rank: 1, total_points: 450, correct_count: 1, top_rows: null },
    },
  };
}

describe("personal result and shared clock contracts", () => {
  it("保存済み内訳と回答時間を必須とし、欠測と0msを区別する", () => {
    const valid = response();
    for (const elapsed_ms of [null, 0, 2399, 2400]) {
      const outcome = { ...valid.result.outcome, elapsed_ms };
      expect(scoredParticipantOutcomeSchema.parse(outcome)).toEqual(outcome);
    }
    for (const invalid of [
      { base_points: undefined }, { speed_points: undefined }, { elapsed_ms: undefined },
      { base_points: -1 }, { speed_points: -1 }, { elapsed_ms: -1 },
      { base_points: 400.5 }, { speed_points: 50.5 }, { elapsed_ms: 0.5 },
      { base_points: "400" }, { speed_points: "50" }, { elapsed_ms: "5000" },
      { base_points: null }, { speed_points: null },
      { total_points: 451 }, { base_points: 399 }, { speed_points: 51 },
    ]) {
      expect(scoredParticipantOutcomeSchema.safeParse({ ...valid.result.outcome, ...invalid }).success).toBe(false);
    }
  });

  it("本人順位を隠しても本人得点の内訳と受付時間は維持する", () => {
    const valid = response();
    valid.delivery.revision.result_visibility.personal_ranking = false;
    const hiddenRanking = { ...valid, result: {
      ...valid.result, ranking: null,
      outcome: { ...valid.result.outcome, speed_rank: null, speed_cohort_size: null },
    } };
    expect(personalResultResponseSchema.parse(hiddenRanking).result?.outcome).toMatchObject({
      total_points: 450, base_points: 400, speed_points: 50, elapsed_ms: 5000,
      speed_rank: null, speed_cohort_size: null,
    });
  });

  it("本人結果のready版とpendingを区別して厳密に検証する", () => {
    const valid = response();
    expect(personalResultResponseSchema.safeParse(valid).success).toBe(true);
    expect(personalResultResponseSchema.safeParse({ delivery: { ...valid.delivery, status: "pending" }, result: null }).success).toBe(true);
    for (const invalid of [
      { ...valid, result: null },
      { ...valid, delivery: { status: "pending", revision: null } },
      { ...valid, delivery: { ...valid.delivery, status: "pending" } },
      { ...valid, result: { ...valid.result, outcome: null } },
      { ...valid, delivery: { ...valid.delivery, revision: { ...valid.delivery.revision, result_round_id: null } } },
      { ...valid, all_participants: {} },
      { ...valid, result: { ...valid.result, participant_id: roundId } },
    ]) expect(personalResultResponseSchema.safeParse(invalid).success).toBe(false);
  });

  it("非公開順位/TOP3または別snapshotの行を含めない", () => {
    const valid = response();
    const hidden = { ...valid.delivery.revision, result_visibility: {
      distribution: false, personal_ranking: false, overall_ranking: false,
    } };
    expect(personalResultResponseSchema.safeParse({ ...valid, delivery: { ...valid.delivery, revision: hidden } }).success).toBe(false);
    expect(personalResultResponseSchema.safeParse({ ...valid, result: {
      ...valid.result, ranking: { ...valid.result.ranking, snapshot_version: 3 },
    } }).success).toBe(false);
    expect(personalResultResponseSchema.safeParse({ ...valid, result: {
      ...valid.result, ranking: { ...valid.result.ranking, top_rows: [] },
    } }).success).toBe(false);
  });

  it("本人専用WSは別開催と別audienceを拒否し、heartbeatは版通知だけを受理する", () => {
    const envelope = { audience: "participant", event_id: eventId, stream_id: roundId, sequence: 1 };
    const message = { ...envelope, type: "participant.result", result: response() };
    expect(realtimeServerMessageSchema.safeParse(message).success).toBe(true);
    expect(realtimeServerMessageSchema.safeParse({ ...message, audience: "operator" }).success).toBe(false);
    expect(realtimeServerMessageSchema.safeParse({ ...message, event_id: roundId }).success).toBe(false);
    const heartbeat = { ...envelope, type: "heartbeat", personal_result_delivery: response().delivery };
    expect(realtimeServerMessageSchema.safeParse(heartbeat).success).toBe(true);
    expect(realtimeServerMessageSchema.safeParse({ ...heartbeat, audience: "operator" }).success).toBe(false);
    expect(realtimeServerMessageSchema.safeParse({ ...heartbeat, event_id: roundId }).success).toBe(false);
    expect(realtimeServerMessageSchema.safeParse({ ...heartbeat, result: response().result }).success).toBe(false);
  });

  it("時刻標本の合流年齢を必須とし負数・非有限値を拒否する", () => {
    const value = { server_time: "2026-10-01T00:00:00Z", shared_sample_age_ms: 0 };
    expect(timeResponseSchema.safeParse(value).success).toBe(true);
    expect(timeResponseSchema.safeParse({ server_time: value.server_time }).success).toBe(false);
    for (const age of [-1, Infinity, Number.NaN]) {
      expect(timeResponseSchema.safeParse({ ...value, shared_sample_age_ms: age }).success).toBe(false);
    }
  });
});
