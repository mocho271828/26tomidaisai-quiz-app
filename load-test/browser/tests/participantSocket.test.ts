import type { Browser } from "playwright";
import { expect, it, vi } from "vitest";
import { createActor } from "../src/participant.js";
import type { Evidence, SafeEvent } from "../src/evidence.js";
import { configSchema } from "../src/config.js";

it("does not clear the current connection for a delayed obsolete socket close", async () => {
  const handlers = new Map<string, (value: unknown) => void>();
  const page = {setDefaultTimeout: vi.fn(), exposeBinding: vi.fn(), addInitScript: vi.fn(),
    on: (event: string, listener: (value: unknown) => void) => { handlers.set(event, listener); }};
  const browser = {newContext: async () => ({newPage: async () => page})} as unknown as Browser;
  const events: SafeEvent[] = [];
  const evidence = {event: (record: SafeEvent) => events.push(record)} as unknown as Evidence;
  const config = configSchema.parse({runId: "socket-generation", sourceCommit: "0".repeat(40),
    baseUrl: "https://127.0.0.1", count: 1, ordinalStart: 1, startAt: "2026-10-02T00:00:00Z",
    durationSeconds: 180, joinSpreadMs: 0, operatorStartDelaySeconds: 10, preScheduleMarginMs: 1000,
    resultHoldMs: 3000, answerDelayMs: 0, scenario: "normal", faultAfterSeconds: 5,
    faultDurationMs: 100, outputDirectory: "/tmp/socket-generation", channel: "chromium", headless: true});
  const actor = await createActor(browser, 1, config, evidence, undefined, {detailedDiagnostics: false});
  const socket = (): {on: (event: string, listener: () => void) => void; close: () => void} => {
    let close = (): void => undefined;
    return {on: (_event, listener) => { close = listener; }, close: () => close()};
  };
  const old = socket();
  const current = socket();
  handlers.get("websocket")!(old);
  handlers.get("websocket")!(current);
  actor.joined = true; actor.connected = true;
  old.close();
  expect(actor).toMatchObject({connected: true, failed: false, wsCloses: 0});
  expect(events.at(-1)?.event).toBe("ws_obsolete_closed");
  current.close();
  expect(actor).toMatchObject({connected: false, failed: true, wsCloses: 1});
  expect(events.at(-1)?.event).toBe("ws_disconnected");
});
