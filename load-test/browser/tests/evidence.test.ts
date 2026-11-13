import { mkdtemp, readFile, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { z } from "zod";
import { BufferedEventWriter } from "../src/bufferedWriter.js";
import { configSchema } from "../src/config.js";
import { Evidence } from "../src/evidence.js";

afterEach(() => { vi.useRealTimers(); });

describe("bounded asynchronous evidence writer", () => {
  it("preserves order across writes, flushes and an event queued during I/O", async () => {
    let release: (() => void) | undefined;
    const blocked = new Promise<void>(resolve => { release = resolve; });
    const batches: string[] = [];
    const writer = new BufferedEventWriter(async batch => {
      if (batches.length === 0) await blocked;
      batches.push(batch);
    }, {batchMaxBytes: 2});
    writer.enqueue("a\n");
    writer.enqueue("b\n");
    const flushing = writer.flush();
    writer.enqueue("c\n");
    expect(writer.health().writtenRecords).toBe(0);
    release?.();
    await flushing;
    writer.enqueue("d\n");
    await writer.close();
    expect(batches.join("")).toBe("a\nb\nc\nd\n");
    expect(writer.health()).toMatchObject({state: "closed", complete: true,
      writtenRecords: 4, pendingRecords: 0, pendingBytes: 0});
    await writer.close();
  });

  it("counts in-flight records against both byte and record capacities", async () => {
    let release: (() => void) | undefined;
    const blocked = new Promise<void>(resolve => { release = resolve; });
    const writer = new BufferedEventWriter(() => blocked,
      {bufferMaxBytes: 5, bufferMaxRecords: 2, batchMaxBytes: 2});
    expect(writer.enqueue("a\n")).toBe(true);
    expect(writer.enqueue("b\n")).toBe(true);
    expect(writer.enqueue("c")).toBe(false);
    expect(writer.health()).toMatchObject({pendingBytes: 4, pendingRecords: 2, droppedRecords: 1});
    release?.();
    await writer.flush();
    expect(writer.enqueue("abcdef")).toBe(false);
    expect(writer.enqueue("ok\n")).toBe(true);
    await writer.close();
    expect(writer.health()).toMatchObject({complete: false, acceptedRecords: 3,
      writtenRecords: 3, droppedRecords: 2, peakPendingBytes: 4, peakPendingRecords: 2});
    expect(writer.health().firstLossMonotonicMs).not.toBeNull();
  });

  it("reports ambiguous failed batches and queued records without leaking OS error content", async () => {
    const writer = new BufferedEventWriter(async () => { throw new Error("private-file-secret"); });
    writer.enqueue("a\n");
    writer.enqueue("b\n");
    await expect(writer.flush()).rejects.toThrow(/^evidence_write_failed$/u);
    expect(writer.enqueue("c\n")).toBe(false);
    await expect(writer.close()).rejects.toThrow(/^evidence_write_failed$/u);
    expect(writer.health()).toMatchObject({state: "failed", complete: false, failedRecords: 2,
      writtenRecords: 0, droppedRecords: 1, pendingRecords: 0, pendingBytes: 0,
      failure: "evidence_write_failed"});
    expect(JSON.stringify(writer.health())).not.toContain("private-file-secret");
  });

  it("flushes by interval and records queue wait without an unbounded timer backlog", async () => {
    vi.useFakeTimers();
    const batches: string[] = [];
    const writer = new BufferedEventWriter(async batch => { batches.push(batch); }, {flushIntervalMs: 250});
    writer.enqueue("queued\n");
    await vi.advanceTimersByTimeAsync(249);
    expect(batches).toEqual([]);
    await vi.advanceTimersByTimeAsync(1);
    expect(batches).toEqual(["queued\n"]);
    expect(writer.health().queueWaitMaxMs).toBeGreaterThanOrEqual(250);
    await writer.close();
    expect(writer.enqueue("late\n")).toBe(false);
    expect(writer.health().complete).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
  });
});

it("seals actual event order and a separate completion receipt only after close", async () => {
  const directory = await mkdtemp(join(tmpdir(), "quiz-evidence-"));
  const config = configSchema.parse({runId: "evidence-test", sourceCommit: "0".repeat(40),
    baseUrl: "https://127.0.0.1", count: 1, ordinalStart: 1, startAt: new Date().toISOString(),
    durationSeconds: 180, joinSpreadMs: 0, operatorStartDelaySeconds: 10, preScheduleMarginMs: 1000,
    resultHoldMs: 3000, answerDelayMs: 0, scenario: "normal", faultAfterSeconds: 5, faultDurationMs: 100,
    outputDirectory: directory, channel: "chromium", headless: true});
  const evidence = new Evidence(config, "test", {measurementLevel: "minimal"});
  try {
    for (let i = 0; i < 100; i++) evidence.event({event: "example", count: i});
    expect(evidence.count("example")).toBe(100);
    await evidence.flush();
    const rows = z.array(z.object({sequence: z.number(), count: z.number()})).parse(
      (await readFile(join(evidence.directory, "events.jsonl"), "utf8")).trim().split("\n")
        .map(row => JSON.parse(row) as unknown));
    expect(rows.map(row => row.sequence)).toEqual(Array.from({length: 100}, (_, i) => i));
    expect(rows.map(row => row.count)).toEqual(rows.map(row => row.sequence));
    await expect(stat(join(evidence.directory, "evidence-health.json"))).rejects.toMatchObject({code: "ENOENT"});
    await evidence.close();
    const health = JSON.parse(await readFile(join(evidence.directory, "evidence-health.json"), "utf8")) as unknown;
    expect(health).toMatchObject({complete: true, state: "closed", writtenRecords: 100,
      lastWrittenSequence: 99, measurementLevel: "minimal"});
    expect((await stat(join(evidence.directory, "events.jsonl"))).mode & 0o777).toBe(0o600);
    expect(() => new Evidence(config, "test")).toThrow();
  } finally { await evidence.close(); await rm(directory, {recursive: true}); }
});
