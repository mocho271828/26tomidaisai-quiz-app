import type { SessionConnection } from "./ramp-readiness.js";
import type { CanaryCompletion } from "./canary.js";
import type {
  AnswerReceipt,
  AnswerStatus,
  ParticipantState,
  ReadinessAcknowledgement,
  ReadinessRequest,
  SubmitAnswerRequest,
  TimeResponse,
} from "./api-contracts.js";
import type { GeneratorCoreRoundPlan, GeneratorCoreVuPlan } from "./planner-core.js";
import type { K6RuntimeManifestBinding } from "./runtime-manifest.js";

export type GeneratorKnownFailureKind = "client" | "server";

export type GeneratorHttpResult<Value> =
  | {
      readonly kind: "success";
      readonly value: Value;
      readonly startedAtLocalMs: number;
      readonly endedAtLocalMs: number;
    }
  | {
      readonly kind: "known-failure";
      readonly failure: GeneratorKnownFailureKind;
      readonly startedAtLocalMs: number;
      readonly endedAtLocalMs: number;
    }
  | {
      readonly kind: "unknown";
      readonly startedAtLocalMs: number;
      readonly endedAtLocalMs: number;
    };

export interface GeneratorParticipantTransport {
  checkSession(): GeneratorHttpResult<ParticipantState>;
  join(clientJoinToken: string, publicLabel: string): GeneratorHttpResult<ParticipantState>;
  heartbeat(): GeneratorHttpResult<ParticipantState>;
  refreshProjection(): GeneratorHttpResult<ParticipantState>;
  recordReadiness(
    roundId: string,
    request: ReadinessRequest,
  ): GeneratorHttpResult<ReadinessAcknowledgement>;
  submitAnswer(
    roundId: string,
    request: SubmitAnswerRequest,
  ): GeneratorHttpResult<AnswerReceipt>;
  getAnswerStatus(clientAnswerId: string): GeneratorHttpResult<AnswerStatus>;
  getServerTime(): GeneratorHttpResult<TimeResponse>;
  loadRoundAssets(request: GeneratorRoundAssetRequest): boolean;
}

export interface GeneratorRoundAssetRequest {
  readonly roundNumber: number;
  readonly questionId: string;
}

export interface GeneratorVuRoundEventValue {
  readonly format: "load-test-generator-vu-round-event-v1";
  readonly run_id: string;
  readonly event_id: string;
  readonly source_commit: string;
  readonly schema_revision: string;
  readonly manifest_sha256: string;
  readonly scenario_sha256: string;
  readonly round_number: number;
  readonly round_id: string;
  readonly question_id: string;
  readonly scenario_condition: string;
  readonly answer_population: string;
  readonly vu_id: number;
  readonly session_established: true;
  readonly answer:
    | {
        readonly expected: true;
        readonly selected_option: "A" | "B" | "C" | "D";
        readonly request_started_at_unix_ms: number | null;
        readonly resolved: boolean;
        readonly duplicate: boolean;
        readonly misleading_success: boolean;
      }
    | {
        readonly expected: false;
        readonly selected_option: null;
        readonly request_started_at_unix_ms: null;
        readonly resolved: false;
        readonly duplicate: false;
        readonly misleading_success: false;
      };
  readonly replay: {
    readonly attempted: boolean;
    readonly same_answer_id: boolean;
    readonly row_changed: boolean;
  };
  readonly status_recovery: {
    readonly receipt_discarded: boolean;
    readonly status_queried: boolean;
    readonly recovered: boolean;
  };
  readonly observed_at: string;
}

export interface GeneratorEventSink {
  emitSessionConnection?(event: SessionConnection): void;
  emitCanary?(event: CanaryCompletion): void;
  emit(event: GeneratorVuRoundEventValue): void;
}

export interface GeneratorSessionOptions {
  readonly binding: K6RuntimeManifestBinding;
  readonly plan: GeneratorCoreVuPlan;
  readonly clientJoinToken: string;
  readonly transport: GeneratorParticipantTransport;
  readonly eventSink: GeneratorEventSink;
}

export interface GeneratorRoundRuntime {
  readonly plan: GeneratorCoreRoundPlan;
  roundId: string | null;
  roundVersion: number | null;
  optionId: string | null;
  readonly readinessVersions: Set<number>;
  nextReadinessAtLocalMs: number;
  answerStartedAtServerMs: number | null;
  answerRoundVersion: number | null;
  answerId: string | null;
  answerReceivedAt: string | null;
  answerResolved: boolean;
  answerDuplicate: boolean;
  misleadingSuccess: boolean;
  initialAttempted: boolean;
  initialOutcomeUnknown: boolean;
  retryAttempted: boolean;
  replayAttempted: boolean;
  replaySameAnswerId: boolean;
  replayRowChanged: boolean;
  receiptDiscarded: boolean;
  statusQueried: boolean;
  statusRecovered: boolean;
  nextUnexpectedStatusAtLocalMs: number | null;
  emitted: boolean;
}

export type GeneratorProtocolRefusalCode =
  | "generator_answer_contract_invalid"
  | "generator_canary_deadline_exceeded"
  | "generator_clock_invalid"
  | "generator_join_contract_invalid"
  | "generator_plan_invalid"
  | "generator_projection_invalid"
  | "generator_realtime_invalid"
  | "generator_session_lost";

/** request/response本文やcookieを保持しない固定codeだけのprotocol失敗。 */
export class GeneratorProtocolRefusal extends Error {
  readonly code: GeneratorProtocolRefusalCode;

  constructor(code: GeneratorProtocolRefusalCode) {
    super(code);
    this.name = "GeneratorProtocolRefusal";
    this.code = code;
  }
}
