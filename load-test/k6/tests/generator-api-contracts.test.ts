import { describe, expect, it } from "vitest";

import {
  answerReceiptSchema,
  answerStatusSchema,
  decodeApiJson,
  GeneratorPayloadRefusalError,
  participantJoinRequestSchema,
  participantStateSchema,
  readinessAcknowledgementSchema,
  realtimeServerMessageSchema,
  timeResponseSchema,
} from "../src/generator/api-contracts.js";

const EVENT_ID = "11111111-1111-4111-8111-111111111111";
const ROUND_ID = "22222222-2222-4222-8222-222222222222";
const QUESTION_ID = "33333333-3333-4333-8333-333333333333";
const OPTION_ID = "44444444-4444-4444-8444-444444444444";
const ANSWER_ID = "55555555-5555-4555-8555-555555555555";
const CLIENT_ANSWER_ID = "66666666-6666-4666-8666-666666666666";
const NOW = "2026-09-03T14:00:00+09:00";

function participantProjection(): Record<string, unknown> {
  return {
    projected_at: NOW,
    event: {
      event_id: EVENT_ID,
      event_title: "負荷耐久試験",
      event_state: "InProgress",
      event_version: 8,
      start_delay_seconds: 3,
    },
    participant: {
      public_label: "AAA",
      is_ranking_eligible: true,
    },
    latest_result: null,
    current_round: {
      round_id: ROUND_ID,
      round_number: 2,
      version: 2,
      state_version: 4,
      state: "Open",
      open_at: NOW,
      standard_close_at: "2026-09-03T14:00:30+09:00",
      question: {
        question_id: QUESTION_ID,
        prompt: "[LOAD-TEST] question",


        options: [
          { option_id: OPTION_ID, label: "A", content: "[LOAD-TEST] option A" },
        ],
      },
    },
  };
}

function participantAnswer(): Readonly<Record<string, unknown>> {
  return {
    answer_id: ANSWER_ID,
    round_id: ROUND_ID,
    option_id: OPTION_ID,
    client_answer_id: CLIENT_ANSWER_ID,
    received_at: NOW,
  };
}

describe("generator API contracts", () => {
  it("join requestとparticipant projectionを大文字3文字の公開IDへ限定する", () => {
    expect(
      participantJoinRequestSchema.safeParse({
        client_join_token: "j".repeat(43),
        public_label: "ABC",
      }).success,
    ).toBe(true);

    for (const publicLabel of ["AB", "ABCD", "AbC", "１２３", "ABC\n"]) {
      expect(
        participantJoinRequestSchema.safeParse({
          client_join_token: "j".repeat(43),
          public_label: publicLabel,
        }).success,
      ).toBe(false);
    }

    const invalidProjection = participantProjection();
    (invalidProjection["participant"] as Record<string, unknown>)["public_label"] =
      "匿名-0001";
    expect(participantStateSchema.safeParse(invalidProjection).success).toBe(false);
  });

  it("participant projectionの公開契約を受理する", () => {
    expect(participantStateSchema.safeParse(participantProjection()).success).toBe(true);
  });

  it("参加者順位のTOP3はnullまたは同順位を含む3位以内の一覧を必須とする", () => {
    const projection = participantProjection();
    const ranking = { snapshot_version: 2, overall_rank: 5, total_points: 600, correct_count: 2 };
    projection["ranking"] = ranking;
    expect(participantStateSchema.safeParse(projection).success).toBe(false);
    for (const topRows of [null, [], [
      { public_label: "AAA", overall_rank: 1, total_points: 900, correct_count: 2 },
      { public_label: "BBB", overall_rank: 2, total_points: 800, correct_count: 2 },
      { public_label: "CCC", overall_rank: 3, total_points: 700, correct_count: 2 },
      { public_label: "EEE", overall_rank: 3, total_points: 700, correct_count: 2 },
    ]]) {
      projection["ranking"] = { ...ranking, top_rows: topRows };
      expect(participantStateSchema.safeParse(projection).success).toBe(true);
    }
    projection["ranking"] = {
      ...ranking,
      top_rows: [{ public_label: "DDD", overall_rank: 4, total_points: 600, correct_count: 2 }],
    };
    expect(participantStateSchema.safeParse(projection).success).toBe(false);
  });

  it("4問目の表示番号をversion 74と独立して受理する", () => {
    const projection = participantProjection();
    const round = projection["current_round"] as Record<string, unknown>;
    round["round_number"] = 4;
    round["version"] = 74;
    round["state_version"] = 297;

    expect(participantStateSchema.parse(projection).current_round).toMatchObject({
      round_number: 4,
      version: 74,
      state_version: 297,
    });
  });

  it.each([undefined, 0, -1, 0.5, "4", true])(
    "current_roundとlatest_resultの番号に正整数を必須とする: %s",
    (roundNumber) => {
      const projection = participantProjection();
      const round = projection["current_round"] as Record<string, unknown>;
      if (roundNumber === undefined) {
        delete round["round_number"];
      } else {
        round["round_number"] = roundNumber;
      }
      expect(participantStateSchema.safeParse(projection).success).toBe(false);

      projection["current_round"] = null;
      projection["latest_result"] = {
        ...round,
        selected_option_id: null,
        result: {
          publication: "Published",
          correct_option_id: OPTION_ID,
          distribution: null,
          outcome: { status: "Unanswered" },
        },
      };
      expect(participantStateSchema.safeParse(projection).success).toBe(false);
    },
  );

  it("待機時間を必須の正整数として受理する", () => {
    for (const delay of [undefined, 0, -1, 0.5, "3", true]) {
      const projection = participantProjection();
      (projection["event"] as Record<string, unknown>)["start_delay_seconds"] = delay;
      expect(participantStateSchema.safeParse(projection).success).toBe(false);
    }
  });

  it("非公開分布はnullとし、旧集計fieldを結果へ混在させない", () => {
    const projection = participantProjection();
    const round = projection["current_round"] as Record<string, unknown>;
    const result = {
      publication: "Published",
      correct_option_id: OPTION_ID,
      distribution: null,
      outcome: { status: "Unanswered" },
    };
    projection["current_round"] = null;
    projection["latest_result"] = { ...round, selected_option_id: null, result };
    expect(participantStateSchema.safeParse(projection).success).toBe(true);
    for (const legacy of [{ valid_answer_count: 1 }, { options: [] }]) {
      projection["latest_result"] = { ...round, selected_option_id: null, result: { ...result, ...legacy } };
      expect(participantStateSchema.safeParse(projection).success).toBe(false);
    }
  });

  it("projectionの余分fieldを階層ごと拒否する", () => {
    const rootExtra = { ...participantProjection(), session_token: "secret" };
    const projection = participantProjection();
    const nestedExtra = {
      ...projection,
      event: {
        ...(projection["event"] as Record<string, unknown>),
        cookie: "secret",
      },
    };

    expect(participantStateSchema.safeParse(rootExtra).success).toBe(false);
    expect(participantStateSchema.safeParse(nestedExtra).success).toBe(false);
  });

  it("projectionの不正UUIDと不正日時を拒否する", () => {
    const projection = participantProjection();
    const invalidUuid = {
      ...projection,
      event: {
        ...(projection["event"] as Record<string, unknown>),
        event_id: "not-a-uuid",
      },
    };
    const invalidDatetime = {
      ...participantProjection(),
      projected_at: "2026-09-03 14:00:00",
    };

    expect(participantStateSchema.safeParse(invalidUuid).success).toBe(false);
    expect(participantStateSchema.safeParse(invalidDatetime).success).toBe(false);
  });

  it("旧round fieldと画像fieldを拒否する", () => {
    const legacy = participantProjection();
    legacy["round"] = legacy["current_round"];
    delete legacy["current_round"];
    expect(participantStateSchema.safeParse(legacy).success).toBe(false);
    const image = participantProjection();
    const question = (image["current_round"] as Record<string, unknown>)["question"] as Record<string, unknown>;
    question["image_url"] = null;
    expect(participantStateSchema.safeParse(image).success).toBe(false);
  });

  it("readiness acknowledgementをstrictに受理する", () => {
    const acknowledgement = {
      round_id: ROUND_ID,
      round_version: 2,
      round_state: "ReadyToSchedule",
      state_version: 3,
    };

    expect(readinessAcknowledgementSchema.safeParse(acknowledgement).success).toBe(true);
    expect(
      readinessAcknowledgementSchema.safeParse({ ...acknowledgement, token: "secret" })
        .success,
    ).toBe(false);
  });

  it("answer receiptとAcceptedLocked/NotStored statusを受理する", () => {
    const answer = participantAnswer();

    expect(answerReceiptSchema.safeParse({ ...answer, is_replay: false }).success).toBe(true);
    expect(
      answerStatusSchema.safeParse({ status: "AcceptedLocked", answer }).success,
    ).toBe(true);
    expect(
      answerStatusSchema.safeParse({
        status: "NotStored",
        client_answer_id: CLIENT_ANSWER_ID,
      }).success,
    ).toBe(true);
  });

  it("answer receipt/statusの余分fieldと不正UUIDを拒否する", () => {
    expect(
      answerReceiptSchema.safeParse({
        ...participantAnswer(),
        is_replay: false,
        response_body: "secret",
      }).success,
    ).toBe(false);
    expect(
      answerStatusSchema.safeParse({
        status: "NotStored",
        client_answer_id: "invalid-id",
      }).success,
    ).toBe(false);
  });

  it("time responseをstrict datetimeとして受理する", () => {
    expect(timeResponseSchema.safeParse({ server_time: NOW }).success).toBe(true);
    expect(
      timeResponseSchema.safeParse({ server_time: NOW, server_token: "secret" }).success,
    ).toBe(false);
    expect(timeResponseSchema.safeParse({ server_time: "yesterday" }).success).toBe(false);
  });

  it("WS invalidationとpolling fallbackだけを受理する", () => {
    expect(
      realtimeServerMessageSchema.safeParse({
        type: "state.invalidated",
        audience: "participant",
        event_id: EVENT_ID,
      }).success,
    ).toBe(true);
    expect(
      realtimeServerMessageSchema.safeParse({
        type: "state.invalidated",
        audience: "public",
        event_id: EVENT_ID,
      }).success,
    ).toBe(false);
    expect(
      realtimeServerMessageSchema.safeParse({
        type: "polling.required",
        reason: "realtime_unavailable",
        poll_after_ms: 1_000,
      }).success,
    ).toBe(true);
    expect(
      realtimeServerMessageSchema.safeParse({
        type: "polling.required",
        reason: "realtime_unavailable",
        poll_after_ms: 2_000,
      }).success,
    ).toBe(false);
  });

  it("valid JSONをdecodeし、invalid JSONは本文を漏らさず固定codeで拒否する", () => {
    expect(decodeApiJson(timeResponseSchema, JSON.stringify({ server_time: NOW }))).toEqual({
      server_time: NOW,
    });

    const secretBody = '{"participant_token":"do-not-leak"';
    let refusal: unknown;
    try {
      decodeApiJson(timeResponseSchema, secretBody);
    } catch (error: unknown) {
      refusal = error;
    }

    expect(refusal).toEqual(
      new GeneratorPayloadRefusalError("generator_payload_json_invalid"),
    );
    expect(String(refusal)).not.toContain("do-not-leak");
    expect((refusal as Error).stack).not.toContain("do-not-leak");
  });

  it("contract mismatchはZod issueや識別子を保持せず固定codeで拒否する", () => {
    const sensitiveId = "77777777-7777-4777-8777-777777777777";
    let refusal: unknown;
    try {
      decodeApiJson(
        timeResponseSchema,
        JSON.stringify({ server_time: "invalid", participant_id: sensitiveId }),
      );
    } catch (error: unknown) {
      refusal = error;
    }

    expect(refusal).toEqual(
      new GeneratorPayloadRefusalError("generator_payload_contract_invalid"),
    );
    expect(String(refusal)).not.toContain(sensitiveId);
    expect((refusal as Error).stack).not.toContain(sensitiveId);
  });
});
