import { describe, expect, it, vi } from "vitest";
import { EventEmitter } from "node:events";
import type { Page, Request } from "playwright";
import type { SafeEvent } from "../src/evidence.js";
import { observeOperatorMutations, operatorMutationClass } from "../src/operatorDiagnostics.js";

describe("operator mutation diagnostics", () => {
  it("keeps only fixed route categories", () => {
    expect(operatorMutationClass("https://localhost/api/v1/operator/rounds/secret-id/schedule?token=secret")).toBe("schedule");
    expect(operatorMutationClass("https://localhost/api/v1/operator/rounds/secret-id/publish-results?token=secret")).toBe("publish");
    expect(operatorMutationClass("https://localhost/api/v1/operator/rounds/secret-id/complete-event")).toBe("complete");
    expect(operatorMutationClass("https://localhost/api/v1/operator/result-visibility")).toBe("visibility");
    expect(operatorMutationClass("https://localhost/api/v1/operator/auth/login")).toBeNull();
    expect(operatorMutationClass("invalid-url")).toBeNull();
  });
  it("records body completion, HTTP failure and transport failure without identities", async () => {
    const page = new EventEmitter();
    const records: SafeEvent[] = [];
    const stop = observeOperatorMutations(page as unknown as Page, {event: (record: SafeEvent) => { records.push(record); }});
    const now = vi.spyOn(performance, "now");
    const request = (status: number): Request => ({url: () => "https://localhost/api/v1/operator/rounds/private/schedule?token=secret",
      method: () => "POST", response: async () => ({status: () => status})}) as Request;
    const success = request(200);
    const failed = request(503);
    const aborted = request(0);
    now.mockReturnValue(10);
    for (const value of [success, failed, aborted]) page.emit("request", value);
    now.mockReturnValue(110);
    page.emit("requestfinished", success); page.emit("requestfinished", failed); page.emit("requestfailed", aborted);
    await stop(); now.mockRestore();
    expect(records).toHaveLength(3);
    expect(records).toEqual(expect.arrayContaining([
      expect.objectContaining({route: "schedule", outcome: "success", durationMs: 100, status: 200}),
      expect.objectContaining({route: "schedule", outcome: "http_error", status: 503}),
      expect.objectContaining({route: "schedule", outcome: "transport_error"}),
    ]));
    expect(JSON.stringify(records)).not.toMatch(/private|secret|token/);
    expect(page.listenerCount("request")).toBe(0);
  });
});
