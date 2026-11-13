import { SESSION_CONNECTION_PREFIX } from "./generator/ramp-readiness.js";
import { GeneratorRampReadiness } from "./ramp-readiness.js";
import { CANARY_PREFIX, CANARY_DEADLINE_MILLISECONDS, CanaryCompletionSchema } from "./generator/canary.js";
import { canonicalSha256 } from "./canonical-json.js";
import { GENERATOR_EVENT_PREFIX } from "./events.js";
import { GeneratorStreamProcessingRefusal } from "./generator-stream-refusal.js";
import type {
  CompletedFinalArtifact,
  CompletedRoundArtifact,
  GeneratorStreamProcessor,
} from "./launcher.js";
import type { LoadedManifest } from "./manifest.js";
import { GeneratorRoundStreamProcessor } from "./round-stream-processor.js";
import type { GeneratorRoundStreamProcessorOptions } from "./round-stream-processor.js";
import {
  GENERATOR_METRICS_ARTIFACT_NAME,
  GENERATOR_METRICS_PREFIX,
  MAX_GENERATOR_METRICS_ARTIFACT_BYTES,
  parseGeneratorMetricsSummary,
  serializeGeneratorMetricsSummary,
} from "./generator/metrics-summary-contract.js";
import type {
  GeneratorMetricsBinding,
  GeneratorMetricsSummary,
} from "./generator/metrics-summary-contract.js";
import { projectK6RuntimeManifest } from "./generator/runtime-manifest.js";

const FINAL_ROUND_NUMBER = 10;
const RESERVED_OUTPUT_PREFIX = "@@LOAD_TEST_";
const MAX_OUTPUT_LINE_BYTES =
  MAX_GENERATOR_METRICS_ARTIFACT_BYTES +
  new TextEncoder().encode(GENERATOR_METRICS_PREFIX).byteLength;
export const MAX_GENERATOR_OUTPUT_BYTES = 64 * 1_024 * 1_024;

export type GeneratorOutputRefusalCode =
  | "generator_output_bom_forbidden"
  | "generator_output_chunk_invalid"
  | "generator_output_eof_partial"
  | "generator_output_failed"
  | "generator_output_finished"
  | "generator_output_line_too_large"
  | "generator_output_metrics_duplicate"
  | "generator_output_metrics_early"
  | "generator_output_nul_forbidden"
  | "generator_output_size_invalid"
  | "generator_output_reserved_prefix"
  | "generator_output_sequence_invalid"
  | "generator_output_utf8_invalid";

export class GeneratorOutputRefusal extends GeneratorStreamProcessingRefusal {
  readonly code: GeneratorOutputRefusalCode;

  constructor(
    code: GeneratorOutputRefusalCode,
    completedArtifacts: readonly CompletedRoundArtifact[] = [],
  ) {
    super(code, completedArtifacts);
    this.name = "GeneratorOutputRefusal";
    this.code = code;
  }
}

type ProcessorState = "failed" | "finished" | "open";

/**
 * k6 stdoutを行順どおりmultiplexし、Roundは即時、summaryは正常終了後に渡す。
 * 通常のk6行は再出力せず、予約prefixの未知recordはfail-closedにする。
 */
export class GeneratorOutputStreamProcessor implements GeneratorStreamProcessor {
  readonly #rampReadiness: GeneratorRampReadiness;
  readonly #canaryVus = new Set<number>();
  readonly #canaryStart: number;
  readonly #roundProcessor: GeneratorRoundStreamProcessor;
  readonly #binding: GeneratorMetricsBinding;
  readonly #lineBuffer = new Uint8Array(MAX_OUTPUT_LINE_BYTES);
  #lineLength = 0;
  #outputBytes = 0;
  #completedRoundCount = 0;
  #metricsSummary: GeneratorMetricsSummary | null = null;
  #state: ProcessorState = "open";
  #wasFinalArtifactTaken = false;

  constructor(
    loadedManifest: LoadedManifest,
    options: GeneratorRoundStreamProcessorOptions = {},
  ) {
    this.#rampReadiness = new GeneratorRampReadiness(loadedManifest);
    this.#roundProcessor = new GeneratorRoundStreamProcessor(
      loadedManifest,
      options,
    );
    this.#binding = metricsBinding(loadedManifest);
    this.#canaryStart = Date.parse(loadedManifest.manifest.scheduled_start_at);
  }

  takeProgressArtifact(): CompletedFinalArtifact | null {
    if (this.#state !== "open" || this.#lineLength !== 0) return null;
    return this.#rampReadiness.takeArtifact();
  }

  isCanarySatisfied(): boolean { return this.#canaryVus.size === 2; }

  push(chunk: Uint8Array): readonly CompletedRoundArtifact[] {
    this.#requireOpen();
    if (!(chunk instanceof Uint8Array)) {
      return this.#fail("generator_output_chunk_invalid");
    }
    if (chunk.byteLength > MAX_GENERATOR_OUTPUT_BYTES - this.#outputBytes) {
      return this.#fail("generator_output_size_invalid");
    }
    this.#outputBytes += chunk.byteLength;

    const completed: CompletedRoundArtifact[] = [];
    let segmentStart = 0;
    try {
      for (let index = 0; index < chunk.byteLength; index += 1) {
        if (chunk[index] === 0) {
          return this.#fail("generator_output_nul_forbidden");
        }
        if (chunk[index] !== 0x0a) {
          continue;
        }
        this.#append(chunk.subarray(segmentStart, index));
        completed.push(...this.#finishLine());
        segmentStart = index + 1;
      }
      this.#append(chunk.subarray(segmentStart));
      return completed;
    } catch (error: unknown) {
      this.#state = "failed";
      if (error instanceof GeneratorOutputRefusal) {
        throw new GeneratorOutputRefusal(error.code, [
          ...completed,
          ...error.completedArtifacts,
        ]);
      }
      if (completed.length !== 0) {
        throw new GeneratorStreamProcessingRefusal(
          "generator_stream_refused_after_completion",
          completed,
        );
      }
      throw error;
    }
  }

  finish(): readonly CompletedRoundArtifact[] {
    this.#requireOpen();
    if (this.#lineLength !== 0) {
      try {
        this.#decodeLine(this.#lineBuffer.subarray(0, this.#lineLength));
      } finally {
        this.#clearLineBuffer();
      }
      return this.#fail("generator_output_eof_partial");
    }
    if (!this.isCanarySatisfied()) return this.#fail("generator_output_sequence_invalid");
    try {
      const completed = this.#roundProcessor.finish();
      this.#completedRoundCount += completed.length;
      this.#state = "finished";
      return completed;
    } catch (error: unknown) {
      this.#state = "failed";
      throw error;
    }
  }

  takeFinalArtifact(): CompletedFinalArtifact | null {
    if (this.#state === "failed") {
      throw new GeneratorOutputRefusal("generator_output_failed");
    }
    if (this.#state !== "finished" || this.#wasFinalArtifactTaken) {
      throw new GeneratorOutputRefusal("generator_output_sequence_invalid");
    }
    this.#wasFinalArtifactTaken = true;
    if (this.#metricsSummary === null) {
      return null;
    }
    const payload = serializeGeneratorMetricsSummary(this.#metricsSummary);
    return {
      name: GENERATOR_METRICS_ARTIFACT_NAME,
      body: withTrailingLineFeed(payload),
    };
  }

  #finishLine(): readonly CompletedRoundArtifact[] {
    let line: string;
    try {
      line = this.#decodeLine(this.#lineBuffer.subarray(0, this.#lineLength));
    } finally {
      this.#clearLineBuffer();
    }
    if (line.includes("\uFEFF")) {
      return this.#fail("generator_output_bom_forbidden");
    }
    if (line.trim().length === 0) {
      return [];
    }
    if (line.startsWith(SESSION_CONNECTION_PREFIX)) {
      let payload: unknown;
      try { payload = JSON.parse(line.slice(SESSION_CONNECTION_PREFIX.length)); }
      catch { return this.#fail("generator_output_sequence_invalid"); }
      if (!this.#rampReadiness.accept(payload)) return this.#fail("generator_output_sequence_invalid");
      return [];
    }
    if (line.startsWith(CANARY_PREFIX)) {
      let payload: unknown;
      try { payload = JSON.parse(line.slice(CANARY_PREFIX.length)); }
      catch { return this.#fail("generator_output_sequence_invalid"); }
      const parsed = CanaryCompletionSchema.safeParse(payload);
      if (!parsed.success || parsed.data.run_id !== this.#binding.runId ||
          parsed.data.event_id !== this.#binding.eventId ||
          parsed.data.manifest_sha256 !== this.#binding.manifestSha256 ||
          parsed.data.observed_at_unix_ms < this.#canaryStart ||
          parsed.data.observed_at_unix_ms > this.#canaryStart + CANARY_DEADLINE_MILLISECONDS ||
          this.#canaryVus.has(parsed.data.vu_id)) {
        return this.#fail("generator_output_sequence_invalid");
      }
      this.#canaryVus.add(parsed.data.vu_id);
      return [];
    }
    if (line.startsWith(GENERATOR_EVENT_PREFIX)) {
      if (this.#metricsSummary !== null) {
        return this.#fail("generator_output_sequence_invalid");
      }
      const completed = this.#roundProcessor.push(
        new TextEncoder().encode(`${line}\n`),
      );
      this.#completedRoundCount += completed.length;
      if (this.#completedRoundCount > FINAL_ROUND_NUMBER) {
        return this.#fail("generator_output_sequence_invalid");
      }
      return completed;
    }
    if (line.startsWith(GENERATOR_METRICS_PREFIX)) {
      if (this.#metricsSummary !== null) {
        return this.#fail("generator_output_metrics_duplicate");
      }
      if (this.#completedRoundCount !== FINAL_ROUND_NUMBER) {
        return this.#fail("generator_output_metrics_early");
      }
      this.#metricsSummary = parseGeneratorMetricsSummary(
        line.slice(GENERATOR_METRICS_PREFIX.length),
        this.#binding,
      );
      return [];
    }
    if (line.startsWith(RESERVED_OUTPUT_PREFIX)) {
      return this.#fail("generator_output_reserved_prefix");
    }
    return [];
  }

  #append(segment: Uint8Array): void {
    if (this.#lineLength + segment.byteLength > MAX_OUTPUT_LINE_BYTES) {
      this.#fail("generator_output_line_too_large");
    }
    this.#lineBuffer.set(segment, this.#lineLength);
    this.#lineLength += segment.byteLength;
  }

  #decodeLine(bytes: Uint8Array): string {
    try {
      return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
    } catch {
      return this.#fail("generator_output_utf8_invalid");
    }
  }

  #requireOpen(): void {
    if (this.#state === "failed") {
      throw new GeneratorOutputRefusal("generator_output_failed");
    }
    if (this.#state === "finished") {
      throw new GeneratorOutputRefusal("generator_output_finished");
    }
  }

  #clearLineBuffer(): void {
    this.#lineBuffer.fill(0, 0, this.#lineLength);
    this.#lineLength = 0;
  }

  #fail(code: GeneratorOutputRefusalCode): never {
    this.#clearLineBuffer();
    this.#state = "failed";
    throw new GeneratorOutputRefusal(code);
  }
}

function metricsBinding(loadedManifest: LoadedManifest): GeneratorMetricsBinding {
  const runtimeManifest = projectK6RuntimeManifest(
    loadedManifest.manifest,
    loadedManifest.hashes,
  );
  return {
    runId: loadedManifest.manifest.run_id,
    eventId: loadedManifest.manifest.event.id,
    sourceCommit: loadedManifest.manifest.artifacts.source_commit,
    schemaRevision: loadedManifest.manifest.schema_revision,
    manifestSha256: loadedManifest.hashes.manifestSha256,
    scenarioSha256: loadedManifest.hashes.scenarioSha256,
    runtimeManifestSha256: canonicalSha256(runtimeManifest),
  };
}

function withTrailingLineFeed(canonical: string): Uint8Array {
  const body = new TextEncoder().encode(canonical);
  const terminated = new Uint8Array(body.byteLength + 1);
  terminated.set(body);
  terminated[terminated.byteLength - 1] = 0x0a;
  return terminated;
}
