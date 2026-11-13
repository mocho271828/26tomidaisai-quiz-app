import { z } from "zod";

import {
  AnswerPopulationSchema,
  AwareDatetimeSchema,
  OptionLabelSchema,
  ScenarioConditionSchema,
} from "./contracts.js";

export const GENERATOR_EVENT_PREFIX = "@@LOAD_TEST_EVENT@@";
export const DEFAULT_MAX_EVENT_LINE_BYTES = 16 * 1024;
export const MAX_EVENT_LINE_BYTES = 64 * 1024;
export const DEFAULT_MAX_EVENT_COUNT = 600 * 10;

const sha256Schema = z.string().regex(/^[0-9a-f]{64}(?![\s\S])/);
const sourceCommitSchema = z.string().regex(/^[0-9a-f]{40}(?![\s\S])/);
const safeIntegerSchema = z.number().refine(Number.isSafeInteger, "safe integer required");
const roundNumberSchema = safeIntegerSchema.refine(
  (value) => value >= 1 && value <= 10,
  "round number out of range",
);
const vuIdSchema = safeIntegerSchema.refine(
  (value) => value >= 1 && value <= 600,
  "VU id out of range",
);
const unixMillisecondsSchema = safeIntegerSchema.refine(
  (value) => value >= 0,
  "unix milliseconds must be non-negative",
);

const expectedAnswerSchema = z
  .strictObject({
    expected: z.literal(true),
    selected_option: OptionLabelSchema,
    request_started_at_unix_ms: z.union([unixMillisecondsSchema, z.null()]),
    resolved: z.boolean(),
    duplicate: z.boolean(),
    misleading_success: z.boolean(),
  })
  .superRefine((answer, context) => {
    if (answer.request_started_at_unix_ms === null && answer.resolved) {
      context.addIssue({
        code: "custom",
        message: "an unstarted answer cannot be resolved",
      });
    }
  });

const unattemptedAnswerSchema = z.strictObject({
  expected: z.literal(false),
  selected_option: z.null(),
  request_started_at_unix_ms: z.null(),
  resolved: z.literal(false),
  duplicate: z.literal(false),
  misleading_success: z.literal(false),
});

export const GeneratorAnswerEventSchema = z.discriminatedUnion("expected", [
  expectedAnswerSchema,
  unattemptedAnswerSchema,
]);
export type GeneratorAnswerEvent = z.infer<typeof GeneratorAnswerEventSchema>;

export const GeneratorReplayEventSchema = z
  .strictObject({
    attempted: z.boolean(),
    same_answer_id: z.boolean(),
    row_changed: z.boolean(),
  })
  .superRefine((replay, context) => {
    if (!replay.attempted && (replay.same_answer_id || replay.row_changed)) {
      context.addIssue({
        code: "custom",
        message: "replay outcome requires an attempt",
      });
    }
  });
export type GeneratorReplayEvent = z.infer<typeof GeneratorReplayEventSchema>;

export const GeneratorStatusRecoveryEventSchema = z
  .strictObject({
    receipt_discarded: z.boolean(),
    status_queried: z.boolean(),
    recovered: z.boolean(),
  })
  .superRefine((status, context) => {
    if (status.recovered && !status.status_queried) {
      context.addIssue({
        code: "custom",
        message: "status recovery requires a status query",
      });
    }
  });
export type GeneratorStatusRecoveryEvent = z.infer<
  typeof GeneratorStatusRecoveryEventSchema
>;

export const GeneratorVuRoundEventSchema = z
  .strictObject({
    format: z.literal("load-test-generator-vu-round-event-v1"),
    run_id: z.uuid(),
    event_id: z.uuid(),
    source_commit: sourceCommitSchema,
    schema_revision: z.string().min(1).max(128),
    manifest_sha256: sha256Schema,
    scenario_sha256: sha256Schema,
    round_number: roundNumberSchema,
    round_id: z.uuid(),
    question_id: z.uuid(),
    scenario_condition: ScenarioConditionSchema,
    answer_population: AnswerPopulationSchema,
    vu_id: vuIdSchema,
    session_established: z.literal(true),
    answer: GeneratorAnswerEventSchema,
    replay: GeneratorReplayEventSchema,
    status_recovery: GeneratorStatusRecoveryEventSchema,
    observed_at: AwareDatetimeSchema,
  })
  .superRefine((event, context) => {
    const hasAnswerSideEffect =
      event.replay.attempted ||
      event.replay.same_answer_id ||
      event.replay.row_changed ||
      event.status_recovery.receipt_discarded ||
      event.status_recovery.status_queried ||
      event.status_recovery.recovered;
    if (!event.answer.expected && hasAnswerSideEffect) {
      context.addIssue({
        code: "custom",
        message: "a non-answering VU cannot report answer side effects",
      });
    }
    if (
      event.answer.expected &&
      event.answer.request_started_at_unix_ms === null &&
      (event.answer.duplicate ||
        event.answer.misleading_success ||
        event.replay.attempted ||
        event.status_recovery.receipt_discarded ||
        event.status_recovery.status_queried ||
        event.status_recovery.recovered)
    ) {
      context.addIssue({
        code: "custom",
        message: "answer side effects require an initial request",
      });
    }
  });
export type GeneratorVuRoundEvent = z.infer<typeof GeneratorVuRoundEventSchema>;

/** `format`を公開discriminatorとして将来のevent追加をfail-closedにする。 */
export const GeneratorEventSchema = z.discriminatedUnion("format", [
  GeneratorVuRoundEventSchema,
]);
export type GeneratorEvent = z.infer<typeof GeneratorEventSchema>;

export type GeneratorEventRefusalCode =
  | "event_bom_forbidden"
  | "event_chunk_invalid"
  | "event_contract_invalid"
  | "event_count_exceeded"
  | "event_eof_partial"
  | "event_json_invalid"
  | "event_line_too_large"
  | "event_nul_forbidden"
  | "event_parser_failed"
  | "event_parser_finished"
  | "event_parser_options_invalid"
  | "event_stream_failed"
  | "event_utf8_invalid";

/** 入力値を例外messageへ含めず、固定codeだけで拒否理由を返す。 */
export class GeneratorEventRefusal extends Error {
  readonly code: GeneratorEventRefusalCode;

  constructor(code: GeneratorEventRefusalCode) {
    super(code);
    this.name = "GeneratorEventRefusal";
    this.code = code;
  }
}

export interface GeneratorEventStreamParserOptions {
  readonly maximumLineBytes?: number;
  readonly maximumEventCount?: number;
}

type ParserState = "failed" | "finished" | "open";

/**
 * k6 stdoutをbyte単位でframingし、prefix付きeventだけをstrict modelへ変換する。
 * 非prefix行は再出力せず破棄するため、k6由来の秘密値をlauncher logへ移さない。
 */
export class GeneratorEventStreamParser {
  readonly #maximumLineBytes: number;
  readonly #maximumEventCount: number;
  readonly #lineBuffer: Uint8Array;
  #lineLength = 0;
  #eventCount = 0;
  #state: ParserState = "open";

  constructor(options: GeneratorEventStreamParserOptions = {}) {
    const maximumLineBytes = options.maximumLineBytes ?? DEFAULT_MAX_EVENT_LINE_BYTES;
    const maximumEventCount = options.maximumEventCount ?? DEFAULT_MAX_EVENT_COUNT;
    if (
      !Number.isSafeInteger(maximumLineBytes) ||
      maximumLineBytes < 1 ||
      maximumLineBytes > MAX_EVENT_LINE_BYTES ||
      !Number.isSafeInteger(maximumEventCount) ||
      maximumEventCount < 1 ||
      maximumEventCount > DEFAULT_MAX_EVENT_COUNT
    ) {
      throw new GeneratorEventRefusal("event_parser_options_invalid");
    }
    this.#maximumLineBytes = maximumLineBytes;
    this.#maximumEventCount = maximumEventCount;
    this.#lineBuffer = new Uint8Array(maximumLineBytes);
  }

  push(chunk: Uint8Array): readonly GeneratorEvent[] {
    this.#requireOpen();
    if (!(chunk instanceof Uint8Array)) {
      return this.#fail("event_chunk_invalid");
    }

    const events: GeneratorEvent[] = [];
    let segmentStart = 0;
    try {
      for (let index = 0; index < chunk.byteLength; index += 1) {
        if (chunk[index] === 0) {
          return this.#fail("event_nul_forbidden");
        }
        if (chunk[index] !== 0x0a) {
          continue;
        }
        this.#append(chunk.subarray(segmentStart, index));
        const event = this.#finishLine();
        if (event !== null) {
          events.push(event);
        }
        segmentStart = index + 1;
      }
      this.#append(chunk.subarray(segmentStart));
      return events;
    } catch (error: unknown) {
      if (error instanceof GeneratorEventRefusal) {
        this.#state = "failed";
        throw error;
      }
      return this.#fail("event_contract_invalid");
    }
  }

  finish(): void {
    this.#requireOpen();
    if (this.#lineLength !== 0) {
      try {
        this.#validatePartialLineBeforeRejecting();
      } finally {
        this.#clearLineBuffer();
      }
      this.#fail("event_eof_partial");
    }
    this.#state = "finished";
  }

  #append(segment: Uint8Array): void {
    if (this.#lineLength + segment.byteLength > this.#maximumLineBytes) {
      this.#fail("event_line_too_large");
    }
    this.#lineBuffer.set(segment, this.#lineLength);
    this.#lineLength += segment.byteLength;
  }

  #finishLine(): GeneratorEvent | null {
    let line: string;
    try {
      line = this.#decodeLine(this.#lineBuffer.subarray(0, this.#lineLength));
    } finally {
      this.#clearLineBuffer();
    }
    if (line.includes("\uFEFF")) {
      this.#fail("event_bom_forbidden");
    }
    if (line.trim().length === 0) {
      return null;
    }
    if (!line.startsWith(GENERATOR_EVENT_PREFIX)) {
      return null;
    }
    const payload = line.slice(GENERATOR_EVENT_PREFIX.length);
    let parsed: unknown;
    try {
      parsed = JSON.parse(payload) as unknown;
    } catch {
      this.#fail("event_json_invalid");
    }
    const result = GeneratorEventSchema.safeParse(parsed);
    if (!result.success) {
      this.#fail("event_contract_invalid");
    }
    this.#eventCount += 1;
    if (this.#eventCount > this.#maximumEventCount) {
      this.#fail("event_count_exceeded");
    }
    return result.data;
  }

  #decodeLine(bytes: Uint8Array): string {
    try {
      return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
    } catch {
      return this.#fail("event_utf8_invalid");
    }
  }

  #validatePartialLineBeforeRejecting(): void {
    const line = this.#decodeLine(this.#lineBuffer.subarray(0, this.#lineLength));
    if (line.includes("\uFEFF")) {
      this.#fail("event_bom_forbidden");
    }
  }

  #requireOpen(): void {
    if (this.#state === "failed") {
      throw new GeneratorEventRefusal("event_parser_failed");
    }
    if (this.#state === "finished") {
      throw new GeneratorEventRefusal("event_parser_finished");
    }
  }

  #clearLineBuffer(): void {
    this.#lineBuffer.fill(0, 0, this.#lineLength);
    this.#lineLength = 0;
  }

  #fail(code: GeneratorEventRefusalCode): never {
    this.#clearLineBuffer();
    this.#state = "failed";
    throw new GeneratorEventRefusal(code);
  }
}

/** AsyncIterableをbounded parserへ流し、最大6000件だけを保持する。 */
export async function parseGeneratorEventStream(
  chunks: AsyncIterable<Uint8Array>,
  options: GeneratorEventStreamParserOptions = {},
): Promise<readonly GeneratorEvent[]> {
  const parser = new GeneratorEventStreamParser(options);
  const events: GeneratorEvent[] = [];
  try {
    for await (const chunk of chunks) {
      events.push(...parser.push(chunk));
    }
    parser.finish();
    return events;
  } catch (error: unknown) {
    if (error instanceof GeneratorEventRefusal) {
      throw error;
    }
    throw new GeneratorEventRefusal("event_stream_failed");
  }
}
