import { describe, expect, it, vi } from "vitest";
import { RunLifecycle, cancellableDelay, fixedFailureReason } from "../src/lifecycle.js";
import { followRoundProgress, requestSequence } from "../src/participant.js";
import type { Evidence, SafeEvent } from "../src/evidence.js";

function evidenceRecorder() {
  const events: SafeEvent[] = [];
  const evidence = {event: (event: SafeEvent) => events.push(event)} as unknown as Evidence;
  return {events, evidence};
}

describe("runner lifecycle", () => {
  it("seals measurement once before aborting and records shutdown after cleanup", () => {
    const {evidence, events} = evidenceRecorder();
    const lifecycle = new RunLifecycle(evidence, "participants");
    lifecycle.startMeasurement();
    const atAbort = vi.fn(() => expect(events.at(-1)?.event).toBe("participant_run_finished"));
    lifecycle.signal.addEventListener("abort", atAbort);
    lifecycle.finish("run_failed");
    lifecycle.finish("completed");
    expect(atAbort).toHaveBeenCalledOnce();
    expect(lifecycle.phase).toBe("shutdown");
    lifecycle.shutdownFinished();
    expect(events.map(event => event.event)).toEqual([
      "participant_measurement_started", "participant_run_finished", "participant_shutdown_finished",
    ]);
    expect(events[1]).toMatchObject({phase: "measurement", reason: "run_failed", lifecycleVersion: 2});
  });

  it("cancels long waits immediately without waiting for the original deadline", async () => {
    const controller = new AbortController();
    const wait = cancellableDelay(60_000, controller.signal);
    controller.abort();
    await expect(wait).rejects.toThrow();
  });

  it("keeps errors fixed and does not disclose exception text", () => {
    const error = new Error("Cookie=secret; private participant selector");
    expect(fixedFailureReason(error)).toBe("unclassified_failure");
    error.name = "TimeoutError";
    expect(fixedFailureReason(error)).toBe("timeout");
    const controller = new AbortController();
    controller.abort();
    expect(fixedFailureReason(error, controller.signal)).toBe("runner_cancelled");
  });
});

describe("round progress isolation", () => {
  it("answers all later rounds while an earlier result is still pending", async () => {
    const answers: number[] = [];
    const observations: number[] = [];
    let release = (): void => undefined;
    const firstResult = new Promise<void>(resolve => { release = resolve; });
    const run = followRoundProgress(new AbortController().signal,
      async round => { answers.push(round); }, async round => {
        observations.push(round);
        if (round === 1) await firstResult;
      });
    // 各awaitを進めても第1問の結果は保留したまま、次問への経路だけ進める。
    for (let step = 0; step < 20; step++) await Promise.resolve();
    expect(answers).toEqual([1, 2, 3, 4, 5, 6]);
    expect(observations).toEqual([1, 2, 3, 4, 5, 6]);
    release();
    await run;
  });

  it("does not start remaining questions after cancellation and drains pending observations", async () => {
    const controller = new AbortController();
    const answered: number[] = [];
    const observed: number[] = [];
    let released = false;
    let release = (): void => undefined;
    const pending = new Promise<void>(resolve => { release = () => { released = true; resolve(); }; });
    const run = followRoundProgress(controller.signal, async round => {
      answered.push(round);
      if (round === 2) controller.abort();
    }, async round => { observed.push(round); await pending; });
    for (let step = 0; step < 8; step++) await Promise.resolve();
    expect(answered).toEqual([1, 2]);
    expect(observed).toEqual([1]);
    expect(released).toBe(false);
    release();
    await expect(run).rejects.toThrow();
  });
});

describe("safe request correlation", () => {
  it("records only a valid page-local sequence header", () => {
    expect(requestSequence({headers: () => ({"x-quiz-observation-sequence": "12", cookie: "secret"})})).toBe(12);
    for (const value of ["0", "-1", "1.2", "secret", "100000000000000000", "01"])
      expect(requestSequence({headers: () => ({"x-quiz-observation-sequence": value})})).toBeUndefined();
    expect(requestSequence({headers: () => ({cookie: "secret"})})).toBeUndefined();
  });
});
