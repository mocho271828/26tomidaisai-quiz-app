import { beforeEach, describe, expect, it } from "vitest";

import {
  clearPendingSubmission,
  loadPendingSubmission,
  savePendingSubmission,
} from "./pendingSubmission";

const pending = {
  client_elapsed_ms: 2000,
  round_id: "60000000-0000-4000-8000-000000000002",
  round_version: 3,
  option_id: "60000000-0000-4000-8000-000000000003",
  client_answer_id: "60000000-0000-4000-8000-000000000004",
} as const;

describe("pending participant submission", () => {
  beforeEach(() => window.sessionStorage.clear());

  it("Zod検証済みpayloadだけを復元する", () => {
    expect(savePendingSubmission(pending)).toBe(true);
    expect(loadPendingSubmission()).toEqual(pending);
  });

  it("壊れた外部入力を破棄する", () => {
    window.sessionStorage.setItem(
      "quiz.pending-participant-answer.v1",
      JSON.stringify({ ...pending, round_version: "3" }),
    );

    expect(loadPendingSubmission()).toBeNull();
    expect(loadPendingSubmission()).toBeNull();
  });

  it("確定後にpayloadを削除する", () => {
    savePendingSubmission(pending);
    clearPendingSubmission();

    expect(loadPendingSubmission()).toBeNull();
  });

  it("正式な整数msを再読込後も保持する", () => {
    const selected = { ...pending, client_elapsed_ms: 2089 };
    savePendingSubmission(selected);
    expect(loadPendingSubmission()).toEqual(selected);
  });

  it.each(["2000", 0.1, -1, null, true, undefined])(
    "不正または欠測の正式時間 %s は復元しない",
    (client_elapsed_ms) => {
      window.sessionStorage.setItem(
        "quiz.pending-participant-answer.v1",
        JSON.stringify({ ...pending, client_elapsed_ms }),
      );
      expect(loadPendingSubmission()).toBeNull();
    },
  );
});
