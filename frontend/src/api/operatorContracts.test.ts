import { describe, expect, it } from "vitest";

import {
  advanceRoundMutationRequestSchema,
  operatorLatestRankingResponseSchema,
  operatorMutationResponseSchema,
  operatorStateSchema,
  questionSelectionMutationRequestSchema,
  resultVisibilityMutationRequestSchema,
  visibilityControlsSchema,
} from "./operatorContracts";

const ids = {
  event: "80000000-0000-4000-8000-000000000001",
  round: "80000000-0000-4000-8000-000000000002",
  question: "80000000-0000-4000-8000-000000000003",
  option: "80000000-0000-4000-8000-000000000004",
  idempotency: "80000000-0000-4000-8000-000000000005",
  option2: "80000000-0000-4000-8000-000000000006",
} as const;

const stateFixture = {
  award_ceremony_phase: null,
  event_id: ids.event,
  projected_at: "2026-08-18T03:00:00Z",
  event_title: "[DEMO] 問題切替",
  event_state: "LobbyOpen",
  event_version: 2,
  result_visibility: { distribution: true, personal_ranking: false, overall_ranking: false },
  start_delay_seconds: 3,
  visibility_controls: {
    distribution: { mode: "manual", initially_visible: true, changes: [] },
    personal_ranking: { mode: "manual", initially_visible: false, changes: [] },
    overall_ranking: { mode: "manual", initially_visible: false, changes: [] },
  },
  participant_count: 0,
  available_questions: [
    {
      question_id: ids.question,
      version: 1,
      display_order: 1,
      prompt: "[DEMO] 架空問題",
      options: [
        {
          option_id: ids.option,
          label: "A",
          content: "架空選択肢",
          is_correct: true,
        },
      ],
    },
  ],
  used_question_ids: [],
  current_round: {
    round_id: ids.round,
    version: 1,
    state_version: 4,
    state: "PreparingAssets",
    open_at: null,
    standard_close_at: null,
    can_publish_results: false,
    result_publication: "Withheld",
    result_failure_code: null,
    question: {
      question_id: ids.question,
      version: 1,
      prompt: "[DEMO] 架空問題",
      options: [
        {
          option_id: ids.option,
          label: "A",
          content: "架空選択肢",
          is_correct: true,
        },
      ],
    },
    statistics: {
      answer_count: 0,
      average_response_ms: null,
      options: [
        {
          option_id: ids.option,
          answer_count: 0,
          average_response_ms: null,
        },
      ],
    },
  },
  latest_ranking_snapshot_version: null,
} as const;

describe("operator API contracts", () => {
  it("公開可能のサーバー判定を必須booleanとして受理する", () => {
    const readyRound = { ...stateFixture.current_round, state: "ReadyToReveal", can_publish_results: true };
    expect(operatorStateSchema.parse({ ...stateFixture, current_round: readyRound }).current_round?.can_publish_results).toBe(true);
    expect(operatorStateSchema.safeParse({ ...stateFixture, current_round: { ...readyRound, can_publish_results: undefined } }).success).toBe(false);
    expect(operatorStateSchema.safeParse({ ...stateFixture, current_round: { ...readyRound, can_publish_results: "true" } }).success).toBe(false);
  });
  it("問題プレビューの全選択肢とcurrent Roundをstrictに検証する", () => {
    expect(operatorStateSchema.safeParse(stateFixture).success).toBe(true);
    expect(
      operatorStateSchema.safeParse({
        ...stateFixture,
        available_questions: [
          { ...stateFixture.available_questions[0], leaked: "secret" },
        ],
      }).success,
    ).toBe(false);
    expect(
      operatorStateSchema.safeParse({
        ...stateFixture,
        available_questions: [
          {
            ...stateFixture.available_questions[0],
            options: [
              stateFixture.available_questions[0].options[0],
              stateFixture.available_questions[0].options[0],
            ],
          },
        ],
      }).success,
    ).toBe(false);
  });

  it("回答統計の件数・平均速度を境界で検証する", () => {
    expect(
      operatorStateSchema.safeParse({
        ...stateFixture,
        current_round: {
          ...stateFixture.current_round,
          statistics: {
            ...stateFixture.current_round.statistics,
            average_response_ms: -1,
          },
        },
      }).success,
    ).toBe(false);
  });

  it("自動集計の失敗コードを安全な形式とVoidCandidateへ限定する", () => {
    const round = stateFixture.current_round;
    expect(
      operatorStateSchema.safeParse({
        ...stateFixture,
        current_round: {
          ...round,
          state: "VoidCandidate",
          result_failure_code: "automatic_aggregate_domain_failure:round_not_closed",
        },
      }).success,
    ).toBe(true);

    for (const currentRound of [
      { ...round, state: "VoidCandidate", result_failure_code: null },
      {
        ...round,
        result_failure_code: "automatic_aggregate_integrity_failure",
      },
      {
        ...round,
        state: "VoidCandidate",
        result_failure_code: "raw database error: password=secret",
      },
    ]) {
      expect(
        operatorStateSchema.safeParse({
          ...stateFixture,
          current_round: currentRound,
        }).success,
      ).toBe(false);
    }
  });

  it("問題選択肢と選択肢別統計のIDを一意な1対1として検証する", () => {
    const round = stateFixture.current_round;
    const duplicateQuestionOption = {
      ...round,
      question: {
        ...round.question,
        options: [round.question.options[0], round.question.options[0]],
      },
    };
    const duplicateStatisticsOption = {
      ...round,
      statistics: {
        ...round.statistics,
        options: [round.statistics.options[0], round.statistics.options[0]],
      },
    };
    const mismatchedStatisticsOption = {
      ...round,
      statistics: {
        ...round.statistics,
        options: [
          { ...round.statistics.options[0], option_id: ids.option2 },
        ],
      },
    };

    for (const invalidRound of [
      duplicateQuestionOption,
      duplicateStatisticsOption,
      mismatchedStatisticsOption,
    ]) {
      expect(
        operatorStateSchema.safeParse({
          ...stateFixture,
          current_round: invalidRound,
        }).success,
      ).toBe(false);
    }
  });

  it("回答数・平均速度・選択肢別合計の不整合を拒否する", () => {
    const round = stateFixture.current_round;
    const optionStatistics = round.statistics.options[0];
    const validAnsweredStatistics = {
      answer_count: 1,
      average_response_ms: 1200,
      options: [
        {
          ...optionStatistics,
          answer_count: 1,
          average_response_ms: 1200,
        },
      ],
    };
    expect(
      operatorStateSchema.safeParse({
        ...stateFixture,
        current_round: {
          ...round,
          statistics: validAnsweredStatistics,
        },
      }).success,
    ).toBe(true);

    const invalidStatistics = [
      {
        ...round.statistics,
        options: [{ ...optionStatistics, average_response_ms: 100 }],
      },
      {
        ...validAnsweredStatistics,
        options: [
          { ...optionStatistics, answer_count: 1, average_response_ms: null },
        ],
      },
      {
        answer_count: 1,
        average_response_ms: 100,
        options: [optionStatistics],
      },
      { ...round.statistics, average_response_ms: 100 },
      { ...validAnsweredStatistics, average_response_ms: null },
    ];
    for (const statistics of invalidStatistics) {
      expect(
        operatorStateSchema.safeParse({
          ...stateFixture,
          current_round: { ...round, statistics },
        }).success,
      ).toBe(false);
    }
  });

  it("ranking詳細responseはnullまたはstrictなsnapshotだけを許可する", () => {
    expect(operatorLatestRankingResponseSchema.safeParse(null).success).toBe(
      true,
    );
    expect(
      operatorLatestRankingResponseSchema.safeParse({
        snapshot_version: 1,
        round_id: ids.round,
        rows: [],
        leaked: "secret",
      }).success,
    ).toBe(false);
  });

  it("問題選択mutationへUUID・versionを要求し、理由を送らない", () => {
    const request = {
      expected_version: 1,
      idempotency_key: ids.idempotency,
      question_id: ids.question,
    } as const;

    expect(questionSelectionMutationRequestSchema.safeParse(request).success).toBe(true);
    expect(
      questionSelectionMutationRequestSchema.safeParse({
        ...request,
        question_id: "not-a-uuid",
      }).success,
    ).toBe(false);
  });

  it("使用済み問題IDと新Round作成requestをUUIDとして検証する", () => {
    const request = {
      expected_version: 1,
      idempotency_key: ids.idempotency,
      question_id: ids.question,
    } as const;

    expect(
      operatorStateSchema.safeParse({
        ...stateFixture,
        used_question_ids: [ids.question],
      }).success,
    ).toBe(true);
    expect(
      operatorStateSchema.safeParse({
        ...stateFixture,
        used_question_ids: ["not-a-uuid"],
      }).success,
    ).toBe(false);
    expect(advanceRoundMutationRequestSchema.safeParse(request).success).toBe(
      true,
    );
  });

  it.each(["advance_round", "complete_event"] as const)(
    "新しい運営mutation operation %s を受理する",
    (operation) => {
      expect(
        operatorMutationResponseSchema.safeParse({
          operation,
          event_id: ids.event,
          event_version: 3,
          round_id: ids.round,
          round_version: 2,
          round_state: "RoundBackupValidated",
          result_visibility: { distribution: true, personal_ranking: false, overall_ranking: false },
          open_at: null,
          standard_close_at: null,
          valid_answer_count: null,
          ranking_row_count: null,
          is_replay: false,
        }).success,
      ).toBe(true);
    },
  );
});

describe("結果表示設定の契約", () => {
  it("各項目を独立し、自動切替の非表示から再表示まで受理する", () => {
    const controls = {
      ...stateFixture.visibility_controls,
      personal_ranking: {
        mode: "automatic",
        initially_visible: true,
        changes: [
          { after_round: 3, visible: false },
          { after_round: 5, visible: true },
        ],
      },
    };
    expect(visibilityControlsSchema.parse(controls)).toEqual(controls);
  });

  it.each([
    { mode: "manual", changes: [{ after_round: 1, visible: true }] },
    { mode: "automatic", changes: [] },
    { mode: "automatic", changes: [{ after_round: 1, visible: true }, { after_round: 1, visible: false }] },
    { mode: "automatic", changes: [{ after_round: 3, visible: true }, { after_round: 2, visible: false }] },
    { mode: "automatic", changes: [{ after_round: 0, visible: true }] },
  ])("切替modeと変更予定の不整合を拒否する: %j", (invalidControl) => {
    expect(visibilityControlsSchema.safeParse({
      ...stateFixture.visibility_controls,
      distribution: { initially_visible: true, ...invalidControl },
    }).success).toBe(false);
  });

  it.each(["personal_ranking", "rankings"])("手動変更%sはbooleanを要求し、旧公開範囲を拒否する", (target) => {
    const request = {
      expected_version: 1,
      idempotency_key: ids.idempotency,
      target,
      visible: true,
    };
    expect(resultVisibilityMutationRequestSchema.parse(request)).toEqual(request);
    expect(resultVisibilityMutationRequestSchema.safeParse({ ...request, visible: "true" }).success).toBe(false);
    expect(resultVisibilityMutationRequestSchema.safeParse({ ...request, visibility: "Public" }).success).toBe(false);
    expect(resultVisibilityMutationRequestSchema.safeParse({ ...request, target: "unknown" }).success).toBe(false);
    expect(operatorStateSchema.safeParse({ ...stateFixture, ranking_visibility: "Public" }).success).toBe(false);
    expect(operatorStateSchema.safeParse({
      ...stateFixture, result_visibility: { ...stateFixture.result_visibility, rankings: true },
    }).success).toBe(false);
  });
});
