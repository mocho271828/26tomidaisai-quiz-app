import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  clearPendingParticipantJoin,
  getPendingParticipantJoinToken,
  ParticipantJoinStorageError,
} from "./pendingParticipantJoin";

const storageKey = "quiz.pending-participant-join.v1";
const token = "A".repeat(43);

describe("pending participant join", () => {
  beforeEach(() => window.sessionStorage.clear());
  afterEach(() => vi.restoreAllMocks());

  it("tokenと形式versionだけを同じタブに保存し、再取得でも同じtokenを返す", () => {
    const actual = getPendingParticipantJoinToken();

    expect(actual).toMatch(/^[A-Za-z0-9_-]{43}$/u);
    expect(JSON.parse(window.sessionStorage.getItem(storageKey) ?? "null")).toEqual({
      version: 1,
      client_join_token: actual,
    });
    expect(getPendingParticipantJoinToken()).toBe(actual);
  });

  it("端末時刻を理由にtokenを更新せず、再送期限をサーバーへ委ねる", () => {
    window.sessionStorage.setItem(storageKey, JSON.stringify({ version: 1, client_join_token: token }));
    vi.spyOn(Date, "now").mockReturnValue(Date.parse("2099-01-01T00:00:00Z"));

    expect(getPendingParticipantJoinToken()).toBe(token);
  });

  it.each([
    "broken-json",
    "null",
    JSON.stringify({ version: 2, client_join_token: token }),
    JSON.stringify({ version: 1, client_join_token: "short" }),
    JSON.stringify({ version: 1, client_join_token: token, public_label: "ABC" }),
  ])("不明な保存内容 %s は上書きせず登録を止める", (serialized) => {
    window.sessionStorage.setItem(storageKey, serialized);

    expect(getPendingParticipantJoinToken).toThrow(ParticipantJoinStorageError);
    expect(window.sessionStorage.getItem(storageKey)).toBe(serialized);
  });

  it.each(["getItem", "setItem"] as const)("storageの%sが拒否されたらtokenを渡さない", (method) => {
    vi.spyOn(Storage.prototype, method).mockImplementation(() => {
      throw new DOMException("storage unavailable", "SecurityError");
    });

    expect(getPendingParticipantJoinToken).toThrow(ParticipantJoinStorageError);
  });

  it("参加が確認されたら保存tokenを削除する", () => {
    getPendingParticipantJoinToken();

    clearPendingParticipantJoin();

    expect(window.sessionStorage.getItem(storageKey)).toBeNull();
  });

  it("端末の削除拒否でサーバー確認済みの参加を失敗扱いしない", () => {
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new DOMException("storage unavailable", "SecurityError");
    });

    expect(clearPendingParticipantJoin).not.toThrow();
  });
});
