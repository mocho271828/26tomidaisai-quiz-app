import { describe, expect, it } from "vitest";

import {
  GENERATOR_EVENT_PREFIX,
  GeneratorEventRefusal,
  GeneratorEventSchema,
  GeneratorEventStreamParser,
  parseGeneratorEventStream,
} from "../src/events.js";
import {
  buildEvent,
  buildManifest,
  encodeEventLine,
} from "./generator-event-fixtures.js";

function refusalCode(action: () => unknown): string {
  try {
    action();
  } catch (error: unknown) {
    expect(error).toBeInstanceOf(GeneratorEventRefusal);
    if (error instanceof GeneratorEventRefusal) {
      return error.code;
    }
  }
  throw new Error("expected_generator_event_refusal");
}

describe("GeneratorEventSchema", () => {
  it("strictなdiscriminated eventだけを受理する", () => {
    const manifest = buildManifest();
    const answering = buildEvent(manifest, 1, 1);
    const observing = buildEvent(manifest, 1, 598);

    expect(GeneratorEventSchema.parse(answering)).toEqual(answering);
    expect(GeneratorEventSchema.parse(observing)).toEqual(observing);
    expect(
      GeneratorEventSchema.safeParse({
        ...answering,
        format: "load-test-generator-vu-round-event-v2",
      }).success,
    ).toBe(false);
    expect(
      GeneratorEventSchema.safeParse({
        ...answering,
        source_commit: `${answering.source_commit}\n`,
      }).success,
    ).toBe(false);
    expect(
      GeneratorEventSchema.safeParse({
        ...answering,
        manifest_sha256: `${answering.manifest_sha256}\n`,
      }).success,
    ).toBe(false);
  });

  it.each([
    "authorization",
    "cookie",
    "join_token",
    "participant_id",
    "public_label",
    "client_answer_id",
    "answer_id",
    "raw_http_response",
  ])("top-levelの秘密・raw identity field `%s` を拒否する", (field) => {
    const event = buildEvent(buildManifest(), 1, 1);

    expect(
      GeneratorEventSchema.safeParse({ ...event, [field]: "must-not-survive" }).success,
    ).toBe(false);
  });

  it("nested objectの秘密・extra fieldも拒否する", () => {
    const event = buildEvent(buildManifest(), 1, 1);

    expect(
      GeneratorEventSchema.safeParse({
        ...event,
        answer: { ...event.answer, client_answer_id: "must-not-survive" },
      }).success,
    ).toBe(false);
    expect(
      GeneratorEventSchema.safeParse({
        ...event,
        replay: { ...event.replay, answer_id: "must-not-survive" },
      }).success,
    ).toBe(false);
    expect(
      GeneratorEventSchema.safeParse({
        ...event,
        status_recovery: { ...event.status_recovery, response_body: "must-not-survive" },
      }).success,
    ).toBe(false);
  });

  it("answer/replay/statusの因果関係をfail-closedにする", () => {
    const manifest = buildManifest();
    const answering = buildEvent(manifest, 1, 1);
    const observing = buildEvent(manifest, 1, 598);

    expect(
      GeneratorEventSchema.safeParse({
        ...answering,
        answer: {
          ...answering.answer,
          request_started_at_unix_ms: null,
          resolved: true,
        },
      }).success,
    ).toBe(false);
    expect(
      GeneratorEventSchema.safeParse({
        ...answering,
        replay: { attempted: false, same_answer_id: true, row_changed: false },
      }).success,
    ).toBe(false);
    expect(
      GeneratorEventSchema.safeParse({
        ...answering,
        status_recovery: {
          receipt_discarded: true,
          status_queried: false,
          recovered: true,
        },
      }).success,
    ).toBe(false);
    expect(
      GeneratorEventSchema.safeParse({
        ...observing,
        status_recovery: {
          receipt_discarded: false,
          status_queried: true,
          recovered: true,
        },
      }).success,
    ).toBe(false);
  });
});

describe("GeneratorEventStreamParser", () => {
  it("chunk境界とmultibyte logに依存せずprefix eventだけを返す", () => {
    const event = buildEvent(buildManifest(), 1, 1);
    const nonEvent = new TextEncoder().encode("秘密を含み得るk6ログです\n");
    const eventLine = encodeEventLine(event);
    const bytes = new Uint8Array(nonEvent.byteLength + eventLine.byteLength);
    bytes.set(nonEvent);
    bytes.set(eventLine, nonEvent.byteLength);
    const parser = new GeneratorEventStreamParser();
    const parsed = [];

    for (const byte of bytes) {
      parsed.push(...parser.push(Uint8Array.of(byte)));
    }
    parser.finish();

    expect(parsed).toEqual([event]);
  });

  it("1 chunk内の複数eventを順序どおり返す", () => {
    const manifest = buildManifest();
    const first = buildEvent(manifest, 1, 1);
    const second = buildEvent(manifest, 1, 2);
    const firstLine = encodeEventLine(first);
    const secondLine = encodeEventLine(second);
    const chunk = new Uint8Array(firstLine.byteLength + secondLine.byteLength);
    chunk.set(firstLine);
    chunk.set(secondLine, firstLine.byteLength);
    const parser = new GeneratorEventStreamParser();

    expect(parser.push(chunk)).toEqual([first, second]);
    parser.finish();
  });

  it.each([
    [
      "BOM",
      new Uint8Array([0xef, 0xbb, 0xbf, ...new TextEncoder().encode("log\n")]),
      "event_bom_forbidden",
    ],
    ["NUL", new TextEncoder().encode("log\u0000value\n"), "event_nul_forbidden"],
    ["invalid UTF-8", Uint8Array.of(0xc3, 0x28, 0x0a), "event_utf8_invalid"],
  ])("%s lineを固定codeで拒否する", (_name, bytes, expectedCode) => {
    const parser = new GeneratorEventStreamParser();

    expect(refusalCode(() => parser.push(bytes as Uint8Array))).toBe(expectedCode);
    expect(refusalCode(() => parser.finish())).toBe("event_parser_failed");
  });

  it("blank/whitespaceを通常の非prefix k6 logとして破棄する", () => {
    const parser = new GeneratorEventStreamParser();

    expect(parser.push(new TextEncoder().encode("\n \t\r\n"))).toEqual([]);
    parser.finish();
  });

  it("prefix payloadのinvalid JSONとstrict contract違反を区別して拒否する", () => {
    const malformed = new GeneratorEventStreamParser();
    const invalidContract = new GeneratorEventStreamParser();

    expect(
      refusalCode(() =>
        malformed.push(new TextEncoder().encode(`${GENERATOR_EVENT_PREFIX}{\n`)),
      ),
    ).toBe("event_json_invalid");
    expect(
      refusalCode(() =>
        invalidContract.push(
          new TextEncoder().encode(
            `${GENERATOR_EVENT_PREFIX}${JSON.stringify({ format: "wrong", secret: "hidden" })}\n`,
          ),
        ),
      ),
    ).toBe("event_contract_invalid");
    expect(
      refusalCode(() => invalidContract.finish()),
    ).not.toContain("hidden");
  });

  it("最大byte数をallocation前に適用し、exact boundaryは受理する", () => {
    const exact = new GeneratorEventStreamParser({ maximumLineBytes: 32 });
    const oversized = new GeneratorEventStreamParser({ maximumLineBytes: 32 });

    expect(exact.push(new TextEncoder().encode(`${"x".repeat(32)}\n`))).toEqual([]);
    exact.finish();
    expect(
      refusalCode(() => oversized.push(new TextEncoder().encode("x".repeat(33)))),
    ).toBe("event_line_too_large");
  });

  it("終端LFのないpartial JSONを完全なJSONでも拒否する", () => {
    const event = buildEvent(buildManifest(), 1, 1);
    const parser = new GeneratorEventStreamParser();
    const unterminated = new TextEncoder().encode(
      `${GENERATOR_EVENT_PREFIX}${JSON.stringify(event)}`,
    );

    expect(parser.push(unterminated)).toEqual([]);
    expect(refusalCode(() => parser.finish())).toBe("event_eof_partial");
  });

  it("event数上限とfinished stateを強制する", () => {
    const event = buildEvent(buildManifest(), 1, 1);
    const parser = new GeneratorEventStreamParser({ maximumEventCount: 1 });

    expect(parser.push(encodeEventLine(event))).toEqual([event]);
    expect(refusalCode(() => parser.push(encodeEventLine(event)))).toBe(
      "event_count_exceeded",
    );

    const finished = new GeneratorEventStreamParser();
    finished.finish();
    expect(refusalCode(() => finished.push(new Uint8Array()))).toBe(
      "event_parser_finished",
    );
  });

  it("AsyncIterable adapterもframing errorを握りつぶさない", async () => {
    async function* partialStream(): AsyncGenerator<Uint8Array> {
      yield new TextEncoder().encode("unterminated");
    }

    await expect(parseGeneratorEventStream(partialStream())).rejects.toMatchObject({
      code: "event_eof_partial",
      message: "event_eof_partial",
    });
  });

  it("AsyncIterable由来の例外本文を固定codeへ置換する", async () => {
    async function* failingStream(): AsyncGenerator<Uint8Array> {
      yield new Uint8Array();
      throw new Error("do-not-log-this-stream-secret");
    }

    await expect(parseGeneratorEventStream(failingStream())).rejects.toMatchObject({
      code: "event_stream_failed",
      message: "event_stream_failed",
    });
  });
});
