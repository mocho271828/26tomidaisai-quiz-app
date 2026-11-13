import { vi } from "vitest";

import type { RealtimeStateMessage } from "../../api/realtimeContracts";

export type Refresh = () => Promise<boolean>;
export const eventId = "60000000-0000-4000-8000-000000000001";
export const streamId = "60000000-0000-4000-8000-000000000002";
const state = {
  projected_at: "2026-10-01T00:00:00Z",
  event: {
  timing_basis: "client_monotonic_v1", arrival_grace_ms: 2000, clock_tolerance_ms: 500, client_ahead_tolerance_ms: 2500,  event_id: eventId, event_title: "Quiz", event_state: "LobbyOpen", event_version: 1, start_delay_seconds: 3 },
  current_round: null, latest_result: null,
  result_visibility: { distribution: false, personal_ranking: false, overall_ranking: false },
  latest_ranking_snapshot_version: null,
  personal_result_delivery: { status: "none", revision: null },
  award_ceremony: null,
} as const;
export function snapshot(sequence = 1, stream_id = streamId): RealtimeStateMessage {
  return { type: "participant.state", audience: "participant", event_id: eventId, stream_id, sequence, state };
}
export function control(type: "heartbeat" | "session.changed", sequence: number, stream_id = streamId) {
  return { type, audience: "participant", event_id: eventId, stream_id, sequence };
}

export class FakeWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static instances: FakeWebSocket[] = [];

  readonly url: string;
  readyState = FakeWebSocket.CONNECTING;
  private readonly listeners = new Map<string, Set<EventListener>>();

  constructor(url: string | URL) {
    this.url = String(url);
    FakeWebSocket.instances.push(this);
  }

  addEventListener(type: string, listener: EventListener): void {
    const listeners = this.listeners.get(type) ?? new Set<EventListener>();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  close(): void {
    if (this.readyState === FakeWebSocket.CLOSED) {
      return;
    }
    this.readyState = FakeWebSocket.CLOSED;
    this.emit("close", new CloseEvent("close", { code: 1000 }));
  }

  open(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.emit("open", new Event("open"));
  }

  message(value: unknown): void {
    this.emit(
      "message",
      new MessageEvent("message", { data: JSON.stringify(value) }),
    );
  }

  invalidMessage(): void {
    this.emit("message", new MessageEvent("message", { data: "{" }));
  }

  serverClose(code: number): void {
    this.readyState = FakeWebSocket.CLOSED;
    this.emit("close", new CloseEvent("close", { code }));
  }

  private emit(type: string, event: Event): void {
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event);
    }
  }
}

export function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
} {
  let resolvePromise: (value: T) => void = () => undefined;
  const promise = new Promise<T>((resolve) => {
    resolvePromise = resolve;
  });
  return { promise, resolve: resolvePromise };
}

export function changeVisibility(visibility: DocumentVisibilityState): void {
  vi.spyOn(document, "visibilityState", "get").mockReturnValue(visibility);
  document.dispatchEvent(new Event("visibilitychange"));
}
