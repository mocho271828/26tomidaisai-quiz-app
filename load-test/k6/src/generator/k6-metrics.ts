import { Counter, Trend } from "k6/metrics";

import { classifyWebSocketCloseCode } from "./k6-classification.js";
import { GENERATOR_METRIC_NAMES } from "./metric-taxonomy.js";
import type {
  GeneratorHttpOutcome,
  GeneratorHttpRoute,
  GeneratorProtocolBoundary,
  GeneratorStaticAssetFailure,
  GeneratorStaticCacheMode,
  GeneratorWebSocketAttempt,
  GeneratorWebSocketMessage,
  GeneratorWebSocketOutcome,
} from "./metric-taxonomy.js";

export type {
  GeneratorHttpOutcome,
  GeneratorHttpRoute,
  GeneratorProtocolBoundary,
  GeneratorStaticAssetFailure,
  GeneratorStaticCacheMode,
  GeneratorWebSocketAttempt,
  GeneratorWebSocketMessage,
  GeneratorWebSocketOutcome,
} from "./metric-taxonomy.js";

/**
 * custom metricsの名前とtag集合を固定し、VU・UUID・URLを時系列labelへ混入させない。
 */
export class K6GeneratorMetrics {
  readonly #httpRequests = new Counter(GENERATOR_METRIC_NAMES.httpRequests);
  readonly #httpDuration = new Trend(GENERATOR_METRIC_NAMES.httpDuration, true);
  readonly #staticAssetFailures = new Counter(
    GENERATOR_METRIC_NAMES.staticAssetFailures,
  );
  readonly #webSocketConnections = new Counter(
    GENERATOR_METRIC_NAMES.webSocketConnections,
  );
  readonly #webSocketMessages = new Counter(
    GENERATOR_METRIC_NAMES.webSocketMessages,
  );
  readonly #webSocketCloses = new Counter(GENERATOR_METRIC_NAMES.webSocketCloses);
  readonly #webSocketErrors = new Counter(GENERATOR_METRIC_NAMES.webSocketErrors);
  readonly #protocolRefusals = new Counter(
    GENERATOR_METRIC_NAMES.protocolRefusals,
  );

  recordHttp(
    route: GeneratorHttpRoute,
    outcome: GeneratorHttpOutcome,
    durationMs: number,
  ): void {
    const tags = { route, outcome };
    this.#httpRequests.add(1, tags);
    this.#httpDuration.add(requireFiniteDuration(durationMs), tags);
  }

  recordStaticAssetFailure(
    cacheMode: GeneratorStaticCacheMode,
    reason: GeneratorStaticAssetFailure,
  ): void {
    this.#staticAssetFailures.add(1, { cache_mode: cacheMode, reason });
  }


  recordWebSocketConnect(
    attempt: GeneratorWebSocketAttempt,
    outcome: GeneratorWebSocketOutcome,
  ): void {
    this.#webSocketConnections.add(1, { attempt, outcome });
  }

  recordWebSocketMessage(message: GeneratorWebSocketMessage): void {
    this.#webSocketMessages.add(1, { message });
  }

  recordWebSocketClose(code: number): void {
    this.#webSocketCloses.add(1, { code: classifyWebSocketCloseCode(code) });
  }

  recordWebSocketError(): void {
    this.#webSocketErrors.add(1);
  }

  recordProtocolRefusal(boundary: GeneratorProtocolBoundary): void {
    this.#protocolRefusals.add(1, { boundary });
  }
}

function requireFiniteDuration(value: number): number {
  return Number.isFinite(value) && value >= 0 ? value : 0;
}
