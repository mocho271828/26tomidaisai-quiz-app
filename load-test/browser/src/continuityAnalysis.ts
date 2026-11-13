import type { AnalysisEvent } from "./analyze.js";
import { analysisWindow, inCohort, type AnalysisWindow } from "./analysisWindow.js";

type Interval = {serial: number; round?: number; startedMs: number; durationMs: number; recovered: boolean};

function interval(serial: number, start: number, end: number, recovered: boolean, round?: number): Interval {
  return {serial, round, startedMs: start, durationMs: Math.max(0, end - start), recovered};
}

function firstQuestion(event: AnalysisEvent, round: number): boolean {
  return event.telemetry?.kind === "projection_received" && event.telemetry.round_number === round && event.telemetry.has_question;
}

function accepted(event: AnalysisEvent, round: number): boolean {
  const telemetry = event.telemetry;
  return event.event === "accepted_dom" && event.round === round ||
    telemetry?.kind === "answer" && telemetry.round_number === round && telemetry.phase === "accepted" ||
    telemetry?.kind === "answer_dom" && telemetry.round_number === round && telemetry.status === "accepted" && telemetry.visible;
}

export function analyzeContinuity(input: AnalysisEvent[], window = analysisWindow(input)): object {
  const cohort = window.cohort;
  if (cohort === null)
    return {available: false, denominator: null, reason: "single_fixed_cohort_missing"};
  const start = window.measurementStartedMs;
  const end = window.measurementFinishedMs;
  const serials = Array.from({length: cohort.count}, (_, index) => cohort.ordinalStart + index);
  const outsideCohortEvents = window.measurement.filter(event => !inCohort(event, window)).length;
  const within = window.measurement.filter(event => event.serial !== undefined && inCohort(event, window));
  const lifecycle = actorLifecycle(serials, input, window);
  if (start === null || end === null) return {
    available: true, denominator: cohort.count, ordinalStart: cohort.ordinalStart,
    measurementAvailable: false, runDurationMs: null, runFinishedObserved: window.finishedObserved,
    lifecycle, neverConnectedParticipants: null, disconnectedParticipants: null,
    connectionIssueParticipants: null, affectedParticipants: null, perRound: [],
    scope: "測定未開始。固定cohortと準備状況だけを記録し、準備待ちを接続障害へ数えない",
  };
  const neverConnected: Interval[] = [];
  const disconnected: Interval[] = [];
  const initialConnectionWait: Interval[] = [];
  for (const serial of serials) {
    let hasConnected = false;
    let disconnectedAt: number | undefined;
    const participant = within.filter(event => event.serial === serial);
    const startedAt = participant.find(event => event.event === "actor_started")?.monotonicMs;
    for (const event of participant) {
      const telemetry = event.telemetry;
      const websocket = telemetry?.kind === "ws" && telemetry.audience === "participant" ? telemetry : undefined;
      if (websocket?.phase === "open" || websocket?.phase === "stable") {
        if (!hasConnected && startedAt !== undefined)
          initialConnectionWait.push(interval(serial, startedAt, event.monotonicMs, true));
        hasConnected = true;
        if (disconnectedAt !== undefined) disconnected.push(interval(serial, disconnectedAt, event.monotonicMs, true));
        disconnectedAt = undefined;
      } else if (hasConnected && (event.event === "ws_disconnected" || websocket?.phase === "closed" || websocket?.phase === "retry")) {
        disconnectedAt ??= event.monotonicMs;
      }
    }
    if (!hasConnected) {
      neverConnected.push(interval(serial, startedAt ?? start, end, false));
      if (startedAt !== undefined) initialConnectionWait.push(interval(serial, startedAt, end, false));
    }
    if (disconnectedAt !== undefined) disconnected.push(interval(serial, disconnectedAt, end, false));
  }
  const timeoutIntervals: Interval[] = [];
  const missingQuestionIntervals: Interval[] = [];
  const rounds = [...new Set(within.filter(event => event.event === "round_expected" && event.round !== undefined)
    .map(event => event.round!))].sort((left, right) => left - right);
  const perRound = rounds.map(round => {
    const earliestExpectation = within.find(event => event.event === "round_expected" && event.round === round)!.monotonicMs;
    const missingQuestion: Interval[] = [];
    const timedOut = new Set<number>();
    const failed = new Set<number>();
    for (const serial of serials) {
      const participant = within.filter(event => event.serial === serial);
      const expected = participant.find(event => event.event === "round_expected" && event.round === round)?.monotonicMs ?? earliestExpectation;
      // 未接続・join失敗でround_expectedが無い端末も、固定の分母から除かない。
      if (!participant.some(event => firstQuestion(event, round))) missingQuestion.push(interval(serial, expected, end, false, round));
      if (participant.some(event => event.event === "actor_failed" && event.round === round)) failed.add(serial);
      let timeoutAt: number | undefined;
      for (const event of participant) {
        if (event.telemetry?.kind === "answer_timeout" && event.telemetry.round_number === round) {
          timedOut.add(serial);
          timeoutAt ??= event.monotonicMs;
        } else if (timeoutAt !== undefined && accepted(event, round)) {
          timeoutIntervals.push(interval(serial, timeoutAt, event.monotonicMs, true, round));
          timeoutAt = undefined;
        }
      }
      if (timeoutAt !== undefined) timeoutIntervals.push(interval(serial, timeoutAt, end, false, round));
    }
    missingQuestionIntervals.push(...missingQuestion);
    return {round, denominator: cohort.count, questionMissingParticipants: missingQuestion.length,
      questionMissingIntervals: missingQuestion, timeoutParticipants: timedOut.size, failedParticipants: failed.size};
  });
  const distinct = (intervals: Interval[]): number => new Set(intervals.map(value => value.serial)).size;
  const failedSerials = within.filter(event => event.event === "actor_failed").map(event => event.serial!);
  const affected = new Set([
    ...neverConnected, ...disconnected, ...timeoutIntervals, ...missingQuestionIntervals,
  ].map(value => value.serial));
  for (const serial of failedSerials) affected.add(serial);
  return {
    available: true, denominator: cohort.count, ordinalStart: cohort.ordinalStart,
    measurementAvailable: true, windowMode: window.mode, lifecycle,
    runDurationMs: end - start, runFinishedObserved: window.finishedObserved, outsideCohortEvents,
    neverConnectedParticipants: neverConnected.length, disconnectedParticipants: distinct(disconnected),
    connectionIssueParticipants: distinct([...(window.mode === "lifecycle-v2"
      ? initialConnectionWait.filter(value => !value.recovered) : neverConnected), ...disconnected]),
    initialConnectionWaitIntervals: window.mode === "lifecycle-v2" ? initialConnectionWait : null,
    initialConnectionPendingParticipants: window.mode === "lifecycle-v2"
      ? initialConnectionWait.filter(value => !value.recovered).length : null,
    timeoutParticipants: distinct(timeoutIntervals),
    questionMissingParticipants: distinct(missingQuestionIntervals),
    failedParticipants: new Set(failedSerials).size,
    // 分類を足し合わせず、複数の障害を経験した同一参加者も全体で1人に数える。
    affectedParticipants: affected.size,
    neverConnectedIntervals: neverConnected, disconnectedIntervals: disconnected, timeoutIntervals, perRound,
    scope: "固定cohort全員が分母。v2は準備・未起動・初回接続待ち・接続後切断を分離。neverConnectedは終端まで未接続の全員であり、準備/未起動者も保持する。Node観測時刻を使用し、未回復区間は測定終端で打ち切る。legacyの準備/未起動は推定しない。正式合否・既存成功条件は変更しない",
  };
}

function actorLifecycle(serials: number[], input: AnalysisEvent[], window: AnalysisWindow): object {
  if (window.mode !== "lifecycle-v2") return {available: false, reason: "legacy_actor_lifecycle_missing"};
  const beforeFinish = [...window.setup, ...window.measurement];
  const runFinish = input.find(event => event.event === "participant_run_finished")?.monotonicMs;
  const end = window.measurementFinishedMs ?? runFinish ?? input.at(-1)?.monotonicMs ?? window.setupStartedMs!;
  const setup: Interval[] = [];
  const pendingLaunch: Interval[] = [];
  let created = 0;
  let started = 0;
  const invalidSerials: number[] = [];
  for (const serial of serials) {
    const actor = beforeFinish.filter(event => event.serial === serial);
    const creations = actor.filter(event => event.event === "actor_created");
    const launches = actor.filter(event => event.event === "actor_started");
    const creation = creations[0];
    const launch = launches[0];
    if (creations.length > 1 || launches.length > 1 || launch !== undefined &&
      (creation === undefined || creation.monotonicMs > launch.monotonicMs)) invalidSerials.push(serial);
    if (creation !== undefined) created += 1;
    if (launch !== undefined) started += 1;
    setup.push(interval(serial, window.setupStartedMs!, creation?.monotonicMs ?? end, creation !== undefined));
    if (window.measurementStartedMs !== null && creation !== undefined)
      pendingLaunch.push(interval(serial, Math.max(window.measurementStartedMs, creation.monotonicMs),
        launch?.monotonicMs ?? end, launch !== undefined));
  }
  return {available: true, createdParticipants: created, setupIncompleteParticipants: serials.length - created,
    startedParticipants: started, notStartedParticipants: serials.length - started,
    createdNotStartedParticipants: pendingLaunch.filter(value => !value.recovered).length,
    invalidParticipants: invalidSerials.length, setupIntervals: setup, pendingLaunchIntervals: pendingLaunch};
}
