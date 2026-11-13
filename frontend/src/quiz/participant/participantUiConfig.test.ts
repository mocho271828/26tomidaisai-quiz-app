import { describe, expect, it } from "vitest";

import { parseParticipantUiConfig } from "./participantUiConfig";

describe("participant UI config", () => {
  it("未設定値を安全な既定値へ変換する", () => {
    expect(parseParticipantUiConfig({})).toEqual({
      eventStartAt: null,
      homeUrl: null,
      surveyUrl: null,
    });
  });

  it("offset付き開始日時と外部HTTPS遷移先を受理する", () => {
    expect(
      parseParticipantUiConfig({
        VITE_EVENT_START_AT: "2026-10-10T10:00:00+09:00",
        VITE_HOME_URL: "https://festival.example.org/home",
        VITE_SURVEY_URL: "https://forms.example.jp/survey?id=2026",
      }),
    ).toEqual({
      eventStartAt: "2026-10-10T10:00:00+09:00",
      homeUrl: "https://festival.example.org/home",
      surveyUrl: "https://forms.example.jp/survey?id=2026",
    });
  });

  it.each([
    "javascript:alert(1)",
    "http://example.jp/survey",
    "//evil.example/survey",
    "https://user:password@example.jp/survey",
  ])("危険な遷移先 %s を拒否する", (target) => {
    expect(() =>
      parseParticipantUiConfig({ VITE_SURVEY_URL: target }),
    ).toThrow();
  });

  it.each([
    "/",
    "/festival-home",
    "https://quiz.example.org/",
    "https://quiz.example.org/result",
    "https://quiz.example.org:8443/survey",
    "https://quiz.example.org./survey",
    "https://quiz.example.org../survey",
    "https://QUIZ.EXAMPLE.ORG?survey=1",
  ])(
    "アプリ内へ循環または404になる遷移先 %s を拒否する",
    (target) => {
      expect(() =>
        parseParticipantUiConfig({ VITE_HOME_URL: target }, "quiz.example.org"),
      ).toThrow();
      expect(() =>
        parseParticipantUiConfig({ VITE_SURVEY_URL: target }, "quiz.example.org"),
      ).toThrow();
    },
  );

  it("アプリ以外の企画のHTTPSサイトを終了リンクに利用できる", () => {
    expect(parseParticipantUiConfig({ VITE_HOME_URL: "https://festival.example.org/" }))
      .toMatchObject({ homeUrl: "https://festival.example.org/" });
  });

  it("offsetのない曖昧な開始日時を拒否する", () => {
    expect(() =>
      parseParticipantUiConfig({
        VITE_EVENT_START_AT: "2026-10-10T10:00:00",
      }),
    ).toThrow();
  });
});
