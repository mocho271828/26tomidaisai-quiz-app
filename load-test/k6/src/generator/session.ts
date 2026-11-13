import { CANARY_DEADLINE_MILLISECONDS } from "./canary.js";
import type { ParticipantState, RealtimeServerMessage } from "./api-contracts.js";
import { participantPublicLabelForOrdinal } from "../participant-public-label.js";
import { GeneratorRoundCoordinator } from "./round-executor.js";
import { GeneratorServerClock } from "./server-clock.js";
import {
  GeneratorProtocolRefusal,
  type GeneratorSessionOptions,
} from "./session-contracts.js";

const JOIN_RETRY_MS = 1_000;

type SocketState = "closed" | "connecting" | "open";

/** 1 VUのCookie session、時計、polling、WebSocket lifecycleを管理する。 */
export class GeneratorVirtualSession {
  readonly #binding: GeneratorSessionOptions["binding"];
  readonly #plan: GeneratorSessionOptions["plan"];
  readonly #clientJoinToken: string;
  readonly #publicLabel: string;
  readonly #transport: GeneratorSessionOptions["transport"];
  readonly #clock = new GeneratorServerClock();
  readonly #roundCoordinator: GeneratorRoundCoordinator;
  readonly #eventSink: GeneratorSessionOptions["eventSink"];
  #lastReportedConnection: boolean | undefined;
  #hasReportedCanary = false;
  #hasConnectedSocket = false;
  #hasValidatedSession = false;
  #hasCheckedInitialSession = false;
  #isSessionEstablished = false;
  #isSessionLost = false;
  #socketState: SocketState = "closed";
  #isPolling = true;
  #isPollingRequired = false;
  #nextJoinAtLocalMs = 0;
  #nextHeartbeatAtLocalMs = Number.POSITIVE_INFINITY;
  #nextClockSyncAtLocalMs = 0;
  #nextPollAtLocalMs = 0;
  #nextSocketConnectAtLocalMs = 0;

  constructor(options: GeneratorSessionOptions) {
    this.#binding = options.binding;
    this.#plan = options.plan;
    this.#clientJoinToken = options.clientJoinToken;
    this.#publicLabel = participantPublicLabelForOrdinal(options.plan.vu_id);
    this.#transport = options.transport;
    this.#eventSink = options.eventSink;
    if (
      options.plan.vu_id < 1 ||
      options.plan.vu_id > 600 ||
      options.plan.rounds.length !== 10 ||
      options.clientJoinToken.length !== 43
    ) {
      throw new GeneratorProtocolRefusal("generator_plan_invalid");
    }
    this.#roundCoordinator = new GeneratorRoundCoordinator({
      binding: options.binding,
      plan: options.plan,
      transport: options.transport,
      eventSink: options.eventSink,
      refreshProjection: (localNowMs) => this.#refreshProjection(localNowMs),
    });
  }

  get isPolling(): boolean {
    return this.#isPolling;
  }

  get isSocketOpen(): boolean {
    return this.#socketState === "open";
  }

  start(localNowMs: number): void {
    this.#requireUsable();
    if (this.shouldStop(localNowMs)) {
      return;
    }
    this.#synchronizeClock(localNowMs);
    if (!this.#hasCheckedInitialSession) {
      this.#hasCheckedInitialSession = true;
      const result = this.#transport.checkSession();
      if (result.kind === "success") {
        this.#establishSession(result.value, result.endedAtLocalMs);
        return;
      }
      this.#nextJoinAtLocalMs = result.endedAtLocalMs;
      this.#attemptJoin(Math.max(localNowMs, result.endedAtLocalMs));
    }
  }

  tick(localNowMs: number): void {
    this.#requireUsable();
    this.#reportConnection(localNowMs);
    this.#checkCanary(localNowMs);
    if (this.shouldStop(localNowMs)) {
      return;
    }
    if (!this.#isSessionEstablished) {
      if (localNowMs >= this.#nextJoinAtLocalMs) {
        this.#attemptJoin(localNowMs);
      }
      return;
    }

    const serverNowMs = this.#clock.now(localNowMs);
    if (serverNowMs !== null) {
      // 1秒の回答windowをtimer系HTTPより先に処理する。
      this.#roundCoordinator.process(localNowMs, serverNowMs);
    }
    if (localNowMs >= this.#nextHeartbeatAtLocalMs) {
      this.#heartbeat(localNowMs);
    }
    if (localNowMs >= this.#nextClockSyncAtLocalMs) {
      this.#synchronizeClock(localNowMs);
    }
    if (this.#isPolling && localNowMs >= this.#nextPollAtLocalMs) {
      this.#refreshProjection(localNowMs);
    }
  }

  shouldConnectSocket(localNowMs: number): boolean {
    return (
      this.#isSessionEstablished &&
      this.#socketState === "closed" &&
      !this.shouldStop(localNowMs) &&
      localNowMs >= this.#nextSocketConnectAtLocalMs
    );
  }

  markSocketConnecting(): void {
    this.#requireUsable();
    if (this.#socketState !== "closed") {
      throw new GeneratorProtocolRefusal("generator_realtime_invalid");
    }
    this.#socketState = "connecting";
  }

  onSocketOpen(localNowMs: number): void {
    this.#requireUsable();
    if (this.#socketState !== "connecting") {
      throw new GeneratorProtocolRefusal("generator_realtime_invalid");
    }
    this.#socketState = "open";
    this.#hasConnectedSocket = true;
    if (this.shouldStop(localNowMs)) {
      return;
    }
    this.#isPollingRequired = false;
    this.#refreshProjection(localNowMs);
    this.#reportConnection(localNowMs);
  }

  onRealtimeMessage(message: RealtimeServerMessage, localNowMs: number): boolean {
    this.#requireUsable();
    if (this.shouldStop(localNowMs)) {
      return false;
    }
    if (message.type === "polling.required") {
      // serverの1013 closeを観測するまで、成功したHTTP refreshだけではpollingを止めない。
      this.#isPollingRequired = true;
      this.#startPolling(localNowMs);
      return false;
    }
    if (
      message.audience !== "participant" ||
      message.event_id !== this.#binding.manifest.event.id
    ) {
      this.#isPollingRequired = true;
      this.#startPolling(localNowMs);
      return true;
    }
    this.#refreshProjection(localNowMs);
    return false;
  }

  onSocketProtocolError(localNowMs: number): void {
    if (this.shouldStop(localNowMs)) {
      return;
    }
    this.#isPollingRequired = true;
    this.#startPolling(localNowMs);
  }

  onSocketClosed(localNowMs: number): void {
    this.#socketState = "closed";
    this.#reportConnection(localNowMs);
    if (this.shouldStop(localNowMs)) {
      return;
    }
    this.#nextSocketConnectAtLocalMs =
      localNowMs +
      this.#binding.manifest.scenario.protocol.websocket_reconnect_interval_seconds *
        1_000;
    this.#startPolling(localNowMs);
  }

  shouldStop(localNowMs: number): boolean {
    const stopAt =
      Date.parse(this.#binding.manifest.scheduled_start_at) +
      this.#plan.session_stop_offset_ms;
    return localNowMs >= stopAt;
  }

  #reportConnection(localNowMs: number): void {
    if (!this.#isSessionEstablished || !this.#hasValidatedSession ||
        this.#eventSink.emitSessionConnection === undefined) return;
    const connected = this.#socketState === "open";
    if (connected === this.#lastReportedConnection) return;
    this.#eventSink.emitSessionConnection({
      format: "load-test-generator-session-connection-v1", run_id: this.#binding.manifest.run_id,
      event_id: this.#binding.manifest.event.id, manifest_sha256: this.#binding.manifestSha256,
      vu_id: this.#plan.vu_id, session_checked: true, websocket_connected: connected,
      observed_at_unix_ms: localNowMs,
    });
    this.#lastReportedConnection = connected;
  }

  #checkCanary(localNowMs: number): void {
    if (this.#plan.vu_id > 2 || this.#hasReportedCanary || this.#eventSink.emitCanary === undefined) return;
    const deadline = Date.parse(this.#binding.manifest.scheduled_start_at) + CANARY_DEADLINE_MILLISECONDS;
    if (localNowMs > deadline) throw new GeneratorProtocolRefusal("generator_canary_deadline_exceeded");
    if (!this.#isSessionEstablished || !this.#hasValidatedSession || !this.#hasConnectedSocket ||
        !this.#roundCoordinator.hasInitialReadiness) return;
    this.#eventSink.emitCanary({
      format: "load-test-generator-canary-v1", run_id: this.#binding.manifest.run_id,
      event_id: this.#binding.manifest.event.id, manifest_sha256: this.#binding.manifestSha256,
      vu_id: this.#plan.vu_id as 1 | 2, join_checked: true, session_checked: true,
      readiness_checked: true, websocket_checked: true, observed_at_unix_ms: localNowMs,
    });
    this.#hasReportedCanary = true;
  }

  #attemptJoin(localNowMs: number): void {
    const result = this.#transport.join(this.#clientJoinToken, this.#publicLabel);
    if (result.kind === "success") {
      this.#establishSession(result.value, result.endedAtLocalMs);
      return;
    }
    this.#nextJoinAtLocalMs =
      Math.max(localNowMs, result.endedAtLocalMs) + JOIN_RETRY_MS;
  }

  #establishSession(projection: ParticipantState, localNowMs: number): void {
    if (projection.participant.public_label !== this.#publicLabel) {
      throw new GeneratorProtocolRefusal("generator_join_contract_invalid");
    }
    this.#roundCoordinator.acceptProjection(projection, localNowMs);
    this.#isSessionEstablished = true;
    this.#isPolling = true;
    this.#nextHeartbeatAtLocalMs =
      localNowMs +
      this.#binding.manifest.scenario.protocol.heartbeat_interval_seconds * 1_000;
    this.#nextPollAtLocalMs = localNowMs;
    this.#nextSocketConnectAtLocalMs = localNowMs;
  }

  #heartbeat(localNowMs: number): void {
    const result = this.#transport.heartbeat();
    this.#nextHeartbeatAtLocalMs =
      Math.max(localNowMs, result.endedAtLocalMs) +
      this.#binding.manifest.scenario.protocol.heartbeat_interval_seconds * 1_000;
    if (result.kind === "success") {
      this.#roundCoordinator.acceptProjection(result.value, result.endedAtLocalMs);
      return;
    }
    if (result.kind === "known-failure" && result.failure === "client") {
      this.#isSessionLost = true;
      throw new GeneratorProtocolRefusal("generator_session_lost");
    }
  }

  #synchronizeClock(localNowMs: number): void {
    const protocol = this.#binding.manifest.scenario.protocol;
    const samples = Array.from({ length: protocol.clock_samples_per_sync }, () =>
      this.#transport.getServerTime(),
    );
    const appliedAt = Math.max(
      localNowMs,
      ...samples.map((sample) => sample.endedAtLocalMs),
    );
    this.#clock.apply(samples, appliedAt, protocol.clock_sample_max_rtt_ms);
    this.#nextClockSyncAtLocalMs =
      appliedAt + protocol.clock_sync_interval_seconds * 1_000;
  }

  #refreshProjection(localNowMs: number): boolean {
    const result = this.#transport.refreshProjection();
    if (result.kind === "success") this.#hasValidatedSession = true;
    this.#nextPollAtLocalMs =
      Math.max(localNowMs, result.endedAtLocalMs) +
      this.#binding.manifest.scenario.protocol.polling_interval_seconds * 1_000;
    if (result.kind !== "success") {
      this.#isPolling = true;
      return false;
    }
    this.#roundCoordinator.acceptProjection(result.value, result.endedAtLocalMs);
    if (this.#socketState === "open" && !this.#isPollingRequired) {
      this.#isPolling = false;
    }
    return true;
  }

  #startPolling(localNowMs: number): void {
    if (this.#isPolling) {
      return;
    }
    this.#isPolling = true;
    this.#nextPollAtLocalMs = Math.min(this.#nextPollAtLocalMs, localNowMs);
    this.#refreshProjection(localNowMs);
  }

  #requireUsable(): void {
    if (this.#isSessionLost) {
      throw new GeneratorProtocolRefusal("generator_session_lost");
    }
  }
}
