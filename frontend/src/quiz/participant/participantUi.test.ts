import { describe, expect, it } from "vitest";

import {
  createParticipantJoinToken,
  formatEventStartCountdown,
} from "./participantUi";

describe("participant UI", () => {
  it("再送識別用tokenを表示IDとは別の256bit URL-safe文字列で生成する", () => {
    expect(createParticipantJoinToken()).toMatch(/^[A-Za-z0-9_-]{43}$/u);
  });

  it("開催時刻までの残り分を切り上げ、開始後は断定しない案内へ切り替える", () => {
    const startAt = "2026-08-08T01:10:00Z";
    expect(
      formatEventStartCountdown(startAt, Date.parse("2026-08-08T01:00:01Z")),
    ).toBe("開始まで残り10分");
    expect(
      formatEventStartCountdown(startAt, Date.parse("2026-08-08T01:10:00Z")),
    ).toBe("まもなく開始");
    expect(formatEventStartCountdown(null, Date.now())).toBeNull();
    expect(formatEventStartCountdown("invalid", Date.now())).toBeNull();
  });
});
