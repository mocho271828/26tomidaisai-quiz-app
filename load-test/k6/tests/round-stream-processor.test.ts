import { describe, expect, it } from "vitest";

import { canonicalJsonBytes } from "../src/canonical-json.js";
import {
  GeneratorRoundEvidenceSchema,
  RoundAggregationRefusal,
} from "../src/round-aggregator.js";
import {
  GeneratorRoundStreamProcessor,
  RoundStreamRefusal,
  canonicalAwareDatetime,
} from "../src/round-stream-processor.js";
import { GeneratorEventRefusal } from "../src/events.js";
import {
  buildEvent,
  buildRoundEvents,
  encodeEventLine,
  loadedManifest,
  roundStartMilliseconds,
  sealTime,
} from "./generator-event-fixtures.js";

function expectRefusal(action: () => unknown, code: string): void {
  expect(action).toThrow(code);
}

function concatenate(chunks: readonly Uint8Array[]): Uint8Array {
  const size = chunks.reduce((total, chunk) => total + chunk.byteLength, 0);
  const result = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return result;
}

describe("GeneratorRoundStreamProcessor", () => {
  it("Date clockをPydantic canonical aware datetimeへ0ms/nonzero両方変換する", () => {
    expect(canonicalAwareDatetime(new Date("2026-09-03T01:15:00.000Z"))).toBe(
      "2026-09-03T01:15:00Z",
    );
    expect(canonicalAwareDatetime(new Date("2026-09-03T01:15:00.123Z"))).toBe(
      "2026-09-03T01:15:00.123000Z",
    );
  });

  it("nonzero millisecond clockも6桁fractionでevidenceへsealする", () => {
    const loaded = loadedManifest();
    const processor = new GeneratorRoundStreamProcessor(loaded, {
      clock: (roundNumber) =>
        new Date(roundStartMilliseconds(loaded.manifest, roundNumber) + 3_123),
    });
    const roundChunk = concatenate(
      buildRoundEvents(loaded.manifest, 1).map((event) => encodeEventLine(event)),
    );

    const [artifact] = processor.push(roundChunk);
    if (artifact === undefined) {
      throw new Error("fixture_artifact_missing");
    }
    const evidence = GeneratorRoundEvidenceSchema.parse(
      JSON.parse(new TextDecoder().decode(artifact.body)) as unknown,
    );
    expect(evidence.sealed_at).toMatch(/\.123000Z$/);
  });

  it("600件到達時にEOFを待たずcanonical JSON+LFを固定名で返す", () => {
    const loaded = loadedManifest();
    const processor = new GeneratorRoundStreamProcessor(loaded, {
      clock: (roundNumber) => new Date(sealTime(loaded.manifest, roundNumber)),
    });
    const stream = concatenate(
      buildRoundEvents(loaded.manifest, 1).map((event) => encodeEventLine(event)),
    );
    const split = Math.floor(stream.byteLength / 2) + 7;

    expect(processor.push(stream.subarray(0, split))).toEqual([]);
    const completed = processor.push(stream.subarray(split));

    expect(completed).toHaveLength(1);
    expect(completed[0]?.roundNumber).toBe(1);
    expect(completed[0]?.name).toBe("generator-round-01.json");
    const body = completed[0]?.body;
    if (body === undefined) {
      throw new Error("fixture_artifact_missing");
    }
    expect(body.at(-1)).toBe(0x0a);
    const evidence = GeneratorRoundEvidenceSchema.parse(
      JSON.parse(new TextDecoder().decode(body)) as unknown,
    );
    const canonical = canonicalJsonBytes(evidence);
    expect(body).toEqual(concatenate([canonical, Uint8Array.of(0x0a)]));
    expectRefusal(() => processor.finish(), "round_stream_incomplete");
  });

  it("10 Roundを順次完結し、各Roundを1回だけ返す", () => {
    const loaded = loadedManifest();
    const processor = new GeneratorRoundStreamProcessor(loaded, {
      clock: (roundNumber) => new Date(sealTime(loaded.manifest, roundNumber)),
    });
    const completedNames: string[] = [];

    for (let roundNumber = 1; roundNumber <= 10; roundNumber += 1) {
      for (const event of buildRoundEvents(loaded.manifest, roundNumber)) {
        const completed = processor.push(encodeEventLine(event));
        completedNames.push(...completed.map((artifact) => artifact.name));
      }
    }

    expect(completedNames).toEqual(
      Array.from(
        { length: 10 },
        (_, index) => `generator-round-${String(index + 1).padStart(2, "0")}.json`,
      ),
    );
    expect(processor.finish()).toEqual([]);
    expectRefusal(
      () => processor.push(encodeEventLine(buildEvent(loaded.manifest, 10, 1))),
      "round_stream_sequence_invalid",
    );
  });

  it("次Round先行、重複、partial EOF、invalid clockを拒否する", () => {
    const loaded = loadedManifest();
    const ahead = new GeneratorRoundStreamProcessor(loaded, {
      clock: (roundNumber) => new Date(sealTime(loaded.manifest, roundNumber)),
    });
    expectRefusal(
      () => ahead.push(encodeEventLine(buildEvent(loaded.manifest, 2, 1))),
      "round_stream_sequence_invalid",
    );

    const duplicate = new GeneratorRoundStreamProcessor(loaded);
    const first = encodeEventLine(buildEvent(loaded.manifest, 1, 1));
    duplicate.push(first);
    expectRefusal(() => duplicate.push(first), "round_aggregation_event_duplicate");

    const partial = new GeneratorRoundStreamProcessor(loaded);
    partial.push(first.subarray(0, first.byteLength - 1));
    expectRefusal(() => partial.finish(), "event_eof_partial");

    const badClock = new GeneratorRoundStreamProcessor(loaded, {
      clock: () => new Date(Number.NaN),
    });
    const firstRound = concatenate(
      buildRoundEvents(loaded.manifest, 1).map((event) => encodeEventLine(event)),
    );
    expectRefusal(() => badClock.push(firstRound), "round_stream_clock_invalid");
  });

  it("prefix eventの11個目Round相当を6000件上限で拒否する", () => {
    const loaded = loadedManifest();
    const processor = new GeneratorRoundStreamProcessor(loaded, {
      clock: (roundNumber) => new Date(sealTime(loaded.manifest, roundNumber)),
    });

    for (let roundNumber = 1; roundNumber <= 10; roundNumber += 1) {
      const roundChunk = concatenate(
        buildRoundEvents(loaded.manifest, roundNumber).map((event) => encodeEventLine(event)),
      );
      processor.push(roundChunk);
    }

    expectRefusal(
      () => processor.push(encodeEventLine(buildEvent(loaded.manifest, 10, 1))),
      "event_count_exceeded",
    );
  });

  it("例外messageへeventや秘密候補を含めない", () => {
    const loaded = loadedManifest();
    const processor = new GeneratorRoundStreamProcessor(loaded);
    const secret = "do-not-log-this-token";
    let captured: unknown;
    try {
      processor.push(
        new TextEncoder().encode(
          `@@LOAD_TEST_EVENT@@${JSON.stringify({ format: "wrong", token: secret })}\n`,
        ),
      );
    } catch (error: unknown) {
      captured = error;
    }

    expect(captured).toBeInstanceOf(Error);
    expect(String(captured)).not.toContain(secret);
    expect(
      captured instanceof GeneratorEventRefusal ||
        captured instanceof RoundAggregationRefusal ||
        captured instanceof RoundStreamRefusal,
    ).toBe(true);
  });
});
