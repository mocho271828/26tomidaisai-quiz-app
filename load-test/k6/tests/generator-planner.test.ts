import { describe, expect, it } from "vitest";

import { LoadTestManifestSchema, type OptionLabel } from "../src/contracts.js";
import {
  buildGeneratorExecutionPlan,
  GeneratorExecutionPlanSchema,
  GeneratorPlannerRefusal,
  GeneratorVuRoundPlanSchema,
  type GeneratorExecutionPlan,
  type GeneratorVuPlan,
} from "../src/generator/planner.js";
import { validManifestFixture } from "./fixtures/manifest.js";

const manifest = LoadTestManifestSchema.parse(validManifestFixture());
const plan = buildGeneratorExecutionPlan(manifest);

function virtualUser(executionPlan: GeneratorExecutionPlan, vuId: number): GeneratorVuPlan {
  const virtualUserPlan = executionPlan.virtual_users[vuId - 1];
  if (virtualUserPlan === undefined || virtualUserPlan.vu_id !== vuId) {
    throw new Error("test_virtual_user_plan_missing");
  }
  return virtualUserPlan;
}

function optionCounts(
  executionPlan: GeneratorExecutionPlan,
  roundNumber: number,
): Readonly<Record<OptionLabel, number>> {
  const counts: Record<OptionLabel, number> = { A: 0, B: 0, C: 0, D: 0 };
  for (const virtualUserPlan of executionPlan.virtual_users) {
    const round = virtualUserPlan.rounds[roundNumber - 1];
    if (round?.selected_option !== null && round?.selected_option !== undefined) {
      counts[round.selected_option] += 1;
    }
  }
  return counts;
}

describe("generator execution planner", () => {
  it("同じmanifestから600 VU×10 Roundを常に同じ順序で生成する", () => {
    const secondPlan = buildGeneratorExecutionPlan(structuredClone(manifest));

    expect(plan).toEqual(secondPlan);
    expect(plan.virtual_user_count).toBe(600);
    expect(plan.round_count).toBe(10);
    expect(plan.virtual_users).toHaveLength(600);
    expect(plan.virtual_users.flatMap((virtualUserPlan) => virtualUserPlan.rounds)).toHaveLength(
      6_000,
    );
    expect(GeneratorExecutionPlanSchema.safeParse(plan).success).toBe(true);
    expect(virtualUser(plan, 600).rounds.map((round) => round.round_number)).toEqual([
      1, 2, 3, 4, 5, 6, 7, 8, 9, 10,
    ]);
  });

  it("plannerは入力manifestを変更しない", () => {
    const input = structuredClone(manifest);
    const before = structuredClone(input);

    buildGeneratorExecutionPlan(input);

    expect(input).toEqual(before);
  });

  it.each([
    [1, 500, 100, { A: 126, B: 125, C: 125, D: 124 }],
    [2, 500, 100, { A: 126, B: 125, C: 125, D: 124 }],
    [3, 500, 100, { A: 126, B: 125, C: 125, D: 124 }],
    [4, 500, 100, { A: 126, B: 125, C: 125, D: 124 }],
    [5, 500, 100, { A: 126, B: 125, C: 125, D: 124 }],
    [6, 500, 100, { A: 126, B: 125, C: 125, D: 124 }],
    [7, 500, 100, { A: 126, B: 125, C: 125, D: 124 }],
    [8, 500, 100, { A: 126, B: 125, C: 125, D: 124 }],
    [9, 500, 100, { A: 126, B: 125, C: 125, D: 124 }],
    [10, 500, 100, { A: 126, B: 125, C: 125, D: 124 }],
  ] as const)(
    "Round %iのanswerer/observer/option oracleを固定する",
    (roundNumber, answererCount, observerCount, expectedCounts) => {
      const rounds = plan.virtual_users.map(
        (virtualUserPlan) => virtualUserPlan.rounds[roundNumber - 1],
      );

      expect(rounds.filter((round) => round?.role === "answerer")).toHaveLength(
        answererCount,
      );
      expect(rounds.filter((round) => round?.role === "observer")).toHaveLength(
        observerCount,
      );
      expect(optionCounts(plan, roundNumber)).toEqual(expectedCounts);
    },
  );

  it("modulo/fixed oracleとobserverの境界を正確に割り当てる", () => {
    expect(virtualUser(plan, 1).rounds[0]?.selected_option).toBe("A");
    expect(virtualUser(plan, 2).rounds[0]?.selected_option).toBe("B");
    expect(virtualUser(plan, 496).rounds[0]?.selected_option).toBe("D");
    expect(virtualUser(plan, 497).rounds[0]?.selected_option).toBe("A");
    expect(virtualUser(plan, 498).rounds[0]?.selected_option).toBe("A");
    expect(virtualUser(plan, 499).rounds[0]).toMatchObject({
      role: "observer",
      selected_option: null,
    });
    expect(virtualUser(plan, 499).rounds[2]?.selected_option).toBe("B");
    expect(virtualUser(plan, 500).rounds[2]?.selected_option).toBe("C");
    expect(virtualUser(plan, 501).rounds[2]).toMatchObject({
      role: "observer",
      selected_option: null,
    });
  });

  it("question/VUごとのUUIDv5を6000件すべて一意に固定する", () => {
    const clientAnswerIds = plan.virtual_users.flatMap((virtualUserPlan) =>
      virtualUserPlan.rounds.map((round) => round.client_answer_id),
    );

    expect(new Set(clientAnswerIds).size).toBe(6_000);
    expect(virtualUser(plan, 1).rounds[0]?.client_answer_id).toBe(
      "6830027b-dcdb-55d3-ba33-be1450cb2855",
    );
    expect(virtualUser(plan, 600).rounds[0]?.client_answer_id).toBe(
      "4e8695c4-e021-5788-8162-02c8a889c536",
    );
  });

  it("Round 3/10のanswererだけに同じIDのreplay scheduleを割り当てる", () => {
    const replayPlans = plan.virtual_users.flatMap((virtualUserPlan) =>
      virtualUserPlan.rounds.filter((round) => round.replay_required),
    );
    const base = Date.parse(manifest.scheduled_start_at);

    expect(replayPlans).toHaveLength(1_000);
    expect(new Set(replayPlans.map((round) => round.round_number))).toEqual(
      new Set([3, 10]),
    );
    expect(virtualUser(plan, 1).rounds[2]?.schedule).toMatchObject({
      answer_send_offset_ms: 100,
      answer_send_at_unix_ms: base + 1_620_100,
      replay_send_offset_ms: 1_100,
      replay_send_at_unix_ms: base + 1_621_100,
    });
    expect(virtualUser(plan, 500).rounds[2]?.schedule).toMatchObject({
      answer_send_offset_ms: 599,
      replay_send_offset_ms: 1_599,
    });
    expect(virtualUser(plan, 501).rounds[2]?.schedule.replay_send_at_unix_ms).toBeNull();
  });

  it("Round 4のVU 1〜50だけにreceipt破棄とstatus照会を割り当てる", () => {
    const statusPlans = plan.virtual_users.flatMap((virtualUserPlan) =>
      virtualUserPlan.rounds.filter((round) => round.status_discard.enabled),
    );
    const base = Date.parse(manifest.scheduled_start_at);

    expect(statusPlans).toHaveLength(50);
    expect(virtualUser(plan, 1).rounds[3]?.status_discard).toEqual({
      enabled: true,
      discard_only_when_status: "Stored",
      query_delay_ms: 2_500,
      retry_only_when_status: "NotStored",
    });
    expect(virtualUser(plan, 1).rounds[3]?.schedule).toMatchObject({
      answer_send_offset_ms: 100,
      status_query_offset_ms: 2_600,
      status_query_at_unix_ms: base + 1_982_600,
    });
    expect(virtualUser(plan, 50).rounds[3]?.schedule.status_query_offset_ms).toBe(2_649);
    expect(virtualUser(plan, 51).rounds[3]?.status_discard.enabled).toBe(false);
    expect(virtualUser(plan, 51).rounds[3]?.schedule.status_query_at_unix_ms).toBeNull();
  });

  it("ramp境界と全answererの1秒内送信時刻を整数msで固定する", () => {
    const base = Date.parse(manifest.scheduled_start_at);

    expect(virtualUser(plan, 1).session_start_offset_ms).toBe(1_500);
    expect(virtualUser(plan, 100).session_start_offset_ms).toBe(150_000);
    expect(virtualUser(plan, 101).session_start_offset_ms).toBe(150_750);
    expect(virtualUser(plan, 300).session_start_offset_ms).toBe(300_000);
    expect(virtualUser(plan, 500).session_start_offset_ms).toBe(450_000);
    expect(virtualUser(plan, 501).session_start_offset_ms).toBe(451_500);
    expect(virtualUser(plan, 600).session_start_offset_ms).toBe(600_000);
    expect(virtualUser(plan, 600).session_stop_offset_ms).toBe(4_500_500);
    expect(virtualUser(plan, 1).session_stop_offset_ms).toBe(4_800_000);

    expect(virtualUser(plan, 1).rounds[0]?.schedule).toMatchObject({
      round_start_offset_ms: 900_000,
      round_start_at_unix_ms: base + 900_000,
      answer_send_offset_ms: 100,
      answer_send_at_unix_ms: base + 900_100,
    });
    expect(virtualUser(plan, 498).rounds[0]?.schedule.answer_send_offset_ms).toBe(597);
    expect(virtualUser(plan, 499).rounds[0]?.schedule.answer_send_at_unix_ms).toBeNull();
    expect(virtualUser(plan, 500).rounds[9]?.schedule.answer_send_offset_ms).toBe(599);
  });

  it("role/replay/status scheduleの不整合をZod出力契約で拒否する", () => {
    const round = structuredClone(virtualUser(plan, 1).rounds[2]);
    if (round === undefined) {
      throw new Error("test_round_missing");
    }
    round.role = "observer";

    expect(GeneratorVuRoundPlanSchema.safeParse(round).success).toBe(false);
  });

  it("未検証manifestを固定refusal codeで拒否する", () => {
    const invalidManifest = structuredClone(validManifestFixture());
    invalidManifest.scenario.lifecycle.virtual_sessions = 597;

    expect(() => buildGeneratorExecutionPlan(invalidManifest)).toThrowError(
      new GeneratorPlannerRefusal("generator_plan_manifest_invalid"),
    );
  });
});
