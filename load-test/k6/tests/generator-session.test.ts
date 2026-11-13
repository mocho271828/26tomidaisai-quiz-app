import { describe, expect, it } from "vitest";

import type {
  AnswerReceipt,
  AnswerStatus,
  ParticipantState,
  ReadinessAcknowledgement,
  ReadinessRequest,
  SubmitAnswerRequest,
} from "../src/generator/api-contracts.js";
import { planGeneratorVirtualUser } from "../src/generator/planner-core.js";
import {
  loadK6RuntimeManifest,
  type K6RuntimeManifestBinding,
} from "../src/generator/runtime-manifest.js";
import { GeneratorVirtualSession } from "../src/generator/session.js";
import type {
  GeneratorEventSink,
  GeneratorHttpResult,
  GeneratorParticipantTransport,
  GeneratorRoundAssetRequest,
  GeneratorVuRoundEventValue,
} from "../src/generator/session-contracts.js";
import {
  sha256Text,
  validRuntimeManifestFixture,
} from "./fixtures/runtime-manifest.js";

const CLIENT_ANSWER_ID = "77777777-7777-5777-8777-777777777777";
const CLIENT_JOIN_TOKEN = "j".repeat(43);
const runtimeFixture = validRuntimeManifestFixture();
const binding = loadK6RuntimeManifest(
  runtimeFixture.raw,
  runtimeFixture.environment,
  sha256Text,
);
const scheduledStartMs = Date.parse(binding.manifest.scheduled_start_at);

interface TimedCall<Request> {
  readonly at: number;
  readonly request: Request;
}

interface JoinRequest {
  readonly clientJoinToken: string;
  readonly publicLabel: string;
}

class FakeTransport implements GeneratorParticipantTransport {
  now = scheduledStartMs;
  projection: ParticipantState;
  readonly operationLog: string[] = [];
  readonly joinRequests: JoinRequest[] = [];
  readonly heartbeats: number[] = [];
  readonly refreshes: number[] = [];
  readonly readiness: TimedCall<{
    readonly roundId: string;
    readonly request: ReadinessRequest;
  }>[] = [];
  readonly submissions: TimedCall<{
    readonly roundId: string;
    readonly request: SubmitAnswerRequest;
  }>[] = [];
  readonly statusQueries: TimedCall<string>[] = [];
  readonly assetLoads: GeneratorRoundAssetRequest[] = [];
  readonly assetLoadResults: boolean[] = [];
  readonly refreshQueue: ParticipantState[] = [];
  readonly readinessQueue: ReadinessAcknowledgement[] = [];
  readonly statusQueue: AnswerStatus[] = [];
  joinFailuresRemaining = 0;
  answerId = "88888888-8888-4888-8888-888888888888";
  answerReceivedAt = "2026-09-03T01:15:00Z";

  constructor(projection: ParticipantState) {
    this.projection = projection;
  }

  checkSession(): GeneratorHttpResult<ParticipantState> {
    this.operationLog.push("check");
    return knownFailure(this.now, "client");
  }

  join(
    clientJoinToken: string,
    publicLabel: string,
  ): GeneratorHttpResult<ParticipantState> {
    this.operationLog.push("join");
    this.joinRequests.push({ clientJoinToken, publicLabel });
    if (this.joinFailuresRemaining > 0) {
      this.joinFailuresRemaining -= 1;
      return knownFailure(this.now, "server");
    }
    return success(this.projection, this.now);
  }

  heartbeat(): GeneratorHttpResult<ParticipantState> {
    this.operationLog.push("heartbeat");
    this.heartbeats.push(this.now);
    return success(this.projection, this.now);
  }

  refreshProjection(): GeneratorHttpResult<ParticipantState> {
    this.operationLog.push("refresh");
    this.refreshes.push(this.now);
    this.projection = this.refreshQueue.shift() ?? this.projection;
    return success(this.projection, this.now);
  }

  recordReadiness(
    roundId: string,
    request: ReadinessRequest,
  ): GeneratorHttpResult<ReadinessAcknowledgement> {
    this.operationLog.push("readiness");
    this.readiness.push({ at: this.now, request: { roundId, request } });
    const acknowledgement = this.readinessQueue.shift();
    if (acknowledgement === undefined) {
      throw new Error("test_readiness_response_missing");
    }
    return success(acknowledgement, this.now);
  }

  submitAnswer(
    roundId: string,
    request: SubmitAnswerRequest,
  ): GeneratorHttpResult<AnswerReceipt> {
    this.operationLog.push("answer");
    this.submissions.push({ at: this.now, request: { roundId, request } });
    return success(
      {
        answer_id: this.answerId,
        round_id: roundId,
        option_id: request.option_id,
        client_answer_id: request.client_answer_id,
        received_at: this.answerReceivedAt,
        is_replay: this.submissions.length > 1,
      },
      this.now,
    );
  }

  getAnswerStatus(clientAnswerId: string): GeneratorHttpResult<AnswerStatus> {
    this.operationLog.push("status");
    this.statusQueries.push({ at: this.now, request: clientAnswerId });
    const status = this.statusQueue.shift();
    if (status === undefined) {
      throw new Error("test_status_response_missing");
    }
    return success(status, this.now);
  }

  getServerTime() {
    this.operationLog.push("time");
    return success({ server_time: new Date(this.now).toISOString() }, this.now);
  }

  loadRoundAssets(request: GeneratorRoundAssetRequest): boolean {
    this.operationLog.push("assets");
    this.assetLoads.push(request);
    return this.assetLoadResults.shift() ?? true;
  }
}

class CapturingEventSink implements GeneratorEventSink {
  readonly events: GeneratorVuRoundEventValue[] = [];

  emit(event: GeneratorVuRoundEventValue): void {
    this.events.push(event);
  }
}

describe("generator virtual session", () => {
  it("initial GET後に同じtokenでjoinを再試行し、readiness version競合を吸収する", () => {
    const initial = roundProjection(1, "PreparingAssets", 1, null, null);
    const readyVersion2 = roundProjection(1, "ReadyToSchedule", 2, null, null);
    const openVersion2 = roundProjection(1, "Open", 2);
    const transport = new FakeTransport(initial);
    transport.joinFailuresRemaining = 1;
    transport.refreshQueue.push(readyVersion2, openVersion2);
    transport.readinessQueue.push(
      acknowledgement(initial, 2, "ReadyToSchedule"),
      acknowledgement(readyVersion2, 2, "ReadyToSchedule"),
    );
    const session = createSession(1, transport);
    const startAt = scheduledStartMs + 1_500;

    transport.now = startAt;
    session.start(startAt);
    expect(transport.operationLog.slice(0, 5)).toEqual(["time", "time", "time", "check", "join"]);
    expect(transport.joinRequests).toEqual([
      { clientJoinToken: CLIENT_JOIN_TOKEN, publicLabel: "AAA" },
    ]);

    transport.now = startAt + 999;
    session.tick(transport.now);
    expect(transport.joinRequests).toHaveLength(1);
    transport.now = startAt + 1_000;
    session.tick(transport.now);

    expect(transport.joinRequests).toEqual([
      { clientJoinToken: CLIENT_JOIN_TOKEN, publicLabel: "AAA" },
      { clientJoinToken: CLIENT_JOIN_TOKEN, publicLabel: "AAA" },
    ]);
    expect(transport.readiness.map(({ request }) => request.request.round_version)).toEqual([1, 2]);
    expect(transport.readiness.map(({ request }) => request.request.has_asset_failure)).toEqual([
      false,
      false,
    ]);
    expect(transport.assetLoads).toEqual([
      {
        roundNumber: 1,
        questionId: binding.manifest.questions[0]?.id,
      },
      {
        roundNumber: 1,
        questionId: binding.manifest.questions[0]?.id,
      },
    ]);
    expect(session.shouldConnectSocket(transport.now)).toBe(true);
  });

  it("join完了の3秒後からheartbeatし、WS close中だけ1秒poll・5秒後reconnectする", () => {
    const transport = new FakeTransport(roundProjection(1, "Open", 1));
    const session = createSession(1, transport);
    const joinedAt = scheduledStartMs + 2_000;
    transport.now = joinedAt;
    session.start(joinedAt);

    transport.now = joinedAt + 2_999;
    session.tick(transport.now);
    expect(transport.heartbeats).toEqual([]);
    transport.now = joinedAt + 3_000;
    session.tick(transport.now);
    expect(transport.heartbeats).toEqual([joinedAt + 3_000]);

    session.markSocketConnecting();
    session.onSocketOpen(transport.now);
    expect(session.isSocketOpen).toBe(true);
    expect(session.isPolling).toBe(false);
    expect(
      session.onRealtimeMessage(
        {
          type: "polling.required",
          reason: "realtime_unavailable",
          poll_after_ms: 1_000,
        },
        transport.now,
      ),
    ).toBe(false);
    expect(session.isPolling).toBe(true);
    const pollingStartedAt = transport.now;
    const refreshesBeforeClose = transport.refreshes.length;

    transport.now += 10;
    session.onSocketClosed(transport.now);
    expect(session.isPolling).toBe(true);
    expect(transport.refreshes).toHaveLength(refreshesBeforeClose);
    expect(session.shouldConnectSocket(transport.now + 4_999)).toBe(false);
    expect(session.shouldConnectSocket(transport.now + 5_000)).toBe(true);

    const refreshesAfterClose = transport.refreshes.length;
    transport.now = pollingStartedAt + 999;
    session.tick(transport.now);
    expect(transport.refreshes).toHaveLength(refreshesAfterClose);
    transport.now = pollingStartedAt + 1_000;
    session.tick(transport.now);
    expect(transport.refreshes).toHaveLength(refreshesAfterClose + 1);
  });

  it("planned stop以降はsession lifecycleからHTTP I/Oを行わない", () => {
    const transport = new FakeTransport(roundProjection(1, "Open", 1));
    const plan = focusedPlan(binding, 1);
    const session = createSession(1, transport);
    const stopAt = scheduledStartMs + plan.session_stop_offset_ms;

    transport.now = stopAt - 1;
    session.start(transport.now);
    session.markSocketConnecting();
    const operationsBeforeStop = [...transport.operationLog];

    transport.now = stopAt;
    session.onSocketOpen(transport.now);
    expect(
      session.onRealtimeMessage(
        {
          type: "polling.required",
          reason: "realtime_unavailable",
          poll_after_ms: 1_000,
        },
        transport.now,
      ),
    ).toBe(false);
    session.onSocketProtocolError(transport.now);
    session.onSocketClosed(transport.now);
    session.tick(transport.now);
    session.start(transport.now);

    expect(session.shouldConnectSocket(transport.now)).toBe(false);
    expect(transport.operationLog).toEqual(operationsBeforeStop);
  });

  it("計画時刻を過ぎてもactual [open_at, standard_close_at) 内だけanswerする", () => {
    const roundStart = roundStartAt(1);
    const plannedAnswerAt = roundStart + 100;
    const beforeOpen = answerHarness(1, plannedAnswerAt + 200, plannedAnswerAt + 500);

    advance(beforeOpen, plannedAnswerAt);
    expect(beforeOpen.transport.submissions).toEqual([]);
    advance(beforeOpen, plannedAnswerAt + 200);
    expect(beforeOpen.transport.submissions).toHaveLength(1);
    expect(beforeOpen.transport.submissions[0]?.request.request).toMatchObject({
      round_version: 1,
      client_answer_id: CLIENT_ANSWER_ID,
      option_id: binding.manifest.questions[0]?.options[0]?.id,
    });

    const atClose = answerHarness(1, plannedAnswerAt - 100, plannedAnswerAt);
    advance(atClose, plannedAnswerAt);
    expect(atClose.transport.submissions).toEqual([]);
  });

  it("Round 4で破棄したreceiptをstatusの同一rowで回復しevidenceへ反映する", () => {
    const harness = answerHarness(4);
    const roundId = harness.transport.projection.current_round?.round_id;
    const optionId = binding.manifest.questions[3]?.options[0]?.id;
    if (roundId === undefined || optionId === undefined) {
      throw new Error("test_round_contract_missing");
    }
    harness.transport.statusQueue.push({
      status: "AcceptedLocked",
      answer: {
        answer_id: harness.transport.answerId,
        round_id: roundId,
        option_id: optionId,
        client_answer_id: CLIENT_ANSWER_ID,
        received_at: harness.transport.answerReceivedAt,
      },
    });

    const answerAt = roundStartAt(4) + 100;
    advance(harness, answerAt);
    expect(harness.transport.submissions).toHaveLength(1);
    advance(harness, answerAt + 2_500);
    expect(harness.transport.statusQueries).toEqual([
      { at: answerAt + 2_500, request: CLIENT_ANSWER_ID },
    ]);
    advance(harness, roundStartAt(4) + 8_000);

    expect(harness.sink.events).toHaveLength(1);
    expect(harness.sink.events[0]).toMatchObject({
      round_number: 4,
      answer: {
        expected: true,
        request_started_at_unix_ms: answerAt,
        resolved: true,
        duplicate: false,
        misleading_success: false,
      },
      status_recovery: {
        receipt_discarded: true,
        status_queried: true,
        recovered: true,
      },
    });
  });

  it("Round 3 replayで同じanswer ID・received_atを確認しevidenceへ反映する", () => {
    const harness = answerHarness(3);
    const answerAt = roundStartAt(3) + 100;

    advance(harness, answerAt);
    advance(harness, answerAt + 1_000);
    expect(harness.transport.submissions).toHaveLength(2);
    expect(harness.transport.submissions[1]?.request).toEqual(
      harness.transport.submissions[0]?.request,
    );
    advance(harness, roundStartAt(3) + 8_000);

    expect(harness.sink.events[0]).toMatchObject({
      round_number: 3,
      answer: { resolved: true, duplicate: false, misleading_success: false },
      replay: { attempted: true, same_answer_id: true, row_changed: false },
      status_recovery: {
        receipt_discarded: false,
        status_queried: false,
        recovered: false,
      },
    });
  });

  it("Round 2のcold-cache取得へmanifest固定の問題画像URLを渡す", () => {
    const transport = new FakeTransport(
      roundProjection(2, "PreparingAssets", 1, null, null),
    );
    transport.readinessQueue.push(
      acknowledgement(transport.projection, 1, "ReadyToSchedule"),
    );
    const session = createSession(2, transport);
    const startedAt = roundStartAt(2) - 1_000;

    transport.now = startedAt;
    session.start(startedAt);

    expect(transport.assetLoads).toEqual([
      {
        roundNumber: 2,
        questionId: binding.manifest.questions[1]?.id,
      },
    ]);
  });

  it("Round 2 assetを同一versionでは再取得せずversion更新時に再取得してfailureを更新する", () => {
    const initial = roundProjection(2, "PreparingAssets", 1, null, null);
    const readyVersion2 = roundProjection(2, "ReadyToSchedule", 2, null, null);
    const transport = new FakeTransport(initial);
    transport.assetLoadResults.push(false, true);
    transport.refreshQueue.push(initial, readyVersion2);
    transport.readinessQueue.push(
      acknowledgement(initial, 1, "ReadyToSchedule"),
      acknowledgement(readyVersion2, 2, "ReadyToSchedule"),
    );
    const session = createSession(2, transport);
    const startedAt = roundStartAt(2) - 1_000;

    transport.now = startedAt;
    session.start(startedAt);
    expect(transport.assetLoads).toHaveLength(1);

    session.markSocketConnecting();
    session.onSocketOpen(startedAt);

    expect(transport.assetLoads).toEqual([
      {
        roundNumber: 2,
        questionId: binding.manifest.questions[1]?.id,
      },
      {
        roundNumber: 2,
        questionId: binding.manifest.questions[1]?.id,
      },
    ]);
    expect(transport.readiness.map(({ request }) => request.request)).toEqual([
      { round_version: 1, is_ready: true, has_asset_failure: true },
      { round_version: 2, is_ready: true, has_asset_failure: false },
    ]);
  });
});

function createSession(roundNumber: number, transport: FakeTransport) {
  const sink = new CapturingEventSink();
  return new GeneratorVirtualSession({
    binding,
    plan: focusedPlan(binding, roundNumber),
    clientJoinToken: CLIENT_JOIN_TOKEN,
    transport,
    eventSink: sink,
  });
}

function answerHarness(
  roundNumber: number,
  openAt = roundStartAt(roundNumber),
  standardCloseAt = roundStartAt(roundNumber) + 30_000,
): {
  readonly session: GeneratorVirtualSession;
  readonly transport: FakeTransport;
  readonly sink: CapturingEventSink;
} {
  const transport = new FakeTransport(
    roundProjection(roundNumber, "Open", 1, openAt, standardCloseAt),
  );
  const sink = new CapturingEventSink();
  const session = new GeneratorVirtualSession({
    binding,
    plan: focusedPlan(binding, roundNumber),
    clientJoinToken: CLIENT_JOIN_TOKEN,
    transport,
    eventSink: sink,
  });
  const startAt = roundStartAt(roundNumber) - 1_000;
  transport.now = startAt;
  session.start(startAt);
  return { session, transport, sink };
}

function advance(
  harness: { readonly session: GeneratorVirtualSession; readonly transport: FakeTransport },
  now: number,
): void {
  harness.transport.now = now;
  harness.session.tick(now);
}

function focusedPlan(runtimeBinding: K6RuntimeManifestBinding, roundNumber: number) {
  const plan = planGeneratorVirtualUser(
    runtimeBinding.manifest,
    1,
    () => CLIENT_ANSWER_ID,
  );
  return {
    ...plan,
    rounds: plan.rounds.map((round) =>
      round.round_number === roundNumber
        ? round
        : {
            ...round,
            role: "observer" as const,
            selected_option: null,
            replay_required: false,
            status_discard: {
              enabled: false,
              discard_only_when_status: null,
              query_delay_ms: null,
              retry_only_when_status: null,
            },
            schedule: {
              ...round.schedule,
              answer_send_offset_ms: null,
              answer_send_at_unix_ms: null,
              replay_send_offset_ms: null,
              replay_send_at_unix_ms: null,
              status_query_offset_ms: null,
              status_query_at_unix_ms: null,
            },
          },
    ),
  };
}

function roundProjection(
  roundNumber: number,
  state: "PreparingAssets" | "ReadyToSchedule" | "Open",
  version: number,
  openAt: number | null = roundStartAt(roundNumber),
  standardCloseAt: number | null = roundStartAt(roundNumber) + 30_000,
): ParticipantState {
  const question = binding.manifest.questions[roundNumber - 1];
  if (question === undefined) {
    throw new Error("test_question_missing");
  }
  return {
    projected_at: new Date(scheduledStartMs + roundNumber * 1_000).toISOString(),
    event: {
      event_id: binding.manifest.event.id,
      event_title: "[LOAD-TEST] session",
      event_state: "InProgress",
      event_version: roundNumber,
      start_delay_seconds: 3,
    },
    participant: {
      public_label: "AAA",
      is_ranking_eligible: true,
    },
    latest_result: null,
    current_round: {
      round_id: roundId(roundNumber),
      round_number: roundNumber,
      version,
      state_version: version * 4 + (state === "Open" ? 1 : 0),
      state,
      open_at: openAt === null ? null : new Date(openAt).toISOString(),
      standard_close_at:
        standardCloseAt === null ? null : new Date(standardCloseAt).toISOString(),
      question: {
        question_id: question.id,
        prompt: "[LOAD-TEST] question",


        options: question.options.map((option) => ({
          option_id: option.id,
          label: option.label,
          content: "[LOAD-TEST] option content",
        })),
      },
    },
  };
}

function acknowledgement(
  projection: ParticipantState,
  version: number,
  state: "ReadyToSchedule",
): ReadinessAcknowledgement {
  if (projection.current_round === null) {
    throw new Error("test_round_missing");
  }
  return {
    round_id: projection.current_round.round_id,
    round_version: version,
    round_state: state,
    state_version: version * 4,
  };
}

function roundStartAt(roundNumber: number): number {
  const scenario = binding.manifest.scenario.rounds[roundNumber - 1];
  if (scenario === undefined) {
    throw new Error("test_scenario_missing");
  }
  return scheduledStartMs + scenario.start_offset_seconds * 1_000;
}

function roundId(roundNumber: number): string {
  return `30000000-0000-4000-8000-${String(roundNumber).padStart(12, "0")}`;
}

function success<Value>(value: Value, now: number): GeneratorHttpResult<Value> {
  return { kind: "success", value, startedAtLocalMs: now, endedAtLocalMs: now };
}

function knownFailure<Value>(
  now: number,
  failure: "client" | "server",
): GeneratorHttpResult<Value> {
  return { kind: "known-failure", failure, startedAtLocalMs: now, endedAtLocalMs: now };
}

describe("initial ramp canary and socket evidence", () => {
  it("join/session/WS/readinessを同じVUで確認し、open/close変化だけを通知する", () => {
    const initial = roundProjection(1, "PreparingAssets", 1, null, null);
    const transport = new FakeTransport(initial);
    transport.readinessQueue.push({
      round_id: roundId(1), round_version: 1,
      round_state: "PreparingAssets", state_version: 4,
    });
    const canaries: unknown[] = [];
    const connections: unknown[] = [];
    const session = new GeneratorVirtualSession({
      binding, plan: focusedPlan(binding, 1), clientJoinToken: CLIENT_JOIN_TOKEN, transport,
      eventSink: { emit: () => {}, emitCanary: (event) => { canaries.push(event); },
        emitSessionConnection: (event) => { connections.push(event); } },
    });
    transport.now = scheduledStartMs + 1_500;
    session.start(transport.now);
    expect(canaries).toEqual([]);
    session.markSocketConnecting();
    session.onSocketOpen(transport.now);
    session.tick(transport.now);
    session.tick(transport.now);
    expect(canaries).toHaveLength(1);
    expect(canaries[0]).toMatchObject({ vu_id: 1, join_checked: true, session_checked: true, websocket_checked: true, readiness_checked: true });
    expect(connections).toHaveLength(1);
    expect(connections[0]).toMatchObject({ vu_id: 1, session_checked: true, websocket_connected: true });
    session.onSocketClosed(transport.now + 1);
    expect(connections).toHaveLength(2);
    expect(connections[1]).toMatchObject({ websocket_connected: false });
    expect(transport.joinRequests).toHaveLength(1);
  });

  it("30秒までにWSが成立しなければrunを停止する", () => {
    const initial = roundProjection(1, "PreparingAssets", 1, null, null);
    const transport = new FakeTransport(initial);
    transport.readinessQueue.push({ round_id: roundId(1), round_version: 1, round_state: "PreparingAssets", state_version: 4 });
    const session = new GeneratorVirtualSession({
      binding, plan: focusedPlan(binding, 1), clientJoinToken: CLIENT_JOIN_TOKEN, transport,
      eventSink: { emit: () => {}, emitCanary: () => {} },
    });
    session.start(scheduledStartMs + 1_500);
    expect(() => session.tick(scheduledStartMs + 30_001)).toThrow("generator_canary_deadline_exceeded");
  });
});
