import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BrowserContext, Page } from "playwright";
import { awsRunConfigSchema, configSchema } from "../src/config.js";
import type { Evidence, SafeEvent, Telemetry } from "../src/evidence.js";
import { ManualParticipant } from "../src/manualParticipant.js";
import { manualCommandSchema, spreadOffset, type ManualAction, type ManualCommand } from "../src/manualProtocol.js";
import type { ManualBrowserDriver, ManualClick } from "../src/manualBrowser.js";
import type { Actor } from "../src/participant.js";

const config = configSchema.parse({runId: "manual-unit", sourceCommit: "0".repeat(40),
  baseUrl: "https://127.0.0.1", count: 1, ordinalStart: 1, startAt: "2026-10-02T00:00:00Z",
  durationSeconds: 180, joinSpreadMs: 0, operatorStartDelaySeconds: 10, preScheduleMarginMs: 1000,
  resultHoldMs: 3000, answerDelayMs: 0, scenario: "normal", faultAfterSeconds: 5,
  faultDurationMs: 100, outputDirectory: "/tmp/manual-unit", channel: "chromium", headless: true});

function command(kind: "answer" | "pause" | "resume" | "stop" = "answer", overrides: object = {}): ManualCommand {
  return manualCommandSchema.parse({runId: config.runId, commandId: "cmd-1", issuedAtMs: Date.now(),
    target: {ordinalStart: 1, count: 1}, kind,
    ...(kind === "answer" ? {round: 1, mode: "when-open", delayMs: 0, spreadMs: 0} : {}), ...overrides});
}

function fixture(isAws = false, expectedRounds = 6) {
  const records: SafeEvent[] = [];
  const navigation = vi.fn().mockResolvedValue(undefined);
  const joinButton = vi.fn().mockResolvedValue(undefined);
  const fonts = vi.fn().mockResolvedValue(undefined);
  const page = {isClosed: () => false, evaluate: vi.fn().mockResolvedValue(null), goto: navigation,
    getByRole: () => ({waitFor: joinButton}), waitForFunction: fonts} as unknown as Page;
  const actor: Actor = {page, context: {} as BrowserContext, serial: 1,
    connected: false, joined: true, complete: false, failed: false, stopped: false,
    wsOpens: 0, wsCloses: 0, rounds: [], results: [], failures: [], pending: Promise.resolve()};
  const evidence = {event: (record: SafeEvent): void => { records.push(record); }} as unknown as Evidence;
  const pendingAnswers = new Map<string, (click: ManualClick) => void>();
  const answer = vi.fn((action: Extract<ManualAction, {kind: "answer"}>) =>
    new Promise<ManualClick>(resolve => { pendingAnswers.set(action.commandId, resolve); }));
  const finishAnswer = (click: ManualClick, commandId = "cmd-1"): void => {
    const resolve = pendingAnswers.get(commandId);
    if (!resolve) throw new Error("fixture_answer_not_pending");
    pendingAnswers.delete(commandId);
    resolve(click);
  };
  const driver: ManualBrowserDriver = {initialize: vi.fn().mockResolvedValue(undefined),
    observeResult: vi.fn().mockResolvedValue(null),
    answer,
    pause: vi.fn(async (_paused: boolean, stopped = false): Promise<void> => {
      if (stopped) for (const commandId of pendingAnswers.keys())
        finishAnswer({clicked: false, reason: "run_stopped"}, commandId);
    }), click: vi.fn().mockResolvedValue({clicked: true, browserMonotonicMs: 10})};
  const runConfig = isAws ? awsRunConfigSchema.parse({...config, baseUrl: "https://d123abc.cloudfront.net",
    durationSeconds: null, aws: {recordResults: true, sourceDirty: false}}) : config;
  const participant = new ManualParticipant(actor, runConfig, evidence,
    {operationTimeoutMs: 1000, deadlineAtMs: isAws ? null : Date.now() + 180000, expectedRounds}, driver);
  return {records, actor, driver, participant, navigation, joinButton, fonts, finishAnswer};
}

function display(round: number, phase: "answering" | "collecting" = "answering"): Extract<Telemetry, {kind: "participant_display"}> {
  return {kind: "participant_display", round_number: round, phase, synchronized: true,
    has_question: true, monotonic_ms: 10, wall_time_ms: Date.now()};
}

describe("manual participant command boundaries", () => {
  it("continues answers after an observation failure while retaining the failed result", async () => {
    const {actor, participant, driver, finishAnswer} = fixture(true);
    actor.failed = true;
    actor.failures.push({round: 0, stage: "join"});
    await participant.dispatch(command());
    expect(driver.answer).toHaveBeenCalledWith(expect.objectContaining({round: 1}), 1, null);
    finishAnswer({clicked: true, browserMonotonicMs: 10});
    await vi.advanceTimersByTimeAsync(0);
    participant.observe({kind: "answer", phase: "sending", round_number: 1,
      monotonic_ms: 10, wall_time_ms: Date.now()});
    participant.observe({...display(1), answer_dom_visible: true});
    expect(participant.status()).toMatchObject({joined: true, failed: true, failures: 1,
      clicked: [1], accepted: [1], complete: false});
    expect(actor.failures).toEqual([{round: 0, stage: "join"}]);
    await participant.stop();
  });

  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it.each([7, 10, 12])("completes %i rounds only after every click, receipt and visible result", async expectedRounds => {
    const {participant, driver, finishAnswer, records} = fixture(true, expectedRounds);
    vi.mocked(driver.observeResult).mockResolvedValue({unanswered: false});
    for (let round = 1; round <= expectedRounds; round++) {
      const commandId = `round-${round}`;
      await participant.dispatch(command("answer", {round, commandId}));
      finishAnswer({clicked: true, browserMonotonicMs: 10}, commandId);
      await vi.advanceTimersByTimeAsync(0);
      participant.observe({kind: "answer", phase: "sending", round_number: round, monotonic_ms: 10, wall_time_ms: Date.now()});
      participant.observe({...display(round), answer_dom_visible: true});
      expect(participant.status().complete).toBe(false);
      participant.observe({...display(round, "collecting"), result_dom_present: true});
      await vi.advanceTimersByTimeAsync(0);
      expect(participant.status().complete).toBe(round === expectedRounds);
    }
    expect(participant.status()).toMatchObject({failed: false, currentRound: expectedRounds, complete: true});
    await participant.dispatch(command("answer", {round: expectedRounds + 1, commandId: "outside-plan"}));
    expect(records.at(-1)).toMatchObject({event: "manual_command_rejected", reason: "round_outside_plan"});
    expect(driver.answer).toHaveBeenCalledTimes(expectedRounds);
    await participant.stop();
  });

  it("does not report prepared while fonts are still transferring or click to join during preparation", async () => {
    const {participant, actor, driver, records, fonts} = fixture();
    actor.joined = false;
    let finishFonts!: () => void;
    fonts.mockReturnValue(new Promise<void>(resolve => { finishFonts = resolve; }));
    const preparing = participant.prepare();
    await vi.advanceTimersByTimeAsync(0);
    expect(participant.status()).toMatchObject({ready: false, joined: false});
    expect(records.some(record => record.event === "manual_prepared")).toBe(false);
    finishFonts();
    await preparing;
    expect(participant.status()).toMatchObject({ready: true, joined: false});
    expect(driver.click).not.toHaveBeenCalled();
    await participant.stop();
  });

  it.each(["navigation", "join_button", "fonts"] as const)("records %s preparation failures without marking the VU ready", async stage => {
    const state = fixture();
    const operation = stage === "navigation" ? state.navigation : stage === "join_button" ? state.joinButton : state.fonts;
    operation.mockRejectedValue(Object.assign(new Error("fixture private error"), {name: "TimeoutError"}));
    await state.participant.prepare();
    expect(state.participant.status()).toMatchObject({ready: false, failed: true, timeouts: 1});
    expect(state.records).toContainEqual(expect.objectContaining({event: "manual_operation_failed", stage, reason: "timeout"}));
    expect(JSON.stringify(state.records)).not.toContain("fixture private error");
    await state.participant.stop();
  });

  it.each(["navigation", "join_button", "fonts"] as const)("stops during %s without waiting for the browser operation or starting another stage", async stage => {
    const state = fixture();
    state.actor.joined = false;
    const operations = [state.navigation, state.joinButton, state.fonts];
    const index = stage === "navigation" ? 0 : stage === "join_button" ? 1 : 2;
    let finishOperation!: () => void;
    operations[index]!.mockReturnValue(new Promise<void>(resolve => { finishOperation = resolve; }));
    const preparing = state.participant.prepare();
    await vi.advanceTimersByTimeAsync(0);
    expect(operations[index]).toHaveBeenCalledOnce();
    await state.participant.stop();
    await preparing;
    expect(state.participant.status()).toMatchObject({ready: false, stopped: true, failed: false, timeouts: 0});
    finishOperation();
    await vi.advanceTimersByTimeAsync(0);
    for (const operation of operations.slice(index + 1)) expect(operation).not.toHaveBeenCalled();
    expect(state.records.some(record => ["manual_prepared", "manual_operation_failed"].includes(record.event))).toBe(false);
    expect(state.driver.click).not.toHaveBeenCalled();
  });

  it("does not start navigation after stopping", async () => {
    const {participant, navigation} = fixture();
    await participant.stop();
    await participant.prepare();
    expect(navigation).not.toHaveBeenCalled();
  });

  it("keeps AWS reservations active past an hour and settles them only on explicit stop", async () => {
    const {participant, driver, records} = fixture(true);
    await vi.advanceTimersByTimeAsync(7200000);
    await participant.dispatch(command());
    expect(driver.answer).toHaveBeenCalledWith(expect.objectContaining({round: 1}), 1, null);
    await vi.advanceTimersByTimeAsync(7200000);
    expect(participant.status()).toMatchObject({pending: 1, stopped: false, failed: false, timeouts: 0});
    await participant.stop();
    expect(participant.status()).toMatchObject({pending: 0, stopped: true, clicked: []});
    expect(records).toContainEqual(expect.objectContaining({event: "manual_operation_skipped", reason: "run_stopped"}));
  });

  it("still bounds receipt observation after an AWS answer without a run deadline", async () => {
    const {participant, finishAnswer} = fixture(true);
    await vi.advanceTimersByTimeAsync(7200000);
    await participant.dispatch(command());
    finishAnswer({clicked: true, browserMonotonicMs: 10});
    await vi.advanceTimersByTimeAsync(1000);
    expect(participant.status()).toMatchObject({clicked: [1], timeouts: 1, failed: true});
    await participant.stop();
  });

  it("arms the browser before display notification and waits for human opening beyond the operation timeout", async () => {
    const {participant, driver, records, finishAnswer} = fixture();
    const action = command("answer", {delayMs: 500});
    const deadlineAtMs = Date.now() + 180000;
    await participant.dispatch(action);
    expect(driver.answer).toHaveBeenCalledExactlyOnceWith(action, 1, deadlineAtMs);
    expect(driver.click).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(10000);
    expect(participant.status()).toMatchObject({pending: 1, clicked: [], timeouts: 0, failed: false});
    expect(records.some(record => record.event === "manual_operation_planned")).toBe(false);
    finishAnswer({clicked: true, browserMonotonicMs: 750, browserPlannedMonotonicMs: 700});
    await vi.advanceTimersByTimeAsync(0);
    expect(participant.status()).toMatchObject({pending: 0, clicked: [1], timeouts: 0});
    const clicked = records.find(record => record.event === "manual_answer_clicked");
    expect(clicked).toMatchObject({round: 1, browserMonotonicMs: 750, browserPlannedMonotonicMs: 700});
    expect(clicked?.plannedAtMs).toBeUndefined();
    await participant.dispatch(action);
    await participant.dispatch(command("answer", {commandId: "different-id"}));
    expect(driver.answer).toHaveBeenCalledTimes(1);
    expect(records.filter(record => record.event === "manual_command_rejected").map(record => record.reason))
      .toEqual(["duplicate_command", "duplicate_operation"]);
    await vi.advanceTimersByTimeAsync(1000);
    expect(participant.status().timeouts).toBe(1);
    await participant.stop();
  });

  it("delegates now availability to the browser without waiting for a Node display observation", async () => {
    const {participant, driver, records, finishAnswer} = fixture();
    const action = command("answer", {mode: "now"});
    await participant.dispatch(action);
    expect(driver.answer).toHaveBeenCalledWith(action, 1, Date.now() + 180000);
    expect(records).toContainEqual(expect.objectContaining({event: "manual_command_accepted", commandId: "cmd-1"}));
    finishAnswer({clicked: false, reason: "round_not_answerable"});
    await vi.advanceTimersByTimeAsync(0);
    expect(records).toContainEqual(expect.objectContaining({event: "manual_operation_skipped", reason: "round_not_answerable"}));
    expect(participant.status()).toMatchObject({pending: 0, clicked: [], failed: false});
    await participant.dispatch(action);
    expect(records.at(-1)?.reason).toBe("duplicate_command");
    const retry = command("answer", {commandId: "reserve-after-early-now"});
    await participant.dispatch(retry);
    expect(driver.answer).toHaveBeenLastCalledWith(retry, 1, Date.now() + 180000);
    expect(driver.answer).toHaveBeenCalledTimes(2);
    finishAnswer({clicked: true, browserMonotonicMs: 15}, "reserve-after-early-now");
    await vi.advanceTimersByTimeAsync(0);
    expect(participant.status().clicked).toEqual([1]);
    await participant.dispatch(command("answer", {commandId: "after-click"}));
    expect(records.at(-1)?.reason).toBe("duplicate_operation");
    await participant.stop();
  });

  it("keeps now planning in the Node clock without attributing browser drift", async () => {
    const {participant, driver, records, finishAnswer} = fixture();
    const action = command("answer", {mode: "now", issuedAtMs: Date.now() - 1000, delayMs: 500});
    const plannedAtMs = Date.now() - 500;
    await participant.dispatch(action);
    expect(driver.answer).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(25);
    finishAnswer({clicked: true, browserMonotonicMs: 900000});
    await vi.advanceTimersByTimeAsync(0);
    const clicked = records.find(record => record.event === "manual_answer_clicked");
    expect(clicked).toMatchObject({plannedAtMs, actualAtMs: Date.now(), operationLagMs: 525,
      browserMonotonicMs: 900000});
    expect(clicked?.browserPlannedMonotonicMs).toBeUndefined();
    expect(clicked?.browserOperationLagMs).toBeUndefined();
    await participant.stop();
  });

  it("retains the round reservation after an uncertain browser failure", async () => {
    const {participant, driver, records} = fixture();
    vi.mocked(driver.answer).mockRejectedValueOnce(Object.assign(new Error("fixture private error"), {name: "TimeoutError"}));
    await participant.dispatch(command("answer", {mode: "now"}));
    await vi.advanceTimersByTimeAsync(0);
    expect(participant.status()).toMatchObject({pending: 0, failed: true, timeouts: 1});
    expect(records).toContainEqual(expect.objectContaining({event: "manual_operation_failed", stage: "answer", reason: "timeout"}));
    expect(JSON.stringify(records)).not.toContain("fixture private error");
    await participant.dispatch(command("answer", {commandId: "uncertain-retry"}));
    expect(records.at(-1)?.reason).toBe("duplicate_operation");
    expect(driver.answer).toHaveBeenCalledTimes(1);
    await participant.stop();
  });

  it("rejects now before joining instead of carrying it through join completion", async () => {
    const {participant, driver, actor, records} = fixture();
    actor.joined = false;
    await participant.dispatch(command("answer", {mode: "now"}));
    expect(records).toContainEqual(expect.objectContaining({event: "manual_command_rejected",
      commandId: "cmd-1", reason: "round_not_answerable"}));
    expect(participant.status()).toMatchObject({pending: 0, clicked: [], failed: false});
    expect(driver.answer).not.toHaveBeenCalled();
    actor.joined = true;
    participant.observe(display(1));
    await vi.advanceTimersByTimeAsync(0);
    expect(driver.answer).not.toHaveBeenCalled();
    const retry = command("answer", {commandId: "now-after-join", mode: "now"});
    await participant.dispatch(retry);
    expect(driver.answer).toHaveBeenCalledExactlyOnceWith(retry, 1, Date.now() + 180000);
    await participant.stop();
  });

  it("waits for join completion before arming an answer", async () => {
    const {participant, driver, actor} = fixture();
    actor.joined = false;
    await participant.dispatch(command());
    participant.observe(display(1));
    await vi.advanceTimersByTimeAsync(10000);
    expect(driver.answer).not.toHaveBeenCalled();
    expect(participant.status()).toMatchObject({pending: 1, timeouts: 0});
    actor.joined = true;
    participant.observe(display(1));
    expect(driver.answer).toHaveBeenCalledTimes(1);
    await participant.stop();
  });

  it("rejects an expired round before arming and never carries a browser reservation into the next round", async () => {
    const {participant, driver, records, finishAnswer} = fixture();
    await participant.dispatch(command("answer", {delayMs: 2000}));
    participant.observe(display(1, "collecting"));
    participant.observe(display(2));
    finishAnswer({clicked: false, reason: "round_expired"});
    await vi.advanceTimersByTimeAsync(10000);
    expect(driver.answer).toHaveBeenCalledTimes(1);
    expect(driver.click).not.toHaveBeenCalled();
    expect(participant.status()).toMatchObject({pending: 0, clicked: [], timeouts: 0});
    expect(records).toContainEqual(expect.objectContaining({event: "manual_operation_skipped", round: 1, reason: "round_expired"}));
    await participant.dispatch(command("answer", {commandId: "duplicate", mode: "now"}));
    expect(records.at(-1)?.reason).toBe("duplicate_operation");
    const fresh = fixture();
    fresh.participant.observe(display(2));
    await fresh.participant.dispatch(command("answer", {commandId: "stale", mode: "now"}));
    expect(fresh.records.at(-1)?.reason).toBe("round_expired");
    expect(fresh.driver.answer).not.toHaveBeenCalled();
    await participant.stop();
    await fresh.participant.stop();
  });

  it("expires an answer waiting for join without arming it", async () => {
    const {participant, driver, actor, records} = fixture();
    actor.joined = false;
    await participant.dispatch(command());
    participant.observe(display(2));
    actor.joined = true;
    participant.observe(display(2));
    expect(driver.answer).not.toHaveBeenCalled();
    expect(participant.status().pending).toBe(0);
    expect(records).toContainEqual(expect.objectContaining({event: "manual_operation_skipped", reason: "round_expired"}));
    await participant.stop();
  });

  it("marks stop during receipt waiting as interrupted observation", async () => {
    const {participant, records, finishAnswer} = fixture();
    await participant.dispatch(command());
    finishAnswer({clicked: true, browserMonotonicMs: 10});
    await vi.advanceTimersByTimeAsync(0);
    await participant.stop();
    expect(records).toContainEqual(expect.objectContaining({event: "manual_observation_interrupted", count: 1}));
    expect(participant.status()).toMatchObject({clicked: [1], accepted: [], complete: false});
  });

  it("registers answers while paused without rearming or resetting their delays on resume", async () => {
    const {participant, driver, finishAnswer} = fixture();
    await participant.dispatch(command("pause"));
    const action = command("answer", {commandId: "reserve", delayMs: 500});
    await participant.dispatch(action);
    expect(driver.answer).toHaveBeenCalledExactlyOnceWith(action, 1, Date.now() + 180000);
    await vi.advanceTimersByTimeAsync(600);
    expect(participant.status()).toMatchObject({paused: true, pending: 1, clicked: []});
    await participant.dispatch(command("resume", {commandId: "resume-1"}));
    expect(driver.pause).toHaveBeenNthCalledWith(1, true);
    expect(driver.pause).toHaveBeenNthCalledWith(2, false);
    expect(driver.answer).toHaveBeenCalledTimes(1);
    finishAnswer({clicked: true, browserMonotonicMs: 610}, "reserve");
    await vi.advanceTimersByTimeAsync(0);
    expect(participant.status()).toMatchObject({paused: false, clicked: [1]});
    await participant.stop();
  });

  it("uses same-browser monotonic latency and continues receipt observations during pause", async () => {
    const {participant, records, finishAnswer} = fixture();
    await participant.dispatch(command());
    finishAnswer({clicked: true, browserMonotonicMs: 10});
    await vi.advanceTimersByTimeAsync(0);
    await participant.dispatch(command("pause", {commandId: "pause"}));
    participant.observe({kind: "answer", phase: "sending", round_number: 1, monotonic_ms: 150, wall_time_ms: 999999});
    participant.observe({...display(1), monotonic_ms: 180, wall_time_ms: 1, answer_notice_visible: true});
    expect(records.find(record => record.event === "answer_send_to_accepted_dom")?.durationMs).toBe(30);
    await vi.advanceTimersByTimeAsync(1000);
    expect(participant.status()).toMatchObject({accepted: [1], paused: true, timeouts: 0, complete: false});
    await participant.stop();
  });

  it("does not start a receipt timeout if acceptance arrives before browser click completion", async () => {
    const {participant, finishAnswer} = fixture();
    await participant.dispatch(command());
    participant.observe({kind: "answer_dom", status: "accepted", visible: true, round_number: 1,
      monotonic_ms: 20, wall_time_ms: Date.now()});
    finishAnswer({clicked: true, browserMonotonicMs: 10});
    await vi.advanceTimersByTimeAsync(10000);
    expect(participant.status()).toMatchObject({clicked: [1], accepted: [1], timeouts: 0, failed: false});
    await participant.stop();
  });

  it("stop settles pending browser reservations, rejects wrong runs, and enforces the execution deadline", async () => {
    const {participant, driver, records} = fixture();
    await participant.dispatch(command("answer", {runId: "other-run"}));
    expect(records.at(-1)?.reason).toBe("run_mismatch");
    await participant.dispatch(command());
    expect(driver.answer).toHaveBeenCalledTimes(1);
    await participant.dispatch(command("stop", {commandId: "stop"}));
    expect(driver.pause).toHaveBeenLastCalledWith(true, true);
    participant.observe(display(1));
    await vi.advanceTimersByTimeAsync(200000);
    await participant.dispatch(command("answer", {commandId: "post-stop"}));
    expect(driver.click).not.toHaveBeenCalled();
    expect(participant.status()).toMatchObject({stopped: true, pending: 0, clicked: [], timeouts: 0});
    expect(records.at(-1)?.reason).toBe("run_stopped");
    expect(records).toContainEqual(expect.objectContaining({event: "manual_observation_interrupted", count: 1}));
    const next = fixture();
    await vi.advanceTimersByTimeAsync(180001);
    await next.participant.dispatch(command());
    expect(next.records.at(-1)?.reason).toBe("run_stopped");
    expect(next.driver.answer).not.toHaveBeenCalled();
    await next.participant.stop();
  });

  it("spreads ordinals over the global target instead of restarting at each worker", () => {
    const global = command("answer", {target: {ordinalStart: 1, count: 650}, spreadMs: 64900});
    if (global.kind !== "answer") throw new Error("test_command_invalid");
    expect(spreadOffset(global, 1)).toBe(0);
    expect(spreadOffset(global, 101)).toBe(10000);
    expect(spreadOffset(global, 650)).toBe(64900);
    expect(() => spreadOffset(global, 651)).toThrow("command_target_mismatch");
    expect(manualCommandSchema.safeParse({...global, target: {ordinalStart: 17576, count: 2}}).success).toBe(false);
  });
});
