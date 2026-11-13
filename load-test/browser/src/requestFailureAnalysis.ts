import type { AnalysisEvent } from "./analyze.js";

type ApiRequest = Extract<NonNullable<AnalysisEvent["telemetry"]>, {kind: "api_request"}>;
const operationRoutes: Readonly<Record<ApiRequest["operation"], string>> = {
  answer_post: "answer", answer_status: "answer_status", participant_state: "state",
  participant_result: "personal_result", server_time: "time", participant_join: "join",
};
const expectedCancellationReasons = new Set([
  "superseded", "hidden", "unmount", "run_shutdown", "result_received", "revision_changed",
]);
type FailureKind = "expected_cancel" | "timeout" | "transport" | "unknown";
const failureKinds: FailureKind[] = ["expected_cancel", "timeout", "transport", "unknown"];

function requestKey(event: AnalysisEvent): string | undefined {
  return event.serial === undefined || event.requestSequence === undefined ? undefined :
    `${event.serial}:${event.requestSequence}`;
}

function classify(failure: AnalysisEvent, candidates: AnalysisEvent[]): FailureKind {
  if (candidates.length !== 1) return "unknown";
  const telemetry = candidates[0]!.telemetry;
  if (telemetry?.kind !== "api_request" || operationRoutes[telemetry.operation] !== failure.route) return "unknown";
  if (telemetry.outcome === "timeout" || telemetry.outcome === "aborted" && telemetry.cancellation_reason === "deadline") return "timeout";
  if (telemetry.outcome === "aborted" && telemetry.cancellation_reason !== undefined &&
    expectedCancellationReasons.has(telemetry.cancellation_reason)) return "expected_cancel";
  return telemetry.outcome === "transport_error" ? "transport" : "unknown";
}

export function analyzeRequestFailures(events: AnalysisEvent[], recorded: AnalysisEvent[], hasMeasurementWindow: boolean) {
  const failures = events.filter(event => event.event === "request_failed");
  const failureKeyCounts = new Map<string, number>();
  for (const failure of failures) {
    const key = requestKey(failure);
    if (key !== undefined) failureKeyCounts.set(key, (failureKeyCounts.get(key) ?? 0) + 1);
  }
  const settled = new Map<string, AnalysisEvent[]>();
  for (const event of events) {
    const telemetry = event.telemetry;
    if (event.serial === undefined || telemetry?.kind !== "api_request" || telemetry.phase !== "settled") continue;
    const key = `${event.serial}:${telemetry.request_sequence}`;
    settled.set(key, [...settled.get(key) ?? [], event]);
  }
  const classified = failures.map(event => {
    const key = requestKey(event);
    return {event, kind: classify(event,
      key !== undefined && failureKeyCounts.get(key) === 1 ? settled.get(key) ?? [] : [])};
  });
  const summary = (kind: FailureKind): object => {
    const matching = classified.filter(value => value.kind === kind);
    return {requests: matching.length,
      participants: new Set(matching.flatMap(value => value.event.serial === undefined ? [] : [value.event.serial])).size};
  };
  const unexpectedFailureCount = classified.filter(value => value.kind !== "expected_cancel").length;
  return {
    classificationVersion: "browser-request-failures-v1" as const,
    measurementWindowAvailable: hasMeasurementWindow,
    recordedRawCount: recorded.filter(event => event.event === "request_failed").length,
    measurementRawCount: failures.length,
    outsideMeasurementRawCount: recorded.filter(event => event.event === "request_failed").length - failures.length,
    strictZeroRequestFailuresSatisfied: hasMeasurementWindow ? failures.length === 0 : null,
    classifiedZeroUnexpectedFailuresSatisfied: hasMeasurementWindow ? unexpectedFailureCount === 0 : null,
    unexpectedFailureCount,
    classifications: Object.fromEntries(failureKinds.map(kind => [kind, summary(kind)])),
    applicationOutcomes: Object.fromEntries(["validated", "http_error", "contract_error", "transport_error", "aborted", "timeout"]
      .map(outcome => [outcome, events.filter(event => event.telemetry?.kind === "api_request" &&
        event.telemetry.phase === "settled" && event.telemetry.outcome === outcome).length])),
    scope: "request_failed原数を保持。serial・request_sequence・routeが一致する唯一のsettled観測だけで分類。相関欠落・重複・取消理由欠落はunknown。strictと分類後の条件を別掲し、この通信条件だけで試験合格とはしない",
  };
}
