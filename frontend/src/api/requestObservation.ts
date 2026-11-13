import { z } from "zod";
import type { CancellationReason } from "./cancellation";

import {
  API_OBSERVATION_CODES,
  observeBrowser,
  SERVER_TIMING_METRICS,
  type ApiObservationOperation,
  type ApiObservationOutcome,
  type SafeServerTiming,
} from "../quiz/_utils/browserObservation";

let requestSequence = 0;
const serverRequestIdSchema = z.string().length(32).regex(/^[a-f0-9]+$/u);
const answerTraceSchema = z.string().length(64).regex(/^[a-f0-9]+$/u);
const apiCodeSchema = z.enum(API_OBSERVATION_CODES);

type RequestObservation = {
  requestSequence: number;
  headers: (response: Response) => void;
  body: () => void;
  settled: (outcome: ApiObservationOutcome, apiCode?: string, cancellationReason?: CancellationReason) => void;
};

function serverTiming(header: string | null): SafeServerTiming {
  const timing: SafeServerTiming = {};
  for (const field of header?.split(",") ?? []) {
    const [rawName, ...parameters] = field.trim().split(";");
    const name = SERVER_TIMING_METRICS.find((metric) => metric === rawName);
    if (name === undefined) continue;
    const duration = parameters.find((parameter) => parameter.trim().startsWith("dur="));
    const rawDuration = duration?.trim().slice(4);
    const value = rawDuration !== undefined && /^\d+(?:\.\d+)?$/u.test(rawDuration)
      ? Number(rawDuration) : Number.NaN;
    if (Number.isFinite(value) && value >= 0) timing[name] = value;
  }
  return timing;
}

// 同一ページ内の連番で照合する。URL・回答ID・本文・例外文字列をイベントへ渡さない。
export function beginRequestObservation(
  operation: ApiObservationOperation | undefined,
  url: string,
): RequestObservation | undefined {
  if (operation === undefined) return undefined;
  const request_sequence = ++requestSequence;
  const started = performance.now();
  let headersAt: number | undefined;
  let bodyAt: number | undefined;
  let status: number | undefined;
  let safeServerTiming: SafeServerTiming | undefined;
  let serverRequestId: string | undefined;
  let answerTrace: string | undefined;
  observeBrowser({ kind: "api_request", operation, request_sequence, phase: "started", elapsed_ms: 0 });
  return {
    requestSequence: request_sequence,
    headers(response) {
      headersAt = performance.now();
      status = response.status;
      safeServerTiming = serverTiming(response.headers.get("Server-Timing"));
      const requestId = serverRequestIdSchema.safeParse(response.headers.get("x-quiz-request-id"));
      const trace = answerTraceSchema.safeParse(response.headers.get("x-quiz-answer-trace"));
      serverRequestId = requestId.success ? requestId.data : undefined;
      answerTrace = trace.success ? trace.data : undefined;
    },
    body() { bodyAt = performance.now(); },
    settled(outcome, apiCode, cancellationReason) {
      const ended = performance.now();
      // ResourceTimingのネットワーク完了とJS処理時刻を分ける。存在しない標本を0で補わない。
      const resource = performance.getEntriesByName?.(new URL(url, window.location.href).href, "resource")
        .filter((entry) => entry.startTime >= started && entry.startTime <= ended)
        .at(-1) as PerformanceResourceTiming | undefined;
      const responseEnd = resource?.responseEnd;
      const safeCode = outcome === "http_error" && apiCode !== undefined ? apiCodeSchema.safeParse(apiCode) : undefined;
      observeBrowser({
        kind: "api_request", operation, request_sequence, phase: "settled", outcome,
        elapsed_ms: ended - started,
        ...((outcome === "aborted" || outcome === "timeout") && cancellationReason !== undefined
          ? { cancellation_reason: cancellationReason } : {}),
        ...(status === undefined ? {} : { status }),
        ...(headersAt === undefined ? {} : { headers_elapsed_ms: headersAt - started }),
        ...(bodyAt === undefined ? {} : { body_elapsed_ms: bodyAt - started, validation_ms: ended - bodyAt }),
        ...(responseEnd === undefined || responseEnd < started || responseEnd > ended ? {} : {
          response_end_elapsed_ms: responseEnd - started, delivery_lag_ms: ended - responseEnd,
        }),
        ...(safeServerTiming === undefined ? {} : { server_timing: safeServerTiming }),
        ...(serverRequestId === undefined ? {} : { server_request_id: serverRequestId }),
        ...(answerTrace === undefined ? {} : { answer_trace: answerTrace }),
        ...(safeCode === undefined ? {} : { api_code: safeCode.success ? safeCode.data : "other" }),
      });
    },
  };
}
