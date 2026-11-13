import { z } from "zod";

import {
  AwareDatetimeSchema,
  canonicalAwareDatetimeMicroseconds,
  type LoadTestManifest,
  type LoadTestRoundScenario,
} from "./contracts.js";
import {
  GeneratorEventSchema,
  type GeneratorEvent,
  type GeneratorVuRoundEvent,
} from "./events.js";
import type { ManifestHashes } from "./manifest.js";

const VIRTUAL_SESSION_COUNT = 600;
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}(?![\s\S])/;
const UUID4_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}(?![\s\S])/;
const SHA256_PATTERN = /^[0-9a-f]{64}(?![\s\S])/;

const safeIntegerSchema = z.number().refine(Number.isSafeInteger, "safe integer required");
const nonNegativeIntegerSchema = safeIntegerSchema.refine(
  (value) => value >= 0,
  "non-negative integer required",
);
const countSchema = nonNegativeIntegerSchema.refine(
  (value) => value <= 500,
  "answer count out of range",
);

export const GeneratorRoundOptionCountsSchema = z.strictObject({
  a: countSchema,
  b: countSchema,
  c: countSchema,
  d: countSchema,
});
export type GeneratorRoundOptionCounts = z.infer<
  typeof GeneratorRoundOptionCountsSchema
>;

/** Python `GeneratorRoundEvidence`とfield/literal/rangeを一致させた公開出力契約。 */
export const GeneratorRoundEvidenceSchema = z
  .strictObject({
    format: z.literal("load-test-generator-round-evidence-v2"),
    run_id: z.string().regex(UUID_PATTERN),
    event_id: z.string().regex(UUID_PATTERN),
    source_commit: z.string().regex(/^[0-9a-f]{40}(?![\s\S])/),
    schema_revision: z.string().min(1).max(128),
    manifest_sha256: z.string().regex(SHA256_PATTERN),
    scenario_sha256: z.string().regex(SHA256_PATTERN),
    round_number: safeIntegerSchema.refine(
      (value) => value >= 1 && value <= 10,
      "round number out of range",
    ),
    round_id: z.string().regex(UUID_PATTERN),
    question_id: z.string().regex(UUID_PATTERN),
    scenario_condition: z.enum([
      "none",
      "receipt-discard",
      "redis-unavailable",
      "redis-recovered",
      "api-secondary-unavailable",
      "api-secondary-recovered",
      "accumulated",
    ]),
    answer_population: z.enum(["mixed", "concentrated"]),
    virtual_session_count: z.literal(VIRTUAL_SESSION_COUNT),
    virtual_answer_count: countSchema,
    virtual_option_counts: GeneratorRoundOptionCountsSchema,
    started_answer_count: countSchema,
    resolved_answer_count: countSchema,
    request_start_width_ms: nonNegativeIntegerSchema,
    unresolved_answer_count: countSchema,
    missed_round_count: countSchema,
    duplicate_answer_count: countSchema,
    misleading_success_count: countSchema,
    replay_attempt_count: countSchema,
    replay_same_answer_id_count: countSchema,
    replay_row_change_count: countSchema,
    receipt_discard_count: countSchema,
    answer_status_query_count: countSchema,
    status_recovered_count: countSchema,
    sealed_at: AwareDatetimeSchema,
  })
  .superRefine((evidence, context) => {
    const optionTotal =
      evidence.virtual_option_counts.a +
      evidence.virtual_option_counts.b +
      evidence.virtual_option_counts.c +
      evidence.virtual_option_counts.d;
    if (optionTotal !== evidence.virtual_answer_count) {
      context.addIssue({
        code: "custom",
        message: "virtual option counts must total virtual answer count",
      });
    }
  });
export type GeneratorRoundEvidence = z.infer<typeof GeneratorRoundEvidenceSchema>;

export interface RoundAggregationContext {
  readonly manifest: LoadTestManifest;
  readonly hashes: ManifestHashes;
  readonly roundNumber: number;
}

export type RoundAggregationRefusalCode =
  | "round_aggregation_context_invalid"
  | "round_aggregation_event_binding_mismatch"
  | "round_aggregation_event_count_mismatch"
  | "round_aggregation_event_duplicate"
  | "round_aggregation_event_invalid"
  | "round_aggregation_event_time_invalid"
  | "round_aggregation_failed"
  | "round_aggregation_finalized"
  | "round_aggregation_round_identity_invalid"
  | "round_aggregation_seal_time_invalid"
  | "round_aggregation_vu_contract_mismatch";

/** eventやpathを例外へ埋め込まず、launcherが安全に記録できる固定codeだけを返す。 */
export class RoundAggregationRefusal extends Error {
  readonly code: RoundAggregationRefusalCode;

  constructor(code: RoundAggregationRefusalCode) {
    super(code);
    this.name = "RoundAggregationRefusal";
    this.code = code;
  }
}

interface RoundWindow {
  readonly startMicroseconds: bigint;
  readonly endMicroseconds: bigint;
}

/** 1 Roundの600 VU eventだけを受け付け、追加順に依存しない集計を生成する。 */
export class RoundEventAggregator {
  readonly #manifest: LoadTestManifest;
  readonly #hashes: ManifestHashes;
  readonly #roundNumber: number;
  readonly #scenario: LoadTestRoundScenario;
  readonly #questionId: string;
  readonly #window: RoundWindow;
  readonly #eventsByVu = new Map<number, GeneratorVuRoundEvent>();
  #roundId: string | null = null;
  #isFailed = false;
  #isFinalized = false;

  constructor(context: RoundAggregationContext) {
    if (
      !Number.isSafeInteger(context.roundNumber) ||
      context.roundNumber < 1 ||
      context.roundNumber > 10 ||
      !SHA256_PATTERN.test(context.hashes.manifestSha256) ||
      !SHA256_PATTERN.test(context.hashes.scenarioSha256)
    ) {
      throw new RoundAggregationRefusal("round_aggregation_context_invalid");
    }
    const scenarios = context.manifest.scenario.rounds.filter(
      (round) => round.number === context.roundNumber,
    );
    const questions = context.manifest.questions.filter(
      (question) => question.display_order === context.roundNumber,
    );
    if (scenarios.length !== 1 || questions.length !== 1) {
      throw new RoundAggregationRefusal("round_aggregation_context_invalid");
    }

    this.#manifest = context.manifest;
    this.#hashes = context.hashes;
    this.#roundNumber = context.roundNumber;
    const scenario = scenarios[0];
    const question = questions[0];
    if (scenario === undefined || question === undefined) {
      throw new RoundAggregationRefusal("round_aggregation_context_invalid");
    }
    this.#scenario = scenario;
    this.#questionId = question.id;
    this.#window = roundWindow(context.manifest, scenario);
  }

  get eventCount(): number {
    return this.#eventsByVu.size;
  }

  add(eventValue: unknown): void {
    this.#requireOpen();
    try {
      const result = GeneratorEventSchema.safeParse(eventValue);
      if (!result.success) {
        throw new RoundAggregationRefusal("round_aggregation_event_invalid");
      }
      const event = result.data;
      this.#requireBinding(event);
      this.#requireVuContract(event);
      this.#requireEventTime(event);
      if (this.#eventsByVu.has(event.vu_id)) {
        throw new RoundAggregationRefusal("round_aggregation_event_duplicate");
      }
      this.#eventsByVu.set(event.vu_id, event);
      this.#roundId ??= event.round_id;
    } catch (error: unknown) {
      this.#isFailed = true;
      this.#discardEvents();
      if (error instanceof RoundAggregationRefusal) {
        throw error;
      }
      throw new RoundAggregationRefusal("round_aggregation_event_invalid");
    }
  }

  finalize(sealedAt: string): GeneratorRoundEvidence {
    this.#requireOpen();
    if (this.#eventsByVu.size !== VIRTUAL_SESSION_COUNT) {
      return this.#fail("round_aggregation_event_count_mismatch");
    }
    if (this.#roundId === null) {
      return this.#fail("round_aggregation_round_identity_invalid");
    }

    const sealedAtResult = AwareDatetimeSchema.safeParse(sealedAt);
    if (!sealedAtResult.success) {
      return this.#fail("round_aggregation_seal_time_invalid");
    }
    const sealedAtMicroseconds = canonicalAwareDatetimeMicroseconds(sealedAtResult.data);
    if (sealedAtMicroseconds === null) {
      return this.#fail("round_aggregation_seal_time_invalid");
    }
    const events = [...this.#eventsByVu.values()].sort((left, right) => left.vu_id - right.vu_id);
    let latestObservation = this.#window.startMicroseconds;
    for (const event of events) {
      const observedAt = canonicalAwareDatetimeMicroseconds(event.observed_at);
      if (observedAt === null) {
        return this.#fail("round_aggregation_event_time_invalid");
      }
      if (observedAt > latestObservation) {
        latestObservation = observedAt;
      }
    }
    if (
      sealedAtMicroseconds < this.#window.startMicroseconds ||
      sealedAtMicroseconds >= this.#window.endMicroseconds ||
      sealedAtMicroseconds < latestObservation
    ) {
      return this.#fail("round_aggregation_seal_time_invalid");
    }

    const answeringEvents = events.filter((event) => event.answer.expected);
    const requestStarts = answeringEvents.flatMap((event) =>
      event.answer.request_started_at_unix_ms === null
        ? []
        : [event.answer.request_started_at_unix_ms],
    );
    const optionCounts: GeneratorRoundOptionCounts = { a: 0, b: 0, c: 0, d: 0 };
    for (const event of answeringEvents) {
      if (!event.answer.expected) {
        return this.#fail("round_aggregation_event_invalid");
      }
      const key = event.answer.selected_option.toLowerCase() as keyof GeneratorRoundOptionCounts;
      optionCounts[key] += 1;
    }

    const evidenceValue: GeneratorRoundEvidence = {
      format: "load-test-generator-round-evidence-v2",
      run_id: this.#manifest.run_id,
      event_id: this.#manifest.event.id,
      source_commit: this.#manifest.artifacts.source_commit,
      schema_revision: this.#manifest.schema_revision,
      manifest_sha256: this.#hashes.manifestSha256,
      scenario_sha256: this.#hashes.scenarioSha256,
      round_number: this.#roundNumber,
      round_id: this.#roundId,
      question_id: this.#questionId,
      scenario_condition: this.#scenario.condition,
      answer_population: this.#scenario.answer_population,
      virtual_session_count: VIRTUAL_SESSION_COUNT,
      virtual_answer_count: answeringEvents.length,
      virtual_option_counts: optionCounts,
      started_answer_count: requestStarts.length,
      resolved_answer_count: count(answeringEvents, (event) => event.answer.resolved),
      request_start_width_ms: requestStartWidth(requestStarts),
      unresolved_answer_count: count(
        answeringEvents,
        (event) => event.answer.request_started_at_unix_ms !== null && !event.answer.resolved,
      ),
      missed_round_count: count(
        answeringEvents,
        (event) => event.answer.request_started_at_unix_ms === null,
      ),
      duplicate_answer_count: count(answeringEvents, (event) => event.answer.duplicate),
      misleading_success_count: count(
        answeringEvents,
        (event) => event.answer.misleading_success,
      ),
      replay_attempt_count: count(answeringEvents, (event) => event.replay.attempted),
      replay_same_answer_id_count: count(
        answeringEvents,
        (event) => event.replay.same_answer_id,
      ),
      replay_row_change_count: count(answeringEvents, (event) => event.replay.row_changed),
      receipt_discard_count: count(
        answeringEvents,
        (event) => event.status_recovery.receipt_discarded,
      ),
      answer_status_query_count: count(
        answeringEvents,
        (event) => event.status_recovery.status_queried,
      ),
      status_recovered_count: count(
        answeringEvents,
        (event) => event.status_recovery.recovered,
      ),
      sealed_at: sealedAtResult.data,
    };
    const evidence = GeneratorRoundEvidenceSchema.safeParse(evidenceValue);
    if (!evidence.success) {
      return this.#fail("round_aggregation_event_invalid");
    }
    this.#discardEvents();
    this.#isFinalized = true;
    return evidence.data;
  }

  #requireBinding(event: GeneratorEvent): void {
    if (
      event.run_id !== this.#manifest.run_id ||
      event.event_id !== this.#manifest.event.id ||
      event.source_commit !== this.#manifest.artifacts.source_commit ||
      event.schema_revision !== this.#manifest.schema_revision ||
      event.manifest_sha256 !== this.#hashes.manifestSha256 ||
      event.scenario_sha256 !== this.#hashes.scenarioSha256 ||
      event.round_number !== this.#roundNumber ||
      event.question_id !== this.#questionId ||
      event.scenario_condition !== this.#scenario.condition ||
      event.answer_population !== this.#scenario.answer_population
    ) {
      throw new RoundAggregationRefusal("round_aggregation_event_binding_mismatch");
    }

    if (this.#roundNumber === 1 && event.round_id !== this.#manifest.initial_round.id) {
      throw new RoundAggregationRefusal("round_aggregation_round_identity_invalid");
    }
    if (this.#roundNumber > 1 && !UUID4_PATTERN.test(event.round_id)) {
      throw new RoundAggregationRefusal("round_aggregation_round_identity_invalid");
    }
    if (this.#roundId !== null && event.round_id !== this.#roundId) {
      throw new RoundAggregationRefusal("round_aggregation_event_binding_mismatch");
    }
  }

  #requireVuContract(event: GeneratorEvent): void {
    const shouldAnswer = this.#scenario.answer_population === "mixed"
      ? event.vu_id <= 498 || event.vu_id === 599 || event.vu_id === 600
      : event.vu_id <= this.#scenario.virtual_answer_count;
    if (event.answer.expected !== shouldAnswer) {
      throw new RoundAggregationRefusal("round_aggregation_vu_contract_mismatch");
    }
    if (event.answer.expected && event.answer.selected_option !== this.#expectedOption(event.vu_id)) {
      throw new RoundAggregationRefusal("round_aggregation_vu_contract_mismatch");
    }
    const discardStart = this.#scenario.receipt_discard_vu_start;
    const discardEnd = this.#scenario.receipt_discard_vu_end;
    const isReceiptDiscardTarget =
      this.#scenario.condition === "receipt-discard" &&
      discardStart !== null &&
      discardEnd !== null &&
      event.vu_id >= discardStart &&
      event.vu_id <= discardEnd;
    const hasStatusRecoveryAction =
      event.status_recovery.receipt_discarded ||
      event.status_recovery.status_queried ||
      event.status_recovery.recovered;
    if (hasStatusRecoveryAction && !isReceiptDiscardTarget) {
      throw new RoundAggregationRefusal("round_aggregation_vu_contract_mismatch");
    }
  }

  #expectedOption(vuId: number): "A" | "B" | "C" | "D" {
    const oracle = this.#manifest.scenario.answer_oracle;
    if (vuId >= oracle.modulo_vu_start && vuId <= oracle.modulo_vu_end) {
      const index = (vuId - oracle.modulo_vu_start) % oracle.modulo_option_cycle.length;
      const option = oracle.modulo_option_cycle[index];
      if (option !== undefined) {
        return option;
      }
    }
    const fixed = oracle.fixed_virtual_answers.find((answer) => answer.vu_id === vuId);
    if (fixed !== undefined) {
      return fixed.option;
    }
    throw new RoundAggregationRefusal("round_aggregation_vu_contract_mismatch");
  }

  #requireEventTime(event: GeneratorEvent): void {
    const observedAtMicroseconds = canonicalAwareDatetimeMicroseconds(event.observed_at);
    const requestStartedAtMilliseconds = event.answer.request_started_at_unix_ms;
    if (observedAtMicroseconds === null) {
      throw new RoundAggregationRefusal("round_aggregation_event_time_invalid");
    }
    const requestStartedAtMicroseconds =
      requestStartedAtMilliseconds === null
        ? null
        : BigInt(requestStartedAtMilliseconds) * 1_000n;
    if (
      observedAtMicroseconds < this.#window.startMicroseconds ||
      observedAtMicroseconds >= this.#window.endMicroseconds ||
      (requestStartedAtMicroseconds !== null &&
        (requestStartedAtMicroseconds < this.#window.startMicroseconds ||
          requestStartedAtMicroseconds >= this.#window.endMicroseconds ||
          requestStartedAtMicroseconds > observedAtMicroseconds))
    ) {
      throw new RoundAggregationRefusal("round_aggregation_event_time_invalid");
    }
  }

  #requireOpen(): void {
    if (this.#isFailed) {
      throw new RoundAggregationRefusal("round_aggregation_failed");
    }
    if (this.#isFinalized) {
      throw new RoundAggregationRefusal("round_aggregation_finalized");
    }
  }

  #fail(code: RoundAggregationRefusalCode): never {
    this.#isFailed = true;
    this.#discardEvents();
    throw new RoundAggregationRefusal(code);
  }

  #discardEvents(): void {
    this.#eventsByVu.clear();
    this.#roundId = null;
  }
}

export function aggregateRoundEvents(
  eventValues: readonly unknown[],
  context: RoundAggregationContext,
  sealedAt: string,
): GeneratorRoundEvidence {
  const aggregator = new RoundEventAggregator(context);
  for (const event of eventValues) {
    aggregator.add(event);
  }
  return aggregator.finalize(sealedAt);
}

function count(
  events: readonly GeneratorVuRoundEvent[],
  predicate: (event: GeneratorVuRoundEvent) => boolean,
): number {
  let result = 0;
  for (const event of events) {
    if (predicate(event)) {
      result += 1;
    }
  }
  return result;
}

function requestStartWidth(requestStarts: readonly number[]): number {
  if (requestStarts.length < 2) {
    return 0;
  }
  return Math.max(...requestStarts) - Math.min(...requestStarts);
}

function roundWindow(
  manifest: LoadTestManifest,
  scenario: LoadTestRoundScenario,
): RoundWindow {
  const scheduledStartMicroseconds = canonicalAwareDatetimeMicroseconds(
    manifest.scheduled_start_at,
  );
  const laterOffsets = manifest.scenario.rounds
    .filter((candidate) => candidate.start_offset_seconds > scenario.start_offset_seconds)
    .map((candidate) => candidate.start_offset_seconds);
  const endOffsetSeconds =
    laterOffsets.length === 0
      ? manifest.scenario.lifecycle.measurement_end_seconds
      : Math.min(...laterOffsets);
  if (scheduledStartMicroseconds === null) {
    throw new RoundAggregationRefusal("round_aggregation_context_invalid");
  }
  const startMicroseconds =
    scheduledStartMicroseconds + BigInt(scenario.start_offset_seconds) * 1_000_000n;
  const endMicroseconds =
    scheduledStartMicroseconds + BigInt(endOffsetSeconds) * 1_000_000n;
  if (endMicroseconds <= startMicroseconds) {
    throw new RoundAggregationRefusal("round_aggregation_context_invalid");
  }
  return { startMicroseconds, endMicroseconds };
}
