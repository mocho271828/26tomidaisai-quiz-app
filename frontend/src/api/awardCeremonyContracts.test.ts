import { describe, expect, it } from "vitest";
import { awardCeremonySchema, participantStateSchema } from "./contracts";
import { participantCommonStateSchema } from "./realtimeContracts";

const row = { public_label: "ABC", overall_rank: 5 };
const common = {
  projected_at: "2026-10-22T00:00:00Z",
  event: {
    event_id: "00000000-0000-4000-8000-000000000001", event_title: "Quiz", event_state: "Completed",
    event_version: 10, start_delay_seconds: 3, timing_basis: "client_monotonic_v1",
    arrival_grace_ms: 2000, clock_tolerance_ms: 500, client_ahead_tolerance_ms: 2500,
  },
  current_round: null, latest_result: null, latest_ranking_snapshot_version: null,
  result_visibility: { distribution: false, personal_ranking: false, overall_ranking: false },
  personal_result_delivery: { status: "none", revision: null },
  award_ceremony: { phase: "Waiting", top_rows: null },
};

describe("表彰の参加者公開契約", () => {
  it("共通状態と本人状態はCompletedの場合だけ表彰段階を必須とする", () => {
    for (const schema of [participantCommonStateSchema, participantStateSchema]) {
      const source = schema === participantStateSchema
        ? { ...common, participant: { public_label: "ABC", is_ranking_eligible: true } } : common;
      expect(schema.safeParse(source).success).toBe(true);
      expect(schema.safeParse({ ...source, award_ceremony: null }).success).toBe(false);
      expect(schema.safeParse({ ...source, award_ceremony: undefined }).success).toBe(false);
      expect(schema.safeParse({ ...source, event: { ...source.event, event_state: "InProgress" } }).success).toBe(false);
      expect(schema.safeParse({ ...source, event: { ...source.event, event_state: "InProgress" }, award_ceremony: null }).success).toBe(true);
    }
  });

  it("Announcedだけ上位5位を受理し、同順位を人数で切らない", () => {
    expect(awardCeremonySchema.safeParse({ phase: "Announced", top_rows: [] }).success).toBe(true);
    const rows = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"].map((public_label) => ({ ...row, public_label }));
    expect(awardCeremonySchema.safeParse({ phase: "Announced", top_rows: rows }).success).toBe(true);
    for (const phase of ["Waiting", "Finished"]) {
      expect(awardCeremonySchema.safeParse({ phase, top_rows: null }).success).toBe(true);
      expect(awardCeremonySchema.safeParse({ phase, top_rows: rows }).success).toBe(false);
    }
    for (const invalid of [null, [{ ...row, overall_rank: 6 }], [{ ...row, overall_rank: 0 }], [{ ...row, public_label: "private-id" }]]) {
      expect(awardCeremonySchema.safeParse({ phase: "Announced", top_rows: invalid }).success).toBe(false);
    }
    expect(awardCeremonySchema.safeParse({ phase: "Other", top_rows: null }).success).toBe(false);
  });
});
