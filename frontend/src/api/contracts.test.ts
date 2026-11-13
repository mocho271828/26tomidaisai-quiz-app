import { describe, expect, it } from "vitest";

import {
  answerStatusSchema,
  eventSchema,
  participantCurrentRoundSchema,
  participantJoinRequestSchema,
  participantLatestResultSchema,
  participantPublicLabelSchema,
  participantRankingSchema,
  participantResultSchema,
  participantStateSchema,
  personalResultResponseSchema,
  publishedResultSchema,
  questionSchema,
  submitAnswerRequestSchema,
} from "./contracts";

const identifiers = {
  event: "00000000-0000-4000-8000-000000000001",
  round: "00000000-0000-4000-8000-000000000002",
  nextRound: "00000000-0000-4000-8000-000000000008",
  question: "00000000-0000-4000-8000-000000000003",
  optionA: "00000000-0000-4000-8000-000000000004",
  optionB: "00000000-0000-4000-8000-000000000005",
  answer: "00000000-0000-4000-8000-000000000006",
  clientAnswer: "00000000-0000-4000-8000-000000000007",
} as const;

const eventFixture = {
  timing_basis: "client_monotonic_v1", arrival_grace_ms: 2000, clock_tolerance_ms: 500, client_ahead_tolerance_ms: 2500,
  event_id: identifiers.event,
  event_title: "[DEMO] 2026 富大祭クイズ",
  event_state: "InProgress",
  event_version: 3,
  start_delay_seconds: 3,
} as const;

const questionFixture = {
  question_id: identifiers.question,
  prompt: "[DEMO] 2 + 2 は？",
  options: [
    { option_id: identifiers.optionA, label: "A", content: "3" },
    { option_id: identifiers.optionB, label: "B", content: "4" },
  ],
} as const;

describe("audience API contracts", () => {
  it("時間差の下限側許容値を上限側と独立した必須整数で受け取る", () => {
    expect(eventSchema.parse(eventFixture)).toMatchObject({
      client_ahead_tolerance_ms: 2500, clock_tolerance_ms: 500, arrival_grace_ms: 2000,
    });
    for (const client_ahead_tolerance_ms of [undefined, null, true, "2500", -1, 0.5]) {
      expect(eventSchema.safeParse({ ...eventFixture, client_ahead_tolerance_ms }).success).toBe(false);
    }
  });

  it.each([
    ["参加者の進行中Round", participantCurrentRoundSchema, {}],
    [
      "参加者の直近結果",
      participantLatestResultSchema,
      {
        question: questionFixture,
        selected_option_id: null,
        result: {
          publication: "Published",
          correct_option_id: identifiers.optionB,
          distribution: null,
          outcome: { status: "Unanswered" },
        },
      },
    ],

  ] as const)("%sはversionと別に正整数のround_numberを必須とする", (_name, schema, fields) => {
    const fixture = {
      round_id: identifiers.round,
      version: 74,
      state_version: 120,
      state: "Reveal",
      open_at: "2026-08-08T01:00:00Z",
      standard_close_at: "2026-08-08T01:00:10Z",
      ...fields,
    };

    expect(schema.parse({ ...fixture, round_number: 4 })).toMatchObject({
      version: 74,
      round_number: 4,
    });
    expect(schema.safeParse(fixture).success).toBe(false);
    for (const roundNumber of [0, -1, 1.5, "4", null]) {
      expect(
        schema.safeParse({ ...fixture, round_number: roundNumber }).success,
      ).toBe(false);
    }
  });

  it("分布非公開の結果を受理し、旧位置の件数と選択肢を拒否する", () => {
    const result = {
      publication: "Published",
      correct_option_id: identifiers.optionB,
      distribution: null,
    };
    expect(publishedResultSchema.parse(result)).toEqual(result);
    expect(publishedResultSchema.safeParse({
      ...result,
      valid_answer_count: 5,
      options: [],
    }).success).toBe(false);
  });

  it("参加者向けの未公開状態を受理する", () => {
    const fixture = {
    award_ceremony: null,
      result_visibility: { distribution: true, personal_ranking: true, overall_ranking: false },
      latest_ranking_snapshot_version: null,
      personal_result_delivery: { status: "none", revision: null },
      projected_at: "2026-08-08T01:00:01Z",
      event: eventFixture,
      participant: {
        public_label: "ABC",
        is_ranking_eligible: true,
      },
      current_round: {
        round_id: identifiers.round,
        round_number: 2,
        version: 2,
        state_version: 9,
        state: "Open",
        open_at: "2026-08-08T10:00:00+09:00",
        standard_close_at: "2026-08-08T10:00:10+09:00",
        question: questionFixture,
      },
      latest_result: null,
      answer: {
        client_elapsed_ms: 2000, timing_basis: "client_monotonic_v1",
        answer_id: identifiers.answer,
        round_id: identifiers.round,
        option_id: identifiers.optionB,
        client_answer_id: identifiers.clientAnswer,
        received_at: "2026-08-08T01:00:05.125Z",
      },
    } as const;

    expect(participantStateSchema.safeParse(fixture).success).toBe(true);
  });

  it("テキスト問題だけを受理し廃止した画像fieldを拒否する", () => {
    expect(questionSchema.parse(questionFixture)).toEqual(questionFixture);
    for (const retiredFields of [
      { image_url: null },
      { image_alt: null },
      { image_url: "https://assets.example.org/old.png", image_alt: "旧問題画像" },
    ]) {
      expect(
        questionSchema.safeParse({ ...questionFixture, ...retiredFields }).success,
      ).toBe(false);
    }
  });

  it("参加者向けの公開結果を本人得点または未回答で判別する", () => {
    const publishedResult = {
      publication: "Published",
      correct_option_id: identifiers.optionB,
      distribution: {
        valid_answer_count: 1,
        options: [
          {
            ...questionFixture.options[0],
            is_correct: false,
            valid_response_count: 0,
            response_percentage: 0,
          },
          {
            ...questionFixture.options[1],
            is_correct: true,
            valid_response_count: 1,
            response_percentage: 100,
          },
        ],
      },
    } as const;
    const scored = {
      ...publishedResult,
      outcome: {
        timing_basis: "server_received_v1",
        status: "Scored",
        is_correct: true,
        total_points: 950,
        base_points: 900, speed_points: 50, elapsed_ms: 5000,
        scoring_policy_version: "scoring-demo-v1",
        speed_cohort: "Correct",
        speed_rank: 1,
        speed_cohort_size: 4,
      },
    } as const;
    const unanswered = {
      ...publishedResult,
      outcome: { status: "Unanswered" },
    } as const;

    expect(participantResultSchema.safeParse(scored).success).toBe(true);
    expect(participantResultSchema.safeParse(unanswered).success).toBe(true);
    expect(
      participantResultSchema.safeParse({
        ...scored,
        outcome: { ...scored.outcome, total_points: "950" },
      }).success,
    ).toBe(false);
    expect(
      participantResultSchema.safeParse({
        ...unanswered,
        outcome: { status: "Unanswered", total_points: 0 },
      }).success,
    ).toBe(false);
  });

  it("参加者向け未公開結果には本人outcomeを受理しない", () => {
    expect(
      participantResultSchema.safeParse({
        publication: "Withheld",
        outcome: { status: "Unanswered" },
      }).success,
    ).toBe(false);
  });

  it("bootstrapと本人結果を分離し、次問準備と直前結果を独立に検証する", () => {
    const fixture = {
    award_ceremony: null,
      result_visibility: { distribution: true, personal_ranking: true, overall_ranking: false },
      latest_ranking_snapshot_version: null,
      personal_result_delivery: { status: "none", revision: null },
      projected_at: "2026-08-08T01:01:00Z",
      event: eventFixture,
      participant: {
        public_label: "ABC",
        is_ranking_eligible: true,
      },
      current_round: {
        round_id: identifiers.nextRound,
        round_number: 5,
        version: 5,
        state_version: 1,
        state: "PreparingAssets",
        open_at: null,
        standard_close_at: null,
      },
      latest_result: {
        round_id: identifiers.round,
        round_number: 4,
        version: 4,
        state_version: 20,
        state: "Reveal",
        open_at: "2026-08-08T01:00:00Z",
        standard_close_at: "2026-08-08T01:00:10Z",
        question: questionFixture,
        selected_option_id: identifiers.optionB,
        result: {
          publication: "Published",
          correct_option_id: identifiers.optionB,
          distribution: {
            valid_answer_count: 1,
            options: [
              {
                ...questionFixture.options[0],
                is_correct: false,
                valid_response_count: 0,
                response_percentage: 0,
              },
              {
                ...questionFixture.options[1],
                is_correct: true,
                valid_response_count: 1,
                response_percentage: 100,
              },
            ],
          },
          outcome: {
            timing_basis: "server_received_v1",
            status: "Scored",
            is_correct: true,
            total_points: 950,
            base_points: 900, speed_points: 50, elapsed_ms: 5000,
            scoring_policy_version: "scoring-demo-v1",
            speed_cohort: "Correct",
            speed_rank: 1,
            speed_cohort_size: 1,
          },
        },
      },
      ranking: {
        snapshot_version: 1,
        overall_rank: 1,
        total_points: 950,
        correct_count: 1,
        top_rows: null,
      },
    } as const;

    const { ranking, latest_result, ...core } = fixture;
    const { selected_option_id, result, ...timeline } = latest_result;
    const { outcome, ...published } = result;
    const revision = {
      event_id: fixture.event.event_id, event_version: fixture.event.event_version,
      result_round_id: latest_result.round_id, result_round_version: latest_result.version,
      ranking_snapshot_version: ranking.snapshot_version, result_visibility: fixture.result_visibility,
    };
    expect(participantStateSchema.safeParse({ ...core,
      latest_result: { ...timeline, result: published }, latest_ranking_snapshot_version: 1,
      personal_result_delivery: { status: "pending", revision },
    }).success).toBe(true);
    expect(participantStateSchema.safeParse(fixture).success).toBe(false);
    expect(personalResultResponseSchema.safeParse({
      delivery: { status: "ready", revision }, result: { selected_option_id, outcome, ranking },
    }).success).toBe(true);
  });

  it("未公開結果へ正解・集計値が混入した場合は拒否する", () => {
    const leakedFixture = {
      publication: "Withheld",
      correct_option_id: identifiers.optionB,
      distribution: {
        valid_answer_count: 4,
        options: [],
      },
    } as const;

    expect(participantResultSchema.safeParse(leakedFixture).success).toBe(false);
  });

  it("共通の公開結果には参加者本人outcomeを受理しない", () => {
    const leakedFixture = {
      publication: "Published",
      correct_option_id: identifiers.optionB,
      distribution: {
        valid_answer_count: 0,
        options: [],
      },
      outcome: { status: "Unanswered" },
    } as const;

    expect(publishedResultSchema.safeParse(leakedFixture).success).toBe(false);
  });

  it("運営用の次問プレビューが参加者契約へ混入した場合は拒否する", () => {
    const availableQuestions = [
      {
        question_id: "00000000-0000-4000-8000-000000000090",
        version: 2,
        display_order: 2,
        prompt: "NEXT-QUESTION-ONLY-FOR-OPERATOR",
        options: [
          {
            option_id: "00000000-0000-4000-8000-000000000091",
            label: "A",
            content: "NEXT-OPTION-ONLY-FOR-OPERATOR",
            is_correct: true,
          },
        ],
      },
    ];
    const participantLeak = {
    award_ceremony: null,
      result_visibility: { distribution: true, personal_ranking: true, overall_ranking: false },
      latest_ranking_snapshot_version: null,
      personal_result_delivery: { status: "none", revision: null },
      projected_at: "2026-08-08T01:00:01Z",
      event: eventFixture,
      participant: {
        public_label: "ABC",
        is_ranking_eligible: true,
      },
      current_round: null,
      latest_result: null,
      available_questions: availableQuestions,
    };
    expect(participantStateSchema.safeParse(participantLeak).success).toBe(
      false,
    );
  });

  it("回答状態を保存済みと未保存で判別する", () => {
    const accepted = {
      status: "AcceptedLocked",
      answer: {
        client_elapsed_ms: 2000, timing_basis: "client_monotonic_v1",
        answer_id: identifiers.answer,
        round_id: identifiers.round,
        option_id: identifiers.optionB,
        client_answer_id: identifiers.clientAnswer,
        received_at: "2026-08-08T01:00:05.125Z",
      },
    } as const;

    expect(answerStatusSchema.safeParse(accepted).success).toBe(true);
    expect(
      answerStatusSchema.safeParse({
        status: "NotStored",
        client_answer_id: identifiers.clientAnswer,
      }).success,
    ).toBe(true);
  });

  it("回答要求へparticipant identityが混入した場合は拒否する", () => {
    const unsafeRequest = {
      client_elapsed_ms: 2000,
      round_version: 2,
      option_id: identifiers.optionB,
      client_answer_id: identifiers.clientAnswer,
      participant_id: "00000000-0000-4000-8000-000000000099",
    };

    expect(submitAnswerRequestSchema.safeParse(unsafeRequest).success).toBe(
      false,
    );
  });

  it("参加要求には256bitの短時間再試行secretを必須とする", () => {
    expect(
      participantJoinRequestSchema.safeParse({
        client_join_token: "A".repeat(43),
      }).success,
    ).toBe(true);
    expect(participantJoinRequestSchema.safeParse({}).success).toBe(false);
    expect(
      participantJoinRequestSchema.safeParse({
        client_join_token: "too-short",
      }).success,
    ).toBe(false);
  });

  it("参加要求で利用者による参加者IDの指定を拒否する", () => {
    expect(
      participantJoinRequestSchema.safeParse({
        client_join_token: "A".repeat(43),
        public_label: "ABC",
      }).success,
    ).toBe(false);
  });

  it("参加者IDはA〜Zの半角英字3文字だけを受理する", () => {
    expect(participantPublicLabelSchema.safeParse("ABC").success).toBe(true);
    for (const invalid of ["abc", "AB", "ABCD", "A1C", "ABC\n", "ＡＢＣ", " AB"] as const) {
      expect(participantPublicLabelSchema.safeParse(invalid).success).toBe(false);
    }
  });

  it("参加者順位は許可したTOP3以外の一覧や本人labelを拒否する", () => {
    const unsafeRanking = {
      snapshot_version: 1,
      overall_rank: 1,
      total_points: 100,
      correct_count: 1,
      top_rows: null,
      public_label: "OTHER-PARTICIPANT",
      rows: [],
    };

    expect(participantRankingSchema.safeParse(unsafeRanking).success).toBe(
      false,
    );
  });

  it("TOP3はnullまたは同順位を含む3位以内の一覧を必須とする", () => {
    const ranking = {
      snapshot_version: 2,
      overall_rank: 5,
      total_points: 600,
      correct_count: 2,
    };
    expect(participantRankingSchema.safeParse(ranking).success).toBe(false);
    for (const topRows of [null, [], [
      { public_label: "AAA", overall_rank: 1, total_points: 900, correct_count: 2 },
      { public_label: "BBB", overall_rank: 2, total_points: 800, correct_count: 2 },
      { public_label: "CCC", overall_rank: 3, total_points: 700, correct_count: 2 },
      { public_label: "EEE", overall_rank: 3, total_points: 700, correct_count: 2 },
    ]]) {
      expect(participantRankingSchema.safeParse({ ...ranking, top_rows: topRows }).success)
        .toBe(true);
    }
    expect(participantRankingSchema.safeParse({
      ...ranking,
      top_rows: [{ public_label: "DDD", overall_rank: 4, total_points: 600, correct_count: 2 }],
    }).success).toBe(false);
  });
});
