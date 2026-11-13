import http, {
  type CookieJar,
  type RefinedParams,
  type RefinedResponse,
} from "k6/http";
import type { ZodType } from "zod";

import {
  answerReceiptSchema,
  answerStatusSchema,
  decodeApiJson,
  participantJoinRequestSchema,
  participantStateSchema,
  readinessAcknowledgementSchema,
  readinessRequestSchema,
  submitAnswerRequestSchema,
  timeResponseSchema,
  type AnswerReceipt,
  type AnswerStatus,
  type ParticipantState,
  type ReadinessAcknowledgement,
  type ReadinessRequest,
  type SubmitAnswerRequest,
  type TimeResponse,
} from "./api-contracts.js";
import { classifyHttpStatus, type GeneratorHttpStatusClass } from "./k6-classification.js";
import {
  type GeneratorHttpOutcome,
  type GeneratorHttpRoute,
  type GeneratorStaticAssetFailure,
  K6GeneratorMetrics,
} from "./k6-metrics.js";
import {
  APPLICATION_ASSET_HTTP_NAMES,
  fetchK6ApplicationAssets,
} from "./k6-application-assets.js";
import type {
  GeneratorHttpResult,
  GeneratorParticipantTransport,
  GeneratorRoundAssetRequest,
} from "./session-contracts.js";
import {
  isRuntimeHttpsOriginShape,
} from "./runtime-manifest.js";

const API_TIMEOUT_MS = 5_000;
const CLOCK_TIMEOUT_MS = 1_000;
const MUTATION_TIMEOUT_MS = 2_500;
const EXPECTED_OK = http.expectedStatuses(200);
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu;

const HTTP_NAMES = {
  answer_status: "GET /api/v1/participant/answers/{client_answer_id}/status",
  ...APPLICATION_ASSET_HTTP_NAMES,
  heartbeat: "POST /api/v1/participant/session/heartbeat",
  join: "POST /api/v1/participant/join",
  participant_session: "GET /api/v1/participant/session",
  readiness: "POST /api/v1/participant/rounds/{round_id}/readiness",
  server_time: "GET /api/v1/time",
  submit_answer: "POST /api/v1/participant/rounds/{round_id}/answers",
} as const satisfies Record<GeneratorHttpRoute, string>;

export interface K6ParticipantTransportOptions {
  readonly publicOrigin: string;
  readonly roundQuestionIds: readonly string[];
  readonly jar: CookieJar;
  readonly metrics: K6GeneratorMetrics;
  readonly clock: () => number;
}

/** 公開APIと初回HTML/JS/CSSだけを、同じVU sessionで取得する。 */
export class K6ParticipantTransport implements GeneratorParticipantTransport {
  readonly #origin: string;
  readonly #apiOrigin: string;
  readonly #roundQuestionIds: readonly string[];
  readonly #jar: CookieJar;
  readonly #metrics: K6GeneratorMetrics;
  readonly #clock: () => number;
  #applicationAssetsLoaded: boolean | undefined;

  constructor(options: K6ParticipantTransportOptions) {
    if (
      !isRuntimeHttpsOriginShape(options.publicOrigin) ||
      options.roundQuestionIds.length !== 10 ||
      options.roundQuestionIds.some((questionId) => !UUID_PATTERN.test(questionId)) ||
      new Set(options.roundQuestionIds).size !== 10
    ) {
      throw new K6TransportConfigurationError();
    }
    this.#origin = options.publicOrigin;
    this.#apiOrigin = `${options.publicOrigin}/api/v1`;
    this.#roundQuestionIds = [...options.roundQuestionIds];
    this.#jar = options.jar;
    this.#metrics = options.metrics;
    this.#clock = options.clock;
  }

  checkSession(): GeneratorHttpResult<ParticipantState> {
    return this.#getJson(
      "participant_session",
      "/participant/session",
      participantStateSchema,
      API_TIMEOUT_MS,
    );
  }

  join(clientJoinToken: string, publicLabel: string): GeneratorHttpResult<ParticipantState> {
    const request = participantJoinRequestSchema.safeParse({
      client_join_token: clientJoinToken,
      public_label: publicLabel,
    });
    return this.#postJson(
      "join",
      "/participant/join",
      request,
      participantStateSchema,
      API_TIMEOUT_MS,
    );
  }

  heartbeat(): GeneratorHttpResult<ParticipantState> {
    return this.#requestJson(
      "POST",
      "heartbeat",
      "/participant/session/heartbeat",
      null,
      participantStateSchema,
      API_TIMEOUT_MS,
      false,
    );
  }

  refreshProjection(): GeneratorHttpResult<ParticipantState> {
    return this.checkSession();
  }

  recordReadiness(
    roundId: string,
    requestValue: ReadinessRequest,
  ): GeneratorHttpResult<ReadinessAcknowledgement> {
    const request = readinessRequestSchema.safeParse(requestValue);
    return this.#postForUuid(
      "readiness",
      roundId,
      `/participant/rounds/${roundId}/readiness`,
      request,
      readinessAcknowledgementSchema,
      MUTATION_TIMEOUT_MS,
    );
  }

  submitAnswer(
    roundId: string,
    requestValue: SubmitAnswerRequest,
  ): GeneratorHttpResult<AnswerReceipt> {
    const request = submitAnswerRequestSchema.safeParse(requestValue);
    return this.#postForUuid(
      "submit_answer",
      roundId,
      `/participant/rounds/${roundId}/answers`,
      request,
      answerReceiptSchema,
      MUTATION_TIMEOUT_MS,
    );
  }

  getAnswerStatus(clientAnswerId: string): GeneratorHttpResult<AnswerStatus> {
    if (!UUID_PATTERN.test(clientAnswerId)) {
      return this.#contractFailure("answer_status");
    }
    return this.#getJson(
      "answer_status",
      `/participant/answers/${clientAnswerId}/status`,
      answerStatusSchema,
      API_TIMEOUT_MS,
    );
  }

  getServerTime(): GeneratorHttpResult<TimeResponse> {
    return this.#getJson("server_time", "/time", timeResponseSchema, CLOCK_TIMEOUT_MS);
  }

  loadRoundAssets(request: GeneratorRoundAssetRequest): boolean {
    if (this.#roundQuestionIds[request.roundNumber - 1] !== request.questionId) {
      this.#recordAssetFailure("warm", "bundle_contract_invalid");
      return false;
    }
    if (this.#applicationAssetsLoaded !== undefined) {
      return this.#applicationAssetsLoaded;
    }
    const failure = fetchK6ApplicationAssets({
      origin: this.#origin, cacheMode: "warm", jar: this.#jar,
      metrics: this.#metrics, clock: this.#clock,
    });
    this.#applicationAssetsLoaded = failure === null;
    if (failure !== null) this.#recordAssetFailure("warm", failure);
    return this.#applicationAssetsLoaded;
  }

  #getJson<Value>(
    route: GeneratorHttpRoute,
    path: string,
    schema: ZodType<Value>,
    timeoutMs: number,
  ): GeneratorHttpResult<Value> {
    return this.#requestJson("GET", route, path, null, schema, timeoutMs, false);
  }

  #postJson<Input, Value>(
    route: GeneratorHttpRoute,
    path: string,
    parsedRequest: SafeParseResult<Input>,
    schema: ZodType<Value>,
    timeoutMs: number,
  ): GeneratorHttpResult<Value> {
    if (!parsedRequest.success) {
      return this.#contractFailure(route);
    }
    return this.#requestJson(
      "POST",
      route,
      path,
      JSON.stringify(parsedRequest.data),
      schema,
      timeoutMs,
      true,
    );
  }

  #postForUuid<Input, Value>(
    route: GeneratorHttpRoute,
    id: string,
    path: string,
    parsedRequest: SafeParseResult<Input>,
    schema: ZodType<Value>,
    timeoutMs: number,
  ): GeneratorHttpResult<Value> {
    if (!UUID_PATTERN.test(id)) {
      return this.#contractFailure(route);
    }
    return this.#postJson(route, path, parsedRequest, schema, timeoutMs);
  }

  #requestJson<Value>(
    method: "GET" | "POST",
    route: GeneratorHttpRoute,
    path: string,
    body: string | null,
    schema: ZodType<Value>,
    timeoutMs: number,
    hasJsonBody: boolean,
  ): GeneratorHttpResult<Value> {
    const startedAtLocalMs = this.#clock();
    let response: RefinedResponse<"text">;
    try {
      const params = this.#params(route, timeoutMs, "text", hasJsonBody);
      response = http.request(method, `${this.#apiOrigin}${path}`, body, params);
    } catch {
      return this.#failedResult(
        route,
        "transport_failure",
        startedAtLocalMs,
        this.#clock(),
      );
    }
    const endedAtLocalMs = this.#clock();
    const statusClass = classifyHttpStatus(response.status);
    if (statusClass !== "success") {
      return this.#failedResult(
        route,
        httpFailureOutcome(statusClass),
        startedAtLocalMs,
        endedAtLocalMs,
      );
    }

    try {
      const value = decodeApiJson(schema, response.body);
      this.#metrics.recordHttp(route, "success", endedAtLocalMs - startedAtLocalMs);
      return { kind: "success", value, startedAtLocalMs, endedAtLocalMs };
    } catch {
      return this.#failedResult(
        route,
        "contract_failure",
        startedAtLocalMs,
        endedAtLocalMs,
      );
    }
  }

  #params<ResponseKind extends "binary" | "text">(
    route: GeneratorHttpRoute,
    timeoutMs: number,
    responseType: ResponseKind,
    hasJsonBody: boolean,
  ): RefinedParams<ResponseKind> {
    return {
      headers: {
        Accept: "application/json",
        Origin: this.#origin,
        ...(hasJsonBody ? { "Content-Type": "application/json" } : {}),
      },
      jar: this.#jar,
      redirects: 0,
      responseCallback: EXPECTED_OK,
      responseType,
      tags: { name: HTTP_NAMES[route] },
      timeout: timeoutMs,
    };
  }

  #contractFailure<Value>(route: GeneratorHttpRoute): GeneratorHttpResult<Value> {
    const now = this.#clock();
    return this.#failedResult(route, "contract_failure", now, now);
  }

  #failedResult<Value>(
    route: GeneratorHttpRoute,
    outcome: Exclude<GeneratorHttpOutcome, "success">,
    startedAtLocalMs: number,
    endedAtLocalMs: number,
  ): GeneratorHttpResult<Value> {
    this.#metrics.recordHttp(route, outcome, endedAtLocalMs - startedAtLocalMs);
    if (outcome === "client_failure") {
      return {
        kind: "known-failure",
        failure: "client",
        startedAtLocalMs,
        endedAtLocalMs,
      };
    }
    if (outcome === "server_failure") {
      return {
        kind: "known-failure",
        failure: "server",
        startedAtLocalMs,
        endedAtLocalMs,
      };
    }
    return { kind: "unknown", startedAtLocalMs, endedAtLocalMs };
  }

  #recordAssetFailure(
    cacheMode: "cold" | "warm",
    reason: GeneratorStaticAssetFailure,
  ): void {
    this.#metrics.recordStaticAssetFailure(cacheMode, reason);
  }
}

interface SafeParseResult<Value> {
  readonly success: boolean;
  readonly data?: Value;
}

export class K6TransportConfigurationError extends Error {
  constructor() {
    super("generator_transport_configuration_invalid");
    this.name = "K6TransportConfigurationError";
  }
}

function httpFailureOutcome(
  statusClass: Exclude<GeneratorHttpStatusClass, "success">,
): Exclude<GeneratorHttpOutcome, "contract_failure" | "success"> {
  return statusClass === "client"
    ? "client_failure"
    : statusClass === "server"
      ? "server_failure"
      : "transport_failure";
}
