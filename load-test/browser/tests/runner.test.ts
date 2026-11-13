import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { awsRunConfigSchema, configSchema, controlRunConfigSchema, localHttpsOrigin } from "../src/config.js";
import { routeClass } from "../src/participant.js";
import { telemetrySchema } from "../src/evidence.js";

const validConfig = {
  runId: "runner-boundary-test", sourceCommit: "0".repeat(40), baseUrl: "https://localhost:8443",
  count: 100, ordinalStart: 1, startAt: "2026-09-30T15:00:00+09:00", durationSeconds: 300,
  joinSpreadMs: 0, operatorStartDelaySeconds: 90, preScheduleMarginMs: 5000, resultHoldMs: 5000,
  answerDelayMs: 0, scenario: "normal", faultAfterSeconds: 30, faultDurationMs: 1000,
  outputDirectory: "/tmp/local-browser-evidence", channel: "chromium", headless: true,
};

describe("local TLS browser runner boundaries", () => {
  it("requires private/loopback HTTPS without credentials or arbitrary path", () => {
    expect(localHttpsOrigin("https://127.0.0.1:8443")).toBe(true);
    expect(localHttpsOrigin("https://192.168.1.20:8443")).toBe(true);
    for (const url of ["http://127.0.0.1", "https://example.com", "https://169.254.169.254",
      "https://user:secret@localhost", "https://localhost/secret", "https://localhost/?secret=a"])
      expect(localHttpsOrigin(url)).toBe(false);
  });
  it("rejects extra configuration including insecure TLS switches", () => {
    expect(configSchema.safeParse({ignoreHTTPSErrors: true}).success).toBe(false);
  });
  it("defaults to the existing non-ramped flow and accepts increasing stages through count", () => {
    expect(configSchema.parse(validConfig)).toMatchObject({rampStages: [], rampHoldMs: 5000});
    expect(configSchema.parse({...validConfig, rampStages: [5, 20, 50, 100], rampHoldMs: 1000}))
      .toMatchObject({rampStages: [5, 20, 50, 100], rampHoldMs: 1000});
    expect(configSchema.safeParse({...validConfig, count: 1, rampStages: [1], rampHoldMs: 30000}).success).toBe(true);
  });
  it("rejects unordered, incomplete and out-of-range ramp settings", () => {
    for (const rampStages of [[0, 100], [5, 5, 100], [20, 5, 100], [5, 20, 50], [5, 101], [1.5, 100]])
      expect(configSchema.safeParse({...validConfig, rampStages}).success).toBe(false);
    expect(configSchema.safeParse({...validConfig, count: 50, rampStages: [5, 100]}).success).toBe(false);
    for (const rampHoldMs of [999, 30001, 1000.5])
      expect(configSchema.safeParse({...validConfig, rampHoldMs}).success).toBe(false);
  });
  it("drops UUIDs and URL query from route classification", () => {
    expect(routeClass("https://localhost/api/v1/participant/rounds/private-id/answers?secret=x")).toBe("answer");
    expect(routeClass("https://localhost/api/v1/participant/result?secret=x")).toBe("personal_result");
    expect(routeClass("https://localhost/unknown-secret")).toBe("other");
  });
  it("rejects secrets and unrecognized telemetry payload", () => {
    const event = {kind: "clock_sample", rtt_ms: 25, offset_ms: 10,
      monotonic_ms: 20, wall_time_ms: 123};
    expect(telemetrySchema.safeParse(event).success).toBe(true);
    expect(telemetrySchema.safeParse({...event, participant_id: "secret"}).success).toBe(false);
    expect(telemetrySchema.safeParse({...event, rtt_ms: Infinity}).success).toBe(false);
  });
  it("accepts the separate optional notice visibility without changing the receipt observation contract", () => {
    const display = {kind: "participant_display", round_number: 1, phase: "collecting",
      synchronized: true, has_question: true, submission_status: "accepted",
      answer_dom_present: true, answer_dom_visible: false, answer_notice_visible: true,
      result_dom_present: false, monotonic_ms: 20, wall_time_ms: 123};
    expect(telemetrySchema.safeParse(display).success).toBe(true);
    const previousDisplay = Object.fromEntries(Object.entries(display).filter(([name]) => name !== "answer_notice_visible"));
    expect(telemetrySchema.safeParse(previousDisplay).success).toBe(true);
    expect(telemetrySchema.safeParse({...display, answer_notice_visible: "true"}).success).toBe(false);
    expect(telemetrySchema.safeParse({...display, answer_payload: "secret"}).success).toBe(false);
  });
});

describe("explicit AWS manual runner boundaries", () => {
  beforeEach(() => { vi.stubEnv("AWS_REVIEW_DOMAIN_NAME", "quiz.example.org"); });
  afterEach(() => { vi.unstubAllEnvs(); });

  it.each([7, 10, 12])("keeps UI observations for round %i", round => {
    expect(telemetrySchema.safeParse({kind: "participant_display", round_number: round, phase: "result",
      synchronized: true, has_question: true, result_dom_present: true, monotonic_ms: 20, wall_time_ms: 123}).success).toBe(true);
  });

  it.each([-1, 1.5, 4096])("rejects invalid or unbounded displayed rounds: %s", round => {
    expect(telemetrySchema.safeParse({kind: "participant_display", round_number: round, phase: "result",
      synchronized: true, has_question: true, result_dom_present: true, monotonic_ms: 20, wall_time_ms: 123}).success).toBe(false);
  });

  const awsConfig = {...validConfig, baseUrl: "https://d123abc.cloudfront.net", durationSeconds: null,
    aws: {recordResults: true, sourceDirty: false}};

  it("keeps the local and automatic schemas bounded while accepting independent AWS runs", () => {
    expect(configSchema.safeParse(awsConfig).success).toBe(false);
    expect(configSchema.safeParse({...validConfig, durationSeconds: null}).success).toBe(false);
    expect(controlRunConfigSchema.parse(awsConfig)).toMatchObject({durationSeconds: null, ordinalStart: 1});
    expect(awsRunConfigSchema.parse({...awsConfig, ordinalStart: 551})).toMatchObject({count: 100});
    expect(awsRunConfigSchema.parse({...awsConfig, aws: {...awsConfig.aws, recordResults: false}})).toMatchObject({aws: {recordResults: false}});
    expect(awsRunConfigSchema.safeParse({...awsConfig, durationSeconds: 900}).success).toBe(false);
  });

  it.each(["https://quiz.example.org", "https://d123abc.cloudfront.net"])(
    "accepts the approved canonical origin only for the AWS runner: %s", baseUrl => {
      expect(controlRunConfigSchema.parse({...awsConfig, baseUrl})).toMatchObject({baseUrl, durationSeconds: null});
      expect(configSchema.safeParse({...validConfig, baseUrl}).success).toBe(false);
    },
  );

  it.each(["http://d123abc.cloudfront.net", "https://d123abc.cloudfront.net/", "https://d123abc.cloudfront.net:443",
    "https://D123ABC.cloudfront.net", "https://d123abc.cloudfront.net?x=1", "https://d123abc.cloudfront.net#x",
    "https://user:secret@d123abc.cloudfront.net", "https://d123abc.cloudfront.net/path",
    "https://d123abc.cloudfront.net.evil.test", "https://evil.test", "https://127.0.0.1", "https://10.0.1.2",
    "https://d" + "a".repeat(63) + ".cloudfront.net"])("rejects an unapproved AWS origin shape: %s", baseUrl => {
    expect(awsRunConfigSchema.safeParse({...awsConfig, baseUrl}).success).toBe(false);
  });

  it.each([
    "http://quiz.example.org", "https://quiz.example.org/",
    "https://quiz.example.org:443", "https://quiz.example.org:8443",
    "https://quiz.example.org/path", "https://quiz.example.org?x=1",
    "https://quiz.example.org#x", "https://user:secret@quiz.example.org",
    "https://quiz.example.org.evil.test", "https://quiz.example.org.",
    "https://QUIZ.example.org", "https://www.quiz.example.org",
    "https://guest26.example.org", "https://example.org", "https://other.example.org",
    " https://quiz.example.org", "https://quiz.example.org\n",
  ])("rejects a noncanonical or unapproved custom origin: %s", baseUrl => {
    expect(controlRunConfigSchema.safeParse({...awsConfig, baseUrl}).success).toBe(false);
  });

  it("requires an explicit recording flag and rejects obsolete coordination or automatic settings", () => {
    for (const patch of [
      {ordinalStart: 0}, {ordinalStart: 552}, {count: 101},
      {aws: undefined}, {aws: {}}, {aws: {...awsConfig.aws, recordResults: "false"}}, {aws: {...awsConfig.aws, recordResults: 0}},
      {aws: {recordResults: true}}, {aws: {...awsConfig.aws, sourceDirty: "false"}},
      {aws: {pcIndex: 2, shardVus: [300, 300]}}, {aws: {...awsConfig.aws, ignored: true}},
      {scenario: "reconnect"}, {rampStages: [100]}, {coordination: {directory: "/tmp/shared", cohorts: [{count: 100, ordinalStart: 301}]}},
      {ignoreHTTPSErrors: true},
    ]) expect(awsRunConfigSchema.safeParse({...awsConfig, ...patch}).success).toBe(false);
  });
});
