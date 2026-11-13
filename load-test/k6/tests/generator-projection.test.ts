import { describe, expect, it } from "vitest";

import type { ParticipantState, RoundState } from "../src/generator/api-contracts.js";
import {
  chooseParticipantProjection,
  optionIdForLabel,
} from "../src/generator/projection.js";
import {
  loadK6RuntimeManifest,
} from "../src/generator/runtime-manifest.js";
import { GeneratorProtocolRefusal } from "../src/generator/session-contracts.js";
import {
  sha256Text,
  validRuntimeManifestFixture,
} from "./fixtures/runtime-manifest.js";

const runtimeFixture = validRuntimeManifestFixture();
const ROUND_ID = "33333333-3333-4333-8333-333333333333";
const OTHER_ROUND_ID = "44444444-4444-4444-8444-444444444444";

const manifest = loadK6RuntimeManifest(
  runtimeFixture.raw,
  runtimeFixture.environment,
  sha256Text,
).manifest;

function projection(
  overrides: {
    readonly projectedAt?: string;
    readonly eventVersion?: number;
    readonly roundId?: string;
    readonly roundVersion?: number;
    readonly state?: RoundState;
    readonly stateVersion?: number;
    readonly openAt?: string | null;
    readonly standardCloseAt?: string | null;
    readonly questionIndex?: number;
  } = {},
): ParticipantState {
  const question = manifest.questions[overrides.questionIndex ?? 0];
  if (question === undefined) {
    throw new Error("test_question_missing");
  }
  const roundVersion = overrides.roundVersion ?? 2;
  const state = overrides.state ?? "Open";
  return {
    projected_at: overrides.projectedAt ?? "2026-09-03T01:15:00Z",
    event: {
      event_id: manifest.event.id,
      event_title: "[LOAD-TEST] projection",
      event_state: "InProgress",
      event_version: overrides.eventVersion ?? 7,
      start_delay_seconds: 3,
    },
    participant: {
      public_label: "AAA",
      is_ranking_eligible: true,
    },
    latest_result: null,
    current_round: {
      round_id: overrides.roundId ?? ROUND_ID,
      round_number: (overrides.questionIndex ?? 0) + 1,
      version: roundVersion,
      state_version: overrides.stateVersion ?? roundVersion * 4 + statePhase(state),
      state,
      open_at: overrides.openAt === undefined ? "2026-09-03T01:14:50Z" : overrides.openAt,
      standard_close_at:
        overrides.standardCloseAt === undefined
          ? "2026-09-03T01:15:20Z"
          : overrides.standardCloseAt,
      question: {
        question_id: question.id,
        prompt: "[LOAD-TEST] question",


        options: question.options.map((option) => ({
          option_id: option.id,
          label: option.label,
          content: "[LOAD-TEST] option content",
        })),
      },
    },
  };
}

describe("participant generator projection", () => {
  it.each([
    ["Scheduled", 8],
    ["Open", 9],
    ["ReadyToReveal", 11],
    ["AwaitingValidation", 10],
  ] as const)("round version×4+phaseのstate_versionを受理する: %s", (state, stateVersion) => {
    const candidate = projection({ state, stateVersion });

    expect(chooseParticipantProjection(manifest, null, candidate)).toBe(candidate);
    expect(() =>
      chooseParticipantProjection(manifest, null, {
        ...candidate,
        current_round: candidate.current_round === null
          ? null
          : { ...candidate.current_round, state_version: stateVersion + 1 },
      }),
    ).toThrowError(new GeneratorProtocolRefusal("generator_projection_invalid"));
  });

  it("actual open/standard windowの完全性と昇順を要求する", () => {
    expect(() =>
      chooseParticipantProjection(manifest, null, projection({ standardCloseAt: null })),
    ).toThrowError(new GeneratorProtocolRefusal("generator_projection_invalid"));
    expect(() =>
      chooseParticipantProjection(
        manifest,
        null,
        projection({ standardCloseAt: "2026-09-03T01:14:50Z" }),
      ),
    ).toThrowError(new GeneratorProtocolRefusal("generator_projection_invalid"));
    expect(() =>
      chooseParticipantProjection(manifest, null, projection({
        openAt: null,
        standardCloseAt: null,
      })),
    ).not.toThrow();
  });

  it("event→round version→state version→projected_atの順で新しいprojectionを選ぶ", () => {
    const current = projection({ projectedAt: "2026-09-03T01:15:05Z" });
    const newerEvent = projection({
      eventVersion: 8,
      roundVersion: 1,
      state: "Scheduled",
      stateVersion: 4,
      projectedAt: "2026-09-03T01:15:00Z",
    });
    const newerRound = projection({ roundVersion: 3, stateVersion: 13 });
    const newerState = projection({ state: "ReadyToReveal", stateVersion: 11 });
    const staleTimestamp = projection({ projectedAt: "2026-09-03T01:15:04Z" });

    expect(chooseParticipantProjection(manifest, current, newerEvent)).toBe(newerEvent);
    expect(chooseParticipantProjection(manifest, current, newerRound)).toBe(newerRound);
    expect(chooseParticipantProjection(manifest, current, newerState)).toBe(newerState);
    expect(chooseParticipantProjection(manifest, current, staleTimestamp)).toBe(current);
  });

  it("event versionが同じ別Roundを遅延応答として採用しない", () => {
    const current = projection();
    const otherRound = projection({
      roundId: OTHER_ROUND_ID,
      roundVersion: 99,
      stateVersion: 397,
      projectedAt: "2026-09-03T01:16:00Z",
    });

    expect(chooseParticipantProjection(manifest, current, otherRound)).toBe(current);
  });

  it("manifestと一致するABCD順のoption IDだけを使う", () => {
    const candidate = projection();
    if (candidate.current_round?.question === undefined) {
      throw new Error("test_round_question_missing");
    }

    expect(optionIdForLabel(candidate.current_round, "C")).toBe(
      manifest.questions[0]?.options[2]?.id,
    );
    const wrongOrder = structuredClone(candidate);
    wrongOrder.current_round?.question?.options.reverse();
    expect(() => chooseParticipantProjection(manifest, null, wrongOrder)).toThrowError(
      new GeneratorProtocolRefusal("generator_projection_invalid"),
    );
  });

  it("event、ranking eligibility、projected_atの境界違反を拒否する", () => {
    const wrongEvent = projection();
    wrongEvent.event.event_id = "55555555-5555-4555-8555-555555555555";
    const ineligible = projection();
    ineligible.participant.is_ranking_eligible = false;
    const invalidTime = projection();
    invalidTime.projected_at = "not-a-date";

    for (const candidate of [wrongEvent, ineligible, invalidTime]) {
      expect(() => chooseParticipantProjection(manifest, null, candidate)).toThrowError(
        new GeneratorProtocolRefusal("generator_projection_invalid"),
      );
    }
  });

  it("projectionのanswerを現Roundと公開optionへ束縛する", () => {
    const wrongRound = projection();
    wrongRound.answer = {
      answer_id: "66666666-6666-4666-8666-666666666666",
      round_id: OTHER_ROUND_ID,
      option_id: manifest.questions[0]?.options[0]?.id ?? "",
      client_answer_id: "77777777-7777-5777-8777-777777777777",
      received_at: "2026-09-03T01:15:00Z",
    };

    expect(() => chooseParticipantProjection(manifest, null, wrongRound)).toThrowError(
      new GeneratorProtocolRefusal("generator_projection_invalid"),
    );
  });
});

function statePhase(state: RoundState): number {
  const phases: Readonly<Record<string, number>> = {
    Open: 1,
    ReadyToReveal: 3,
    AwaitingValidation: 2,
  };
  return phases[state] ?? 0;
}

describe("operator-published latest result", () => {
  function latestResult(): NonNullable<ParticipantState["latest_result"]> {
    const round = projection({ state: "Reveal", roundVersion: 3 }).current_round;
    if (round?.question === undefined) throw new Error("test_question_missing");
    return {
      ...round, question: round.question, selected_option_id: null,
      result: { publication: "Published", correct_option_id: round.question.options[0]!.option_id,
        distribution: { valid_answer_count: 500,
        options: round.question.options.map((option, index) => ({ ...option, is_correct: index === 0,
          valid_response_count: [126, 125, 125, 124][index]!, response_percentage: [25.2, 25, 25, 24.8][index]! })) },
        outcome: { status: "Unanswered" },
      },
    };
  }
  it("current_roundと独立した最新結果を受け取り、currentが無くても検証する", () => {
    const resultOnly: ParticipantState = { ...projection(), current_round: null, latest_result: latestResult() };
    expect(chooseParticipantProjection(manifest, null, resultOnly)).toBe(resultOnly);
    const nextQuestion: ParticipantState = { ...projection({ questionIndex: 1, roundId: OTHER_ROUND_ID }), latest_result: latestResult() };
    expect(chooseParticipantProjection(manifest, resultOnly, { ...nextQuestion, event: { ...nextQuestion.event, event_version: 8 } })).toMatchObject({ current_round: { round_id: OTHER_ROUND_ID } });
    resultOnly.latest_result!.result.distribution!.options[0]!.valid_response_count = 0;
    expect(() => chooseParticipantProjection(manifest, null, resultOnly)).toThrow("generator_projection_invalid");
  });
  it("currentとlatestの同じRoundや結果optionの差し替えを拒否する", () => {
    const duplicate = { ...projection(), latest_result: latestResult() };
    expect(() => chooseParticipantProjection(manifest, null, duplicate)).toThrow("generator_projection_invalid");
    const invalid = { ...projection(), current_round: null, latest_result: latestResult() };
    invalid.latest_result.selected_option_id = OTHER_ROUND_ID;
    expect(() => chooseParticipantProjection(manifest, null, invalid)).toThrow("generator_projection_invalid");
  });
  it("分布が非公開でも正答と本人の選択肢を検証する", () => {
    const result = latestResult();
    result.result.distribution = null;
    const hidden = { ...projection(), current_round: null, latest_result: result };
    expect(chooseParticipantProjection(manifest, null, hidden)).toBe(hidden);
    result.result.correct_option_id = OTHER_ROUND_ID;
    expect(() => chooseParticipantProjection(manifest, null, hidden)).toThrow("generator_projection_invalid");
  });
});
