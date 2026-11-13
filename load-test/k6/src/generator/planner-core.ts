export const GENERATOR_VIRTUAL_USER_COUNT = 600;
export const GENERATOR_ROUND_COUNT = 10;
export const GENERATOR_FIRST_ANSWER_OFFSET_MS = 100;
export const GENERATOR_REPLAY_DELAY_MS = 1_000;

export type GeneratorOptionLabel = "A" | "B" | "C" | "D";

interface GeneratorPlanningQuestion {
  readonly id: string;
}

interface GeneratorPlanningLifecycle {
  readonly ramp_start_seconds: number;
  readonly ramp_down_start_seconds: number;
  readonly ramp_down_end_seconds: number;
  readonly ramp_targets: readonly number[];
  readonly ramp_target_offsets_seconds: readonly number[];
}

interface GeneratorPlanningOracle {
  readonly modulo_vu_start: number;
  readonly modulo_vu_end: number;
  readonly modulo_option_cycle: readonly GeneratorOptionLabel[];
  readonly fixed_virtual_answers: readonly {
    readonly vu_id: number;
    readonly option: GeneratorOptionLabel;
  }[];
}

interface GeneratorPlanningRound {
  readonly number: number;
  readonly start_offset_seconds: number;
  readonly virtual_answer_count: number;
  readonly answer_population: "mixed" | "concentrated";
  readonly replay: "all" | "none";
  readonly condition: string;
  readonly receipt_discard_vu_start: number | null;
  readonly receipt_discard_vu_end: number | null;
  readonly receipt_discard_only_when_status: "Stored" | null;
  readonly answer_status_delay_ms: number | null;
  readonly retry_only_when_status: "NotStored" | null;
}

export interface GeneratorPlanningManifest {
  readonly run_id: string;
  readonly scheduled_start_at: string;
  readonly questions: readonly GeneratorPlanningQuestion[];
  readonly scenario: {
    readonly lifecycle: GeneratorPlanningLifecycle;
    readonly answer_oracle: GeneratorPlanningOracle;
    readonly rounds: readonly GeneratorPlanningRound[];
  };
}

export interface GeneratorCoreStatusDiscardPlan {
  readonly enabled: boolean;
  readonly discard_only_when_status: "Stored" | null;
  readonly query_delay_ms: number | null;
  readonly retry_only_when_status: "NotStored" | null;
}

export interface GeneratorCoreRoundPlan {
  readonly round_number: number;
  readonly question_id: string;
  readonly role: "answerer" | "observer";
  readonly selected_option: GeneratorOptionLabel | null;
  readonly client_answer_id: string;
  readonly replay_required: boolean;
  readonly status_discard: GeneratorCoreStatusDiscardPlan;
  readonly schedule: {
    readonly round_start_offset_ms: number;
    readonly round_start_at_unix_ms: number;
    readonly answer_send_offset_ms: number | null;
    readonly answer_send_at_unix_ms: number | null;
    readonly replay_send_offset_ms: number | null;
    readonly replay_send_at_unix_ms: number | null;
    readonly status_query_offset_ms: number | null;
    readonly status_query_at_unix_ms: number | null;
  };
}

export interface GeneratorCoreVuPlan {
  readonly vu_id: number;
  readonly session_start_offset_ms: number;
  readonly session_stop_offset_ms: number;
  readonly rounds: readonly GeneratorCoreRoundPlan[];
}

export type GeneratorClientAnswerIdFactory = (
  runId: string,
  questionId: string,
  vuId: number,
) => string;

/**
 * Node plannerとk6 VUが共有する純粋な1 VU分の割当て。
 * UUID実装を境界から注入し、k6 bundleへNode cryptoを混入させない。
 */
export function planGeneratorVirtualUser(
  manifest: GeneratorPlanningManifest,
  vuId: number,
  createClientAnswerId: GeneratorClientAnswerIdFactory,
): GeneratorCoreVuPlan {
  if (
    !Number.isSafeInteger(vuId) ||
    vuId < 1 ||
    vuId > GENERATOR_VIRTUAL_USER_COUNT
  ) {
    throw new Error("generator_plan_vu_invalid");
  }
  const scheduledStartMilliseconds = Date.parse(manifest.scheduled_start_at);
  if (!Number.isSafeInteger(scheduledStartMilliseconds) || scheduledStartMilliseconds < 0) {
    throw new Error("generator_plan_time_invalid");
  }

  return {
    vu_id: vuId,
    session_start_offset_ms: sessionStartOffsetMilliseconds(manifest, vuId),
    session_stop_offset_ms: sessionStopOffsetMilliseconds(manifest, vuId),
    rounds: manifest.scenario.rounds.map((scenario) => {
      const question = manifest.questions[scenario.number - 1];
      if (question === undefined) {
        throw new Error("generator_plan_round_invalid");
      }
      return planRound(
        manifest,
        scheduledStartMilliseconds,
        vuId,
        scenario,
        question.id,
        createClientAnswerId,
      );
    }),
  };
}

function planRound(
  manifest: GeneratorPlanningManifest,
  scheduledStartMilliseconds: number,
  vuId: number,
  scenario: GeneratorPlanningRound,
  questionId: string,
  createClientAnswerId: GeneratorClientAnswerIdFactory,
): GeneratorCoreRoundPlan {
  const isAnswerer = scenario.answer_population === "mixed"
    ? vuId <= 498 || vuId === 599 || vuId === 600
    : vuId <= scenario.virtual_answer_count;
  const selectedOption = isAnswerer ? optionForVu(manifest, vuId) : null;
  const answerOffset = isAnswerer
    ? GENERATOR_FIRST_ANSWER_OFFSET_MS + (vuId > 598 ? vuId - 100 : vuId) - 1
    : null;
  const replayOffset =
    answerOffset !== null && scenario.replay === "all"
      ? answerOffset + GENERATOR_REPLAY_DELAY_MS
      : null;
  const isStatusDiscardTarget = statusDiscardTarget(scenario, vuId);
  const statusDelay = isStatusDiscardTarget ? scenario.answer_status_delay_ms : null;
  const statusOffset =
    answerOffset !== null && statusDelay !== null ? answerOffset + statusDelay : null;
  const roundStartOffset = scenario.start_offset_seconds * 1_000;
  const roundStartAt = scheduledStartMilliseconds + roundStartOffset;

  return {
    round_number: scenario.number,
    question_id: questionId,
    role: isAnswerer ? "answerer" : "observer",
    selected_option: selectedOption,
    client_answer_id: createClientAnswerId(manifest.run_id, questionId, vuId),
    replay_required: replayOffset !== null,
    status_discard: isStatusDiscardTarget
      ? {
          enabled: true,
          discard_only_when_status: "Stored",
          query_delay_ms: requireStatusDelay(scenario.answer_status_delay_ms),
          retry_only_when_status: "NotStored",
        }
      : {
          enabled: false,
          discard_only_when_status: null,
          query_delay_ms: null,
          retry_only_when_status: null,
        },
    schedule: {
      round_start_offset_ms: roundStartOffset,
      round_start_at_unix_ms: roundStartAt,
      answer_send_offset_ms: answerOffset,
      answer_send_at_unix_ms: addNullable(roundStartAt, answerOffset),
      replay_send_offset_ms: replayOffset,
      replay_send_at_unix_ms: addNullable(roundStartAt, replayOffset),
      status_query_offset_ms: statusOffset,
      status_query_at_unix_ms: addNullable(roundStartAt, statusOffset),
    },
  };
}

function optionForVu(
  manifest: GeneratorPlanningManifest,
  vuId: number,
): GeneratorOptionLabel {
  const oracle = manifest.scenario.answer_oracle;
  if (vuId >= oracle.modulo_vu_start && vuId <= oracle.modulo_vu_end) {
    const index = (vuId - oracle.modulo_vu_start) % oracle.modulo_option_cycle.length;
    const option = oracle.modulo_option_cycle[index];
    if (option !== undefined) {
      return option;
    }
  }
  const fixedAnswer = oracle.fixed_virtual_answers.find(
    (answer) => answer.vu_id === vuId,
  );
  if (fixedAnswer === undefined) {
    throw new Error("generator_plan_answer_invalid");
  }
  return fixedAnswer.option;
}

function statusDiscardTarget(
  scenario: GeneratorPlanningRound,
  vuId: number,
): boolean {
  return (
    scenario.condition === "receipt-discard" &&
    scenario.receipt_discard_vu_start !== null &&
    scenario.receipt_discard_vu_end !== null &&
    scenario.receipt_discard_only_when_status === "Stored" &&
    scenario.answer_status_delay_ms !== null &&
    scenario.retry_only_when_status === "NotStored" &&
    vuId >= scenario.receipt_discard_vu_start &&
    vuId <= scenario.receipt_discard_vu_end
  );
}

function requireStatusDelay(delayMilliseconds: number | null): number {
  if (delayMilliseconds === null) {
    throw new Error("generator_plan_status_invalid");
  }
  return delayMilliseconds;
}

function sessionStartOffsetMilliseconds(
  manifest: GeneratorPlanningManifest,
  vuId: number,
): number {
  const lifecycle = manifest.scenario.lifecycle;
  let previousTarget = 0;
  let previousOffset = lifecycle.ramp_start_seconds * 1_000;
  for (let index = 0; index < lifecycle.ramp_targets.length; index += 1) {
    const target = lifecycle.ramp_targets[index];
    const offsetSeconds = lifecycle.ramp_target_offsets_seconds[index];
    if (target === undefined || offsetSeconds === undefined) {
      throw new Error("generator_plan_ramp_invalid");
    }
    if (vuId <= target) {
      const segmentPosition = vuId - previousTarget;
      const segmentSize = target - previousTarget;
      const segmentDuration = offsetSeconds * 1_000 - previousOffset;
      return previousOffset + Math.ceil((segmentPosition * segmentDuration) / segmentSize);
    }
    previousTarget = target;
    previousOffset = offsetSeconds * 1_000;
  }
  throw new Error("generator_plan_ramp_invalid");
}

function sessionStopOffsetMilliseconds(
  manifest: GeneratorPlanningManifest,
  vuId: number,
): number {
  const lifecycle = manifest.scenario.lifecycle;
  const start = lifecycle.ramp_down_start_seconds * 1_000;
  const duration =
    (lifecycle.ramp_down_end_seconds - lifecycle.ramp_down_start_seconds) * 1_000;
  const reversePosition = GENERATOR_VIRTUAL_USER_COUNT - vuId + 1;
  return start + Math.ceil((reversePosition * duration) / GENERATOR_VIRTUAL_USER_COUNT);
}

function addNullable(base: number, offset: number | null): number | null {
  return offset === null ? null : base + offset;
}
