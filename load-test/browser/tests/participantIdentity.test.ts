import { describe, expect, it } from "vitest";
import { participantIdentity } from "../src/participantIdentity.js";

describe("participant identity evidence", () => {
  it("同一run内だけで照合でき、ID本文を保存しない", () => {
    const key = Buffer.alloc(32, 7);
    const first = participantIdentity(key, "run-a", "ABC");
    expect(first).toEqual({
      participantKey: "c87dae6928ae911a360cc7297186a80900b367fc9e148e2c81edd5613a328d1f",
      identityKeyFingerprint: "e33ec955466fb3015db2e654f649bdab0b8b64ba24bb38d8b6008aeb39945761",
    });
    expect(participantIdentity(key, "run-a", "ABC")).toEqual(first);
    expect(participantIdentity(key, "run-a", "ABD").participantKey).not.toBe(first.participantKey);
    expect(participantIdentity(key, "run-b", "ABC").participantKey).not.toBe(first.participantKey);
    expect(participantIdentity(Buffer.alloc(32, 8), "run-a", "ABC").participantKey).not.toBe(first.participantKey);
    expect(JSON.stringify(first)).not.toContain("ABC");
    expect(first.participantKey).toMatch(/^[a-f0-9]{64}$/u);
  });

  it.each([null, "", "private participant", "ABC\n", "abc"])("不正IDを固定エラーに変換する", (label) => {
    expect(() => participantIdentity(Buffer.alloc(32, 7), "run-a", label)).toThrow("participant_identity_invalid");
  });
});
