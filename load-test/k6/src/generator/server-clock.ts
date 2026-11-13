import type { TimeResponse } from "./api-contracts.js";
import type { GeneratorHttpResult } from "./session-contracts.js";
import { GeneratorProtocolRefusal } from "./session-contracts.js";

interface ClockSample {
  readonly estimatedServerAtEndMs: number;
  readonly endedAtLocalMs: number;
  readonly roundTripMs: number;
}

interface ClockAnchor {
  readonly serverTimeMs: number;
  readonly localTimeMs: number;
}

/** UIと同じ最短RTT・midpoint補正を使い、同期時計を後退させない。 */
export class GeneratorServerClock {
  #anchor: ClockAnchor | null = null;

  get isSynchronized(): boolean {
    return this.#anchor !== null;
  }

  now(localNowMs: number): number | null {
    if (!Number.isFinite(localNowMs)) {
      throw new GeneratorProtocolRefusal("generator_clock_invalid");
    }
    if (this.#anchor === null) {
      return null;
    }
    return this.#anchor.serverTimeMs + Math.max(0, localNowMs - this.#anchor.localTimeMs);
  }

  apply(
    results: readonly GeneratorHttpResult<TimeResponse>[],
    appliedAtLocalMs: number,
    maximumRoundTripMs: number,
  ): boolean {
    if (
      !Number.isFinite(appliedAtLocalMs) ||
      !Number.isFinite(maximumRoundTripMs) ||
      maximumRoundTripMs < 0
    ) {
      throw new GeneratorProtocolRefusal("generator_clock_invalid");
    }
    const samples = results.flatMap((result) => {
      if (result.kind !== "success") {
        return [];
      }
      const serverTimeMs = Date.parse(result.value.server_time);
      const roundTripMs = result.endedAtLocalMs - result.startedAtLocalMs;
      if (
        !Number.isFinite(serverTimeMs) ||
        !Number.isFinite(roundTripMs) ||
        roundTripMs < 0
      ) {
        return [];
      }
      return [
        {
          estimatedServerAtEndMs: serverTimeMs + roundTripMs / 2,
          endedAtLocalMs: result.endedAtLocalMs,
          roundTripMs,
        } satisfies ClockSample,
      ];
    });
    const shortest = samples.reduce<ClockSample | null>(
      (current, sample) =>
        current === null || sample.roundTripMs < current.roundTripMs ? sample : current,
      null,
    );
    if (shortest === null || shortest.roundTripMs > maximumRoundTripMs) {
      this.#anchor = null;
      return false;
    }

    const sampledAtApply =
      shortest.estimatedServerAtEndMs +
      Math.max(0, appliedAtLocalMs - shortest.endedAtLocalMs);
    const currentAtApply = this.now(appliedAtLocalMs) ?? Number.NEGATIVE_INFINITY;
    this.#anchor = {
      serverTimeMs: Math.max(sampledAtApply, currentAtApply),
      localTimeMs: appliedAtLocalMs,
    };
    return true;
  }
}
