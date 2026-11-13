import type {
  AnswerReceipt,
  AnswerStatus,
  ParticipantAnswer,
  ParticipantState,
  SubmitAnswerRequest,
} from "./api-contracts.js";
import {
  chooseParticipantProjection,
  expectedRoundStateVersion,
  optionIdForLabel,
} from "./projection.js";
import type { K6RuntimeManifestBinding } from "./runtime-manifest.js";
import {
  GeneratorProtocolRefusal,
  type GeneratorEventSink,
  type GeneratorHttpResult,
  type GeneratorParticipantTransport,
  type GeneratorRoundRuntime,
  type GeneratorSessionOptions,
  type GeneratorVuRoundEventValue,
} from "./session-contracts.js";

const READINESS_STATES = new Set(["PreparingAssets", "ReadyToSchedule"]);
const READINESS_RETRY_MS = 1_000;
const STATUS_RETRY_MS = 1_000;
const EVIDENCE_OFFSET_MS = 8_000;

interface RoundCoordinatorOptions {
  readonly binding: K6RuntimeManifestBinding;
  readonly plan: GeneratorSessionOptions["plan"];
  readonly transport: GeneratorParticipantTransport;
  readonly eventSink: GeneratorEventSink;
  readonly refreshProjection: (localNowMs: number) => boolean;
}

interface AnswerIdentity {
  readonly roundId: string;
  readonly roundVersion: number;
  readonly request: SubmitAnswerRequest;
}

/** projectionに結び付いたRound単位のreadiness、回答、証跡を管理する。 */
export class GeneratorRoundCoordinator {
  readonly #binding: K6RuntimeManifestBinding;
  readonly #plan: GeneratorSessionOptions["plan"];
  readonly #transport: GeneratorParticipantTransport;
  readonly #eventSink: GeneratorEventSink;
  readonly #refreshProjection: (localNowMs: number) => boolean;
  readonly #rounds: GeneratorRoundRuntime[];
  readonly #assetFailureByRoundVersion = new Map<string, boolean>();
  #projection: ParticipantState | null = null;

  constructor(options: RoundCoordinatorOptions) {
    this.#binding = options.binding;
    this.#plan = options.plan;
    this.#transport = options.transport;
    this.#eventSink = options.eventSink;
    this.#refreshProjection = options.refreshProjection;
    this.#rounds = options.plan.rounds.map((round) => createRoundRuntime(round));
  }

  acceptProjection(projection: ParticipantState, localNowMs: number): void {
    this.#projection = chooseParticipantProjection(
      this.#binding.manifest,
      this.#projection,
      projection,
    );
    this.#bindProjection(localNowMs);
  }

  get hasInitialReadiness(): boolean {
    return (this.#rounds[0]?.readinessVersions.size ?? 0) > 0 &&
      ![...this.#assetFailureByRoundVersion.values()].some(Boolean);
  }

  process(localNowMs: number, serverNowMs: number): void {
    for (const runtime of this.#rounds) {
      this.#startAnswerIfDue(runtime, localNowMs, serverNowMs);
      this.#queryStatusIfDue(runtime, localNowMs, serverNowMs);
      this.#replayIfDue(runtime, serverNowMs);
      if (
        !runtime.emitted &&
        runtime.roundId !== null &&
        serverNowMs >= runtime.plan.schedule.round_start_at_unix_ms + EVIDENCE_OFFSET_MS
      ) {
        this.#emitRound(runtime, serverNowMs);
      }
    }
  }

  #bindProjection(localNowMs: number): void {
    const round = this.#projection?.current_round;
    const question = round?.question;
    if (round === null || round === undefined || question === undefined) {
      return;
    }
    const runtime = this.#rounds.find(
      (candidate) => candidate.plan.question_id === question.question_id,
    );
    if (
      runtime === undefined ||
      (runtime.roundId !== null && runtime.roundId !== round.round_id)
    ) {
      throw new GeneratorProtocolRefusal("generator_projection_invalid");
    }
    runtime.roundId = round.round_id;
    runtime.roundVersion = round.version;
    if (runtime.plan.selected_option !== null) {
      runtime.optionId = optionIdForLabel(round, runtime.plan.selected_option);
    }
    const assetVersionKey = `${round.round_id}:${round.version}`;
    if (!this.#assetFailureByRoundVersion.has(assetVersionKey)) {
      const loaded = this.#transport.loadRoundAssets({
        roundNumber: runtime.plan.round_number,
        questionId: question.question_id,
      });
      this.#assetFailureByRoundVersion.set(assetVersionKey, !loaded);
    }
    this.#acceptProjectedAnswer(runtime, this.#projection?.answer);
    if (READINESS_STATES.has(round.state)) {
      this.#recordReadiness(runtime, localNowMs);
    }
  }

  #recordReadiness(runtime: GeneratorRoundRuntime, localNowMs: number): void {
    const version = runtime.roundVersion;
    if (
      runtime.roundId === null ||
      version === null ||
      runtime.readinessVersions.has(version) ||
      localNowMs < runtime.nextReadinessAtLocalMs
    ) {
      return;
    }
    const result = this.#transport.recordReadiness(runtime.roundId, {
      round_version: version,
      is_ready: true,
      has_asset_failure:
        this.#assetFailureByRoundVersion.get(
          `${runtime.roundId}:${version}`,
        ) ?? true,
    });
    if (result.kind !== "success") {
      runtime.nextReadinessAtLocalMs = result.endedAtLocalMs + READINESS_RETRY_MS;
      this.#refreshProjection(result.endedAtLocalMs);
      return;
    }
    const acknowledgement = result.value;
    if (
      acknowledgement.round_id !== runtime.roundId ||
      acknowledgement.state_version !==
        expectedRoundStateVersion({
          version: acknowledgement.round_version,
          state: acknowledgement.round_state,
        }) ||
      acknowledgement.round_version < version ||
      acknowledgement.round_version > version + 1
    ) {
      throw new GeneratorProtocolRefusal("generator_projection_invalid");
    }
    runtime.readinessVersions.add(version);
    this.#refreshProjection(result.endedAtLocalMs);
  }

  #startAnswerIfDue(
    runtime: GeneratorRoundRuntime,
    localNowMs: number,
    serverNowMs: number,
  ): void {
    const plannedAt = runtime.plan.schedule.answer_send_at_unix_ms;
    if (runtime.initialAttempted || plannedAt === null || serverNowMs < plannedAt) {
      return;
    }
    const identity = this.#answerIdentity(runtime);
    const window = this.#answerWindow(runtime);
    if (
      identity === null ||
      window === null ||
      serverNowMs < window.openAt ||
      serverNowMs >= window.standardCloseAt
    ) {
      return;
    }
    runtime.initialAttempted = true;
    runtime.answerStartedAtServerMs = Math.floor(serverNowMs);
    runtime.answerRoundVersion = identity.roundVersion;
    const result = this.#transport.submitAnswer(identity.roundId, identity.request);
    this.#handleInitialAnswer(runtime, result, localNowMs);
  }

  #handleInitialAnswer(
    runtime: GeneratorRoundRuntime,
    result: GeneratorHttpResult<AnswerReceipt>,
    localNowMs: number,
  ): void {
    if (result.kind === "success" && this.#receiptMatches(runtime, result.value)) {
      if (result.value.is_replay) {
        runtime.misleadingSuccess = true;
      }
      if (runtime.plan.status_discard.enabled) {
        runtime.receiptDiscarded = true;
        runtime.initialOutcomeUnknown = true;
        return;
      }
      this.#acceptAnswer(runtime, result.value);
      this.#refreshProjection(result.endedAtLocalMs);
      return;
    }
    if (result.kind === "success") {
      runtime.misleadingSuccess = true;
    }
    if (result.kind === "known-failure" && result.failure === "client") {
      return;
    }
    runtime.initialOutcomeUnknown = true;
    runtime.nextUnexpectedStatusAtLocalMs = Math.max(localNowMs, result.endedAtLocalMs);
  }

  #queryStatusIfDue(
    runtime: GeneratorRoundRuntime,
    localNowMs: number,
    serverNowMs: number,
  ): void {
    const plannedAt = runtime.plan.schedule.status_query_at_unix_ms;
    const isPlannedDue =
      runtime.plan.status_discard.enabled && plannedAt !== null && serverNowMs >= plannedAt;
    const isUnexpectedDue =
      !runtime.plan.status_discard.enabled &&
      runtime.initialOutcomeUnknown &&
      runtime.nextUnexpectedStatusAtLocalMs !== null &&
      localNowMs >= runtime.nextUnexpectedStatusAtLocalMs;
    if (runtime.statusQueried || (!isPlannedDue && !isUnexpectedDue)) {
      return;
    }

    runtime.statusQueried = runtime.plan.status_discard.enabled;
    const result = this.#transport.getAnswerStatus(runtime.plan.client_answer_id);
    if (result.kind !== "success") {
      runtime.nextUnexpectedStatusAtLocalMs = result.endedAtLocalMs + STATUS_RETRY_MS;
      return;
    }
    const retryResult = this.#handleStatus(runtime, result.value, serverNowMs);
    if (runtime.answerResolved) {
      this.#refreshProjection(retryResult?.endedAtLocalMs ?? result.endedAtLocalMs);
      return;
    }
    runtime.nextUnexpectedStatusAtLocalMs =
      (retryResult?.endedAtLocalMs ?? result.endedAtLocalMs) + STATUS_RETRY_MS;
  }

  #handleStatus(
    runtime: GeneratorRoundRuntime,
    status: AnswerStatus,
    serverNowMs: number,
  ): GeneratorHttpResult<AnswerReceipt> | null {
    if (status.status === "AcceptedLocked") {
      if (!this.#answerMatches(runtime, status.answer)) {
        runtime.misleadingSuccess = true;
        return null;
      }
      this.#acceptAnswer(runtime, status.answer);
      runtime.statusRecovered = runtime.plan.status_discard.enabled;
      return null;
    }
    if (status.client_answer_id !== runtime.plan.client_answer_id) {
      runtime.misleadingSuccess = true;
      return null;
    }
    const identity = this.#answerIdentity(runtime);
    const window = this.#answerWindow(runtime);
    if (
      identity === null ||
      window === null ||
      serverNowMs >= window.standardCloseAt ||
      runtime.retryAttempted
    ) {
      return null;
    }
    runtime.retryAttempted = true;
    const result = this.#transport.submitAnswer(identity.roundId, identity.request);
    if (result.kind === "success" && this.#receiptMatches(runtime, result.value)) {
      this.#acceptAnswer(runtime, result.value);
      runtime.statusRecovered = runtime.plan.status_discard.enabled;
    } else if (result.kind === "success") {
      runtime.misleadingSuccess = true;
    }
    return result;
  }

  #replayIfDue(runtime: GeneratorRoundRuntime, serverNowMs: number): void {
    const replayAt = runtime.plan.schedule.replay_send_at_unix_ms;
    if (
      !runtime.initialAttempted ||
      runtime.replayAttempted ||
      replayAt === null ||
      serverNowMs < replayAt
    ) {
      return;
    }
    const identity = this.#answerIdentity(runtime);
    if (identity === null) {
      return;
    }
    runtime.replayAttempted = true;
    const result = this.#transport.submitAnswer(identity.roundId, identity.request);
    if (result.kind !== "success" || !this.#receiptMatches(runtime, result.value)) {
      runtime.misleadingSuccess = result.kind === "success";
      return;
    }
    runtime.replaySameAnswerId =
      result.value.is_replay &&
      runtime.answerId !== null &&
      result.value.answer_id === runtime.answerId &&
      result.value.received_at === runtime.answerReceivedAt;
    runtime.replayRowChanged =
      runtime.answerId !== null && result.value.answer_id !== runtime.answerId;
    runtime.answerDuplicate ||= runtime.replayRowChanged;
    this.#refreshProjection(result.endedAtLocalMs);
  }

  #emitRound(runtime: GeneratorRoundRuntime, serverNowMs: number): void {
    const roundId = runtime.roundId;
    const scenario = this.#roundScenario(runtime.plan.round_number);
    if (roundId === null) {
      return;
    }
    const event: GeneratorVuRoundEventValue = {
      format: "load-test-generator-vu-round-event-v1",
      run_id: this.#binding.manifest.run_id,
      event_id: this.#binding.manifest.event.id,
      source_commit: this.#binding.manifest.artifacts.source_commit,
      schema_revision: this.#binding.manifest.schema_revision,
      manifest_sha256: this.#binding.manifestSha256,
      scenario_sha256: this.#binding.scenarioSha256,
      round_number: runtime.plan.round_number,
      round_id: roundId,
      question_id: runtime.plan.question_id,
      scenario_condition: scenario.condition,
      answer_population: scenario.answer_population,
      vu_id: this.#plan.vu_id,
      session_established: true,
      answer: answerEvent(runtime),
      replay: {
        attempted: runtime.replayAttempted,
        same_answer_id: runtime.replaySameAnswerId,
        row_changed: runtime.replayRowChanged,
      },
      status_recovery: {
        receipt_discarded: runtime.receiptDiscarded,
        status_queried: runtime.statusQueried,
        recovered: runtime.statusRecovered,
      },
      observed_at: canonicalTimestamp(serverNowMs),
    };
    this.#eventSink.emit(event);
    runtime.emitted = true;
  }

  #answerIdentity(runtime: GeneratorRoundRuntime): AnswerIdentity | null {
    const roundVersion = runtime.answerRoundVersion ?? runtime.roundVersion;
    if (runtime.roundId === null || roundVersion === null || runtime.optionId === null) {
      return null;
    }
    return {
      roundId: runtime.roundId,
      roundVersion,
      request: {
        round_version: roundVersion,
        option_id: runtime.optionId,
        client_answer_id: runtime.plan.client_answer_id,
      },
    };
  }

  #answerWindow(
    runtime: GeneratorRoundRuntime,
  ): { readonly openAt: number; readonly standardCloseAt: number } | null {
    const round = this.#projection?.current_round;
    if (
      round === null ||
      round === undefined ||
      round.round_id !== runtime.roundId ||
      round.open_at === null ||
      round.standard_close_at === null
    ) {
      return null;
    }
    const openAt = Date.parse(round.open_at);
    const standardCloseAt = Date.parse(round.standard_close_at);
    if (!Number.isFinite(openAt) || !Number.isFinite(standardCloseAt)) {
      throw new GeneratorProtocolRefusal("generator_projection_invalid");
    }
    return { openAt, standardCloseAt };
  }

  #receiptMatches(runtime: GeneratorRoundRuntime, receipt: AnswerReceipt): boolean {
    return this.#answerMatches(runtime, receipt);
  }

  #answerMatches(runtime: GeneratorRoundRuntime, answer: ParticipantAnswer): boolean {
    return (
      answer.round_id === runtime.roundId &&
      answer.option_id === runtime.optionId &&
      answer.client_answer_id === runtime.plan.client_answer_id
    );
  }

  #acceptAnswer(runtime: GeneratorRoundRuntime, answer: ParticipantAnswer): void {
    if (runtime.answerId !== null && runtime.answerId !== answer.answer_id) {
      runtime.answerDuplicate = true;
    }
    runtime.answerId = answer.answer_id;
    runtime.answerReceivedAt = answer.received_at;
    runtime.answerResolved = true;
    runtime.initialOutcomeUnknown = false;
  }

  #acceptProjectedAnswer(
    runtime: GeneratorRoundRuntime,
    answer: ParticipantState["answer"],
  ): void {
    if (answer === undefined) {
      return;
    }
    if (answer.round_id !== runtime.roundId || !this.#answerMatches(runtime, answer)) {
      throw new GeneratorProtocolRefusal("generator_projection_invalid");
    }
    this.#acceptAnswer(runtime, answer);
  }

  #roundScenario(roundNumber: number) {
    const scenario = this.#binding.manifest.scenario.rounds[roundNumber - 1];
    if (scenario === undefined || scenario.number !== roundNumber) {
      throw new GeneratorProtocolRefusal("generator_plan_invalid");
    }
    return scenario;
  }
}

function answerEvent(runtime: GeneratorRoundRuntime): GeneratorVuRoundEventValue["answer"] {
  if (runtime.plan.selected_option === null) {
    return {
      expected: false,
      selected_option: null,
      request_started_at_unix_ms: null,
      resolved: false,
      duplicate: false,
      misleading_success: false,
    };
  }
  return {
    expected: true,
    selected_option: runtime.plan.selected_option,
    request_started_at_unix_ms: runtime.answerStartedAtServerMs,
    resolved: runtime.answerResolved,
    duplicate: runtime.answerDuplicate,
    misleading_success: runtime.misleadingSuccess,
  };
}

function createRoundRuntime(
  plan: GeneratorSessionOptions["plan"]["rounds"][number],
): GeneratorRoundRuntime {
  return {
    plan,
    roundId: null,
    roundVersion: null,
    optionId: null,
    readinessVersions: new Set<number>(),
    nextReadinessAtLocalMs: 0,
    answerStartedAtServerMs: null,
    answerRoundVersion: null,
    answerId: null,
    answerReceivedAt: null,
    answerResolved: false,
    answerDuplicate: false,
    misleadingSuccess: false,
    initialAttempted: false,
    initialOutcomeUnknown: false,
    retryAttempted: false,
    replayAttempted: false,
    replaySameAnswerId: false,
    replayRowChanged: false,
    receiptDiscarded: false,
    statusQueried: false,
    statusRecovered: false,
    nextUnexpectedStatusAtLocalMs: null,
    emitted: false,
  };
}

function canonicalTimestamp(milliseconds: number): string {
  return new Date(Math.floor(milliseconds))
    .toISOString()
    .replace(/\.(\d{3})Z$/u, (_match, fraction: string) =>
      fraction === "000" ? "Z" : `.${fraction}000Z`,
    );
}
