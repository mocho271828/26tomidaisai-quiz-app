import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { canonicalJsonBytes } from "../src/canonical-json.js";
import {
  GeneratorRoundEvidenceSchema,
  RoundEventAggregator,
  aggregateRoundEvents,
} from "../src/round-aggregator.js";
import type { GeneratorEvent } from "../src/events.js";
import {
  MANIFEST_SHA256,
  SCENARIO_SHA256,
  buildEvent,
  buildManifest,
  buildRoundEvents,
  canonicalTimestamp,
  roundId,
  roundStartMilliseconds,
  sealTime,
} from "./generator-event-fixtures.js";

function context(manifest: ReturnType<typeof buildManifest>, roundNumber: number) {
  return {
    manifest,
    hashes: {
      manifestSha256: MANIFEST_SHA256,
      scenarioSha256: SCENARIO_SHA256,
    },
    roundNumber,
  };
}

function expectRefusal(action: () => unknown, code: string): void {
  expect(action).toThrow(code);
}

function replaceEvent(
  events: readonly GeneratorEvent[],
  vuId: number,
  replacement: GeneratorEvent,
): GeneratorEvent[] {
  return events.map((event) => (event.vu_id === vuId ? replacement : event));
}

describe("RoundEventAggregator", () => {
  it("mixed Roundの600 session/500 answerをPython evidence shapeへexact集約する", () => {
    const manifest = buildManifest();
    const evidence = aggregateRoundEvents(
      buildRoundEvents(manifest, 1).reverse(),
      context(manifest, 1),
      sealTime(manifest, 1),
    );

    expect(GeneratorRoundEvidenceSchema.parse(evidence)).toEqual(evidence);
    const sharedBody = readFileSync(
      new URL("../../fixtures/generator-round-evidence-v2.json", import.meta.url),
    );
    const sharedEvidence = GeneratorRoundEvidenceSchema.parse(
      JSON.parse(sharedBody.toString("utf8")),
    );
    expect(evidence).toEqual(sharedEvidence);
    expect(sharedBody).toEqual(
      Buffer.concat([Buffer.from(canonicalJsonBytes(evidence)), Buffer.from("\n")]),
    );
    expect(createHash("sha256").update(sharedBody).digest("hex")).toBe(
      "04e4b0a3843114f59b35039c0f1e331b64582c447f34ab8099351257a0d88df9",
    );
    expect(evidence).toEqual({
      format: "load-test-generator-round-evidence-v2",
      run_id: manifest.run_id,
      event_id: manifest.event.id,
      source_commit: manifest.artifacts.source_commit,
      schema_revision: manifest.schema_revision,
      manifest_sha256: MANIFEST_SHA256,
      scenario_sha256: SCENARIO_SHA256,
      round_number: 1,
      round_id: manifest.initial_round.id,
      question_id: manifest.questions[0]?.id,
      scenario_condition: "none",
      answer_population: "mixed",
      virtual_session_count: 600,
      virtual_answer_count: 500,
      virtual_option_counts: { a: 126, b: 125, c: 125, d: 124 },
      started_answer_count: 500,
      resolved_answer_count: 500,
      request_start_width_ms: 499,
      unresolved_answer_count: 0,
      missed_round_count: 0,
      duplicate_answer_count: 0,
      misleading_success_count: 0,
      replay_attempt_count: 0,
      replay_same_answer_id_count: 0,
      replay_row_change_count: 0,
      receipt_discard_count: 0,
      answer_status_query_count: 0,
      status_recovered_count: 0,
      sealed_at: sealTime(manifest, 1),
    });
  });

  it("R3/R10の全再送とR4の50 status recoveryを集約する", () => {
    const manifest = buildManifest();
    const replay = aggregateRoundEvents(
      buildRoundEvents(manifest, 3),
      context(manifest, 3),
      sealTime(manifest, 3),
    );
    const recovery = aggregateRoundEvents(
      buildRoundEvents(manifest, 4),
      context(manifest, 4),
      sealTime(manifest, 4),
    );
    const finalReplay = aggregateRoundEvents(
      buildRoundEvents(manifest, 10),
      context(manifest, 10),
      sealTime(manifest, 10),
    );

    expect(replay.virtual_option_counts).toEqual({ a: 126, b: 125, c: 125, d: 124 });
    expect(replay.replay_attempt_count).toBe(500);
    expect(replay.replay_same_answer_id_count).toBe(500);
    expect(replay.replay_row_change_count).toBe(0);
    expect(recovery.receipt_discard_count).toBe(50);
    expect(recovery.answer_status_query_count).toBe(50);
    expect(recovery.status_recovered_count).toBe(50);
    expect(finalReplay.replay_attempt_count).toBe(500);
    expect(finalReplay.replay_same_answer_id_count).toBe(500);
  });

  it("missedとunresolvedを混同せず、失敗factsとして残す", () => {
    const manifest = buildManifest();
    let events = buildRoundEvents(manifest, 1);
    const first = events[0];
    const second = events[1];
    if (first === undefined || second === undefined || !first.answer.expected || !second.answer.expected) {
      throw new Error("fixture_answer_missing");
    }
    events = replaceEvent(events, 1, {
      ...first,
      answer: {
        ...first.answer,
        request_started_at_unix_ms: null,
        resolved: false,
      },
    });
    events = replaceEvent(events, 2, {
      ...second,
      answer: { ...second.answer, resolved: false },
    });

    const evidence = aggregateRoundEvents(
      events,
      context(manifest, 1),
      sealTime(manifest, 1),
    );

    expect(evidence.started_answer_count).toBe(499);
    expect(evidence.resolved_answer_count).toBe(498);
    expect(evidence.missed_round_count).toBe(1);
    expect(evidence.unresolved_answer_count).toBe(1);
  });

  it("missing/duplicate VUを拒否し、拒否後のaggregatorを再利用させない", () => {
    const manifest = buildManifest();
    const events = buildRoundEvents(manifest, 1);
    const incomplete = new RoundEventAggregator(context(manifest, 1));
    for (const event of events.slice(0, -1)) {
      incomplete.add(event);
    }
    expectRefusal(
      () => incomplete.finalize(sealTime(manifest, 1)),
      "round_aggregation_event_count_mismatch",
    );
    expect(incomplete.eventCount).toBe(0);

    const duplicate = new RoundEventAggregator(context(manifest, 1));
    const first = events[0];
    if (first === undefined) {
      throw new Error("fixture_event_missing");
    }
    duplicate.add(first);
    expectRefusal(() => duplicate.add(first), "round_aggregation_event_duplicate");
    expect(duplicate.eventCount).toBe(0);
    expectRefusal(
      () => duplicate.finalize(sealTime(manifest, 1)),
      "round_aggregation_failed",
    );
  });

  it("successful finalize後もVU eventをmemoryへ保持しない", () => {
    const manifest = buildManifest();
    const aggregator = new RoundEventAggregator(context(manifest, 1));
    for (const event of buildRoundEvents(manifest, 1)) {
      aggregator.add(event);
    }

    aggregator.finalize(sealTime(manifest, 1));

    expect(aggregator.eventCount).toBe(0);
  });

  it("manifest/hash/question/condition bindingの差替えを拒否する", () => {
    const manifest = buildManifest();
    const base = buildEvent(manifest, 1, 1);
    const mutations: GeneratorEvent[] = [
      { ...base, manifest_sha256: "e".repeat(64) },
      { ...base, scenario_sha256: "e".repeat(64) },
      { ...base, question_id: "20000000-0000-5000-8000-000000000001" },
      { ...base, scenario_condition: "redis-unavailable" },
      { ...base, answer_population: "concentrated" },
    ];

    for (const mutation of mutations) {
      const aggregator = new RoundEventAggregator(context(manifest, 1));
      expectRefusal(
        () => aggregator.add(mutation),
        "round_aggregation_event_binding_mismatch",
      );
    }
  });

  it("R1はmanifest UUIDv5 exact、R2以降は同一UUIDv4だけを許可する", () => {
    const manifest = buildManifest();
    const firstRound = buildEvent(manifest, 1, 1);
    const secondRound = buildEvent(manifest, 2, 1);

    expectRefusal(
      () =>
        new RoundEventAggregator(context(manifest, 1)).add({
          ...firstRound,
          round_id: roundId(2),
        }),
      "round_aggregation_round_identity_invalid",
    );
    expectRefusal(
      () =>
        new RoundEventAggregator(context(manifest, 2)).add({
          ...secondRound,
          round_id: "20000000-0000-5000-8000-000000000002",
        }),
      "round_aggregation_round_identity_invalid",
    );

    const inconsistent = new RoundEventAggregator(context(manifest, 2));
    inconsistent.add(secondRound);
    expectRefusal(
      () => inconsistent.add({ ...buildEvent(manifest, 2, 2), round_id: roundId(3) }),
      "round_aggregation_event_binding_mismatch",
    );
  });

  it("VU role偽装とRound window外event/requestを拒否する", () => {
    const manifest = buildManifest();
    const answering = buildEvent(manifest, 1, 1);
    const start = roundStartMilliseconds(manifest, 1);
    const roleMismatch: GeneratorEvent = {
      ...answering,
      answer: {
        expected: false,
        selected_option: null,
        request_started_at_unix_ms: null,
        resolved: false,
        duplicate: false,
        misleading_success: false,
      },
    };
    expectRefusal(
      () => new RoundEventAggregator(context(manifest, 1)).add(roleMismatch),
      "round_aggregation_vu_contract_mismatch",
    );
    expectRefusal(
      () =>
        new RoundEventAggregator(context(manifest, 1)).add({
          ...answering,
          answer: { ...answering.answer, selected_option: "B" },
        }),
      "round_aggregation_vu_contract_mismatch",
    );
    expectRefusal(
      () =>
        new RoundEventAggregator(context(manifest, 1)).add({
          ...answering,
          observed_at: canonicalTimestamp(start - 1),
        }),
      "round_aggregation_event_time_invalid",
    );
    if (!answering.answer.expected) {
      throw new Error("fixture_answer_missing");
    }
    expectRefusal(
      () =>
        new RoundEventAggregator(context(manifest, 1)).add({
          ...answering,
          answer: {
            ...answering.answer,
            request_started_at_unix_ms: Date.parse(answering.observed_at) + 1,
          },
        }),
      "round_aggregation_event_time_invalid",
    );
  });

  it("R4 status recovery対象をVU 1..50から差し替えられない", () => {
    const manifest = buildManifest();
    const outsideTarget = buildEvent(manifest, 4, 51);

    expectRefusal(
      () =>
        new RoundEventAggregator(context(manifest, 4)).add({
          ...outsideTarget,
          status_recovery: {
            receipt_discarded: true,
            status_queried: true,
            recovered: true,
          },
        }),
      "round_aggregation_vu_contract_mismatch",
    );
  });

  it("seal時刻をRound windowと最新eventへ束縛する", () => {
    const manifest = buildManifest();
    const events = buildRoundEvents(manifest, 1);
    const tooEarly = new RoundEventAggregator(context(manifest, 1));
    const outside = new RoundEventAggregator(context(manifest, 1));
    for (const event of events) {
      tooEarly.add(event);
      outside.add(event);
    }

    expectRefusal(
      () =>
        tooEarly.finalize(
          canonicalTimestamp(roundStartMilliseconds(manifest, 1) + 1_999),
        ),
      "round_aggregation_seal_time_invalid",
    );
    expectRefusal(
      () => outside.finalize(sealTime(manifest, 2)),
      "round_aggregation_seal_time_invalid",
    );
  });

  it("sub-millisecondの最新eventより1 microsecond早いsealを拒否する", () => {
    const manifest = buildManifest();
    const sealedAt = sealTime(manifest, 1);
    const events = buildRoundEvents(manifest, 1);
    const first = events[0];
    if (first === undefined) {
      throw new Error("fixture_event_missing");
    }
    events[0] = {
      ...first,
      observed_at: sealedAt.replace(/Z$/, ".000001Z"),
    };

    expectRefusal(
      () => aggregateRoundEvents(events, context(manifest, 1), sealedAt),
      "round_aggregation_seal_time_invalid",
    );
  });
});
