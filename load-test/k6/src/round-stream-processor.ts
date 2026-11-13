import { canonicalJsonBytes } from "./canonical-json.js";
import {
  GeneratorEventRefusal,
  GeneratorEventStreamParser,
} from "./events.js";
import type {
  CompletedRoundArtifact,
  GeneratorStreamProcessor,
} from "./launcher.js";
import type { LoadedManifest } from "./manifest.js";
import {
  RoundAggregationRefusal,
  RoundEventAggregator,
} from "./round-aggregator.js";

const VIRTUAL_SESSION_COUNT = 600;
const FINAL_ROUND_NUMBER = 10;

export type RoundStreamRefusalCode =
  | "round_stream_clock_invalid"
  | "round_stream_failed"
  | "round_stream_incomplete"
  | "round_stream_sequence_invalid";

export class RoundStreamRefusal extends Error {
  readonly code: RoundStreamRefusalCode;

  constructor(code: RoundStreamRefusalCode) {
    super(code);
    this.name = "RoundStreamRefusal";
    this.code = code;
  }
}

export interface GeneratorRoundStreamProcessorOptions {
  /** productionではwall clock、testではRound slotへ固定したclockを注入する。 */
  readonly clock?: (roundNumber: number) => Date;
}

/**
 * stdout framingとRound集約を接続し、600件が揃ったRoundだけを即座に返す。
 * 次Roundの先行eventは保持せず拒否し、partial evidenceをseal対象へ渡さない。
 */
export class GeneratorRoundStreamProcessor implements GeneratorStreamProcessor {
  readonly #loadedManifest: LoadedManifest;
  readonly #parser = new GeneratorEventStreamParser();
  readonly #clock: (roundNumber: number) => Date;
  #nextRoundNumber = 1;
  #aggregator: RoundEventAggregator;
  #isFailed = false;
  #isFinished = false;

  constructor(
    loadedManifest: LoadedManifest,
    options: GeneratorRoundStreamProcessorOptions = {},
  ) {
    this.#loadedManifest = loadedManifest;
    this.#clock = options.clock ?? (() => new Date());
    this.#aggregator = this.#createAggregator(this.#nextRoundNumber);
  }

  push(chunk: Uint8Array): readonly CompletedRoundArtifact[] {
    this.#requireOpen();
    try {
      const completed: CompletedRoundArtifact[] = [];
      for (const event of this.#parser.push(chunk)) {
        if (
          this.#nextRoundNumber > FINAL_ROUND_NUMBER ||
          event.round_number !== this.#nextRoundNumber
        ) {
          throw new RoundStreamRefusal("round_stream_sequence_invalid");
        }
        this.#aggregator.add(event);
        if (this.#aggregator.eventCount !== VIRTUAL_SESSION_COUNT) {
          continue;
        }
        const evidence = this.#aggregator.finalize(
          canonicalAwareDatetime(this.#clock(this.#nextRoundNumber)),
        );
        completed.push({
          roundNumber: this.#nextRoundNumber,
          name: `generator-round-${String(this.#nextRoundNumber).padStart(2, "0")}.json`,
          body: withTrailingLineFeed(canonicalJsonBytes(evidence)),
        });
        this.#nextRoundNumber += 1;
        if (this.#nextRoundNumber <= FINAL_ROUND_NUMBER) {
          this.#aggregator = this.#createAggregator(this.#nextRoundNumber);
        }
      }
      return completed;
    } catch (error: unknown) {
      this.#isFailed = true;
      if (
        error instanceof RoundStreamRefusal ||
        error instanceof RoundAggregationRefusal ||
        error instanceof GeneratorEventRefusal
      ) {
        throw error;
      }
      throw new RoundStreamRefusal("round_stream_failed");
    }
  }

  finish(): readonly CompletedRoundArtifact[] {
    this.#requireOpen();
    try {
      this.#parser.finish();
      this.#isFinished = true;
      if (this.#nextRoundNumber !== FINAL_ROUND_NUMBER + 1) {
        throw new RoundStreamRefusal("round_stream_incomplete");
      }
      return [];
    } catch (error: unknown) {
      this.#isFailed = true;
      if (error instanceof RoundStreamRefusal || error instanceof GeneratorEventRefusal) {
        throw error;
      }
      throw new RoundStreamRefusal("round_stream_failed");
    }
  }

  #createAggregator(roundNumber: number): RoundEventAggregator {
    return new RoundEventAggregator({
      manifest: this.#loadedManifest.manifest,
      hashes: this.#loadedManifest.hashes,
      roundNumber,
    });
  }

  #requireOpen(): void {
    if (this.#isFailed) {
      throw new RoundStreamRefusal("round_stream_failed");
    }
    if (this.#isFinished) {
      throw new RoundStreamRefusal("round_stream_sequence_invalid");
    }
  }
}

/** JavaScript millisecond時刻をPydantic datetimeのcanonical JSON表現へ合わせる。 */
export function canonicalAwareDatetime(date: Date): string {
  if (!(date instanceof Date) || !Number.isFinite(date.getTime())) {
    throw new RoundStreamRefusal("round_stream_clock_invalid");
  }
  return date.toISOString().replace(/\.(\d{3})Z$/, (_match, milliseconds: string) =>
    milliseconds === "000" ? "Z" : `.${milliseconds}000Z`,
  );
}

function withTrailingLineFeed(body: Uint8Array): Uint8Array {
  const terminated = new Uint8Array(body.byteLength + 1);
  terminated.set(body);
  terminated[terminated.byteLength - 1] = 0x0a;
  return terminated;
}
