import { describe, expect, it } from "vitest";
import { answerReceiptSchema, participantAnswerSchema, submitAnswerRequestSchema } from "./contracts";

const identity = { round_version: 3, option_id: "60000000-0000-4000-8000-000000000001",
  client_answer_id: "60000000-0000-4000-8000-000000000002" };
const answer = { answer_id: identity.client_answer_id, round_id: "60000000-0000-4000-8000-000000000003",
  option_id: identity.option_id, client_answer_id: identity.client_answer_id,
  received_at: "2026-10-15T00:00:03Z", client_elapsed_ms: 2089, timing_basis: "client_monotonic_v1" };

describe("正式な端末回答時間のAPI契約", () => {
  it.each([true, "2000", 0.1, -1, null, undefined, Number.NaN, Number.POSITIVE_INFINITY])(
    "非整数・欠測 %s は送信しない", (client_elapsed_ms) => {
      expect(submitAnswerRequestSchema.safeParse({ ...identity, client_elapsed_ms }).success).toBe(false);
    },
  );
  it.each([0, 99, 100, 9999])("非負整数 %s ms をそのまま通す", (client_elapsed_ms) => {
    expect(submitAnswerRequestSchema.parse({ ...identity, client_elapsed_ms }).client_elapsed_ms).toBe(client_elapsed_ms);
  });
  it.each([10000, 12000, 14999])("選択期限以上の %s ms を送信前に拒否する", (client_elapsed_ms) => {
    expect(submitAnswerRequestSchema.safeParse({ ...identity, client_elapsed_ms }).success).toBe(false);
  });
  it("新方式の欠測はreceiptと保存状態のどちらでも拒否する", () => {
    expect(participantAnswerSchema.parse(answer).client_elapsed_ms).toBe(2089);
    expect(participantAnswerSchema.safeParse({ ...answer, client_elapsed_ms: null }).success).toBe(false);
    expect(answerReceiptSchema.safeParse({ ...answer, client_elapsed_ms: null, is_replay: true }).success).toBe(false);
    expect(participantAnswerSchema.safeParse({ ...answer, client_elapsed_ms: null, timing_basis: "server_received_v1" }).success).toBe(true);
  });
  it("未知fieldと従来selected_atを正式な送信値として通さない", () => {
    expect(submitAnswerRequestSchema.safeParse({ ...identity, client_elapsed_ms: 2089, selected_at: answer.received_at }).success).toBe(false);
  });
});
