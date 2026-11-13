import { describe, expect, it } from "vitest";
import type { ParticipantAnswer } from "../../api/contracts";
import { createAcceptedParticipantSubmission, createParticipantSubmission, isSameSubmission } from "./participantUi";

const answer: ParticipantAnswer = {
  answer_id: "60000000-0000-4000-8000-000000000001",
  round_id: "60000000-0000-4000-8000-000000000002",
  option_id: "60000000-0000-4000-8000-000000000003",
  client_answer_id: "60000000-0000-4000-8000-000000000004",
  received_at: "2026-08-08T01:00:03.500Z", client_elapsed_ms: 2000, timing_basis: "client_monotonic_v1",
};

describe("participant submission timing", () => {
  it("再読込後も受付時刻と別に正式整数msを復元する", () => {
    const accepted = createAcceptedParticipantSubmission(answer, 4);
    expect(accepted.clientElapsedMs).toBe(2000);
    expect(accepted.receivedAt).toBe(answer.received_at);
    expect(accepted.timingBasis).toBe("client_monotonic_v1");
    expect(accepted.status).toBe("accepted");
  });
  it("同じID・選択肢でも時間を変更した回答は同一payloadとして扱わない", () => {
    const identity = { ...answer, round_version: 3 };
    const previous = createParticipantSubmission(identity, "unknown", "確認中");
    expect(isSameSubmission(previous, identity)).toBe(true);
    expect(isSameSubmission(previous, { ...identity, client_elapsed_ms: 1999 })).toBe(false);
  });
  it("旧方式の記録は端末時間を捏造せず方式を保持する", () => {
    const accepted = createAcceptedParticipantSubmission({ ...answer, client_elapsed_ms: null,
      timing_basis: "server_received_v1" }, 3);
    expect(accepted.clientElapsedMs).toBeNull();
    expect(accepted.timingBasis).toBe("server_received_v1");
  });
});
