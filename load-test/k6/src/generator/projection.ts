import type { ParticipantRound, ParticipantState } from "./api-contracts.js";
import type { K6RuntimeManifest } from "./runtime-manifest.js";
import { GeneratorProtocolRefusal } from "./session-contracts.js";

const CLOCK_PHASE: Readonly<Record<string, number>> = {
  Scheduled: 0,
  Open: 1,
  ReadyToReveal: 3,
  AwaitingValidation: 2,
};

/** backendの`round.version * 4 + phase`規則をreadiness応答でも共有する。 */
export function expectedRoundStateVersion(round: {
  readonly version: number;
  readonly state: string;
}): number {
  return round.version * 4 + (CLOCK_PHASE[round.state] ?? 0);
}

/** UIと同じversion順でprojectionを採用し、意味上不可能なtimelineは拒否する。 */
export function chooseParticipantProjection(
  manifest: K6RuntimeManifest,
  current: ParticipantState | null,
  candidate: ParticipantState,
): ParticipantState {
  validateProjection(manifest, candidate);
  if (current === null) {
    return candidate;
  }
  if (candidate.event.event_id !== current.event.event_id) {
    throw new GeneratorProtocolRefusal("generator_projection_invalid");
  }
  if (candidate.event.event_version !== current.event.event_version) {
    return candidate.event.event_version > current.event.event_version ? candidate : current;
  }
  if (candidate.current_round?.round_id !== current.current_round?.round_id) {
    return current;
  }
  return compareOrder(projectionOrder(candidate), projectionOrder(current)) >= 0
    ? candidate
    : current;
}

export function optionIdForLabel(
  round: ParticipantRound,
  label: "A" | "B" | "C" | "D",
): string {
  const matches = round.question?.options.filter((option) => option.label === label) ?? [];
  if (matches.length !== 1 || matches[0] === undefined) {
    throw new GeneratorProtocolRefusal("generator_projection_invalid");
  }
  return matches[0].option_id;
}

function validateProjection(
  manifest: K6RuntimeManifest,
  candidate: ParticipantState,
): void {
  if (
    candidate.event.event_id !== manifest.event.id ||
    !candidate.participant.is_ranking_eligible ||
    !Number.isFinite(Date.parse(candidate.projected_at))
  ) {
    throw new GeneratorProtocolRefusal("generator_projection_invalid");
  }
  if (candidate.latest_result !== null) {
    validateRound(manifest, candidate.latest_result);
    validatePublishedResult(candidate.latest_result);
    if (candidate.latest_result.round_id === candidate.current_round?.round_id) {
      throw new GeneratorProtocolRefusal("generator_projection_invalid");
    }
  }
  if (candidate.current_round === null) {
    if (candidate.answer !== undefined) {
      throw new GeneratorProtocolRefusal("generator_projection_invalid");
    }
    return;
  }
  validateRound(manifest, candidate.current_round);
  if (
    candidate.answer !== undefined &&
    (candidate.answer.round_id !== candidate.current_round.round_id ||
      candidate.current_round.question === undefined ||
      !candidate.current_round.question.options.some(
        (option) => option.option_id === candidate.answer?.option_id,
      ))
  ) {
    throw new GeneratorProtocolRefusal("generator_projection_invalid");
  }
}

function validateRound(manifest: K6RuntimeManifest, round: ParticipantRound): void {
  const expectedStateVersion = expectedRoundStateVersion(round);
  if (round.state_version !== expectedStateVersion) {
    throw new GeneratorProtocolRefusal("generator_projection_invalid");
  }

  const timeline = [round.open_at, round.standard_close_at];
  const hasAnyTimeline = timeline.some((value) => value !== null);
  if (hasAnyTimeline) {
    const parsedTimeline = timeline.map((value) =>
      value === null ? Number.NaN : Date.parse(value),
    );
    const openAt = parsedTimeline[0];
    const standardCloseAt = parsedTimeline[1];
    if (
      openAt === undefined ||
      standardCloseAt === undefined ||
      !Number.isFinite(openAt) ||
      !Number.isFinite(standardCloseAt) ||
      !(openAt < standardCloseAt)
    ) {
      throw new GeneratorProtocolRefusal("generator_projection_invalid");
    }
  }

  const question = round.question;
  if (question === undefined) {
    return;
  }
  const manifestQuestion = manifest.questions.find(
    (candidate) => candidate.id === question.question_id,
  );
  if (
    manifestQuestion === undefined ||
    question.options.length !== 4
  ) {
    throw new GeneratorProtocolRefusal("generator_projection_invalid");
  }
  const labels = question.options.map((option) => option.label).join("");
  const ids = new Set(question.options.map((option) => option.option_id));
  const expectedIds = manifestQuestion.options.map((option) => option.id);
  if (
    labels !== "ABCD" ||
    ids.size !== 4 ||
    question.options.some((option, index) => option.option_id !== expectedIds[index])
  ) {
    throw new GeneratorProtocolRefusal("generator_projection_invalid");
  }
}

function validatePublishedResult(round: NonNullable<ParticipantState["latest_result"]>): void {
  const { question, result, selected_option_id: selected } = round;
  const optionIds = new Set(question.options.map((option) => option.option_id));
  if (!optionIds.has(result.correct_option_id) ||
      (selected !== null && !optionIds.has(selected))) {
    throw new GeneratorProtocolRefusal("generator_projection_invalid");
  }
  const distribution = result.distribution;
  if (distribution === null) return;
  if (distribution.options.length !== question.options.length ||
      distribution.options.some((option, index) => {
        const questionOption = question.options[index];
        return option.option_id !== questionOption?.option_id || option.label !== questionOption.label ||
          option.is_correct !== (option.option_id === result.correct_option_id);
      }) ||
      distribution.options.reduce((sum, option) => sum + option.valid_response_count, 0) !== distribution.valid_answer_count) {
    throw new GeneratorProtocolRefusal("generator_projection_invalid");
  }
}

type ProjectionOrder = readonly [number, number, number, number, number, number];

function projectionOrder(value: ParticipantState): ProjectionOrder {
  return [
    value.event.event_version,
    value.current_round?.version ?? 0,
    value.current_round?.state_version ?? 0,
    value.latest_result?.version ?? 0,
    value.latest_result?.state_version ?? 0,
    Date.parse(value.projected_at),
  ];
}

function compareOrder(left: ProjectionOrder, right: ProjectionOrder): number {
  for (let index = 0; index < left.length; index += 1) {
    const leftPart = left[index];
    const rightPart = right[index];
    if (leftPart !== undefined && rightPart !== undefined && leftPart !== rightPart) {
      return leftPart - rightPart;
    }
  }
  return 0;
}
