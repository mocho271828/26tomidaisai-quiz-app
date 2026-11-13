import type { Page, Request } from "playwright";
import type { Evidence } from "./evidence.js";

type Mutation = "prepare" | "schedule" | "publish" | "advance" | "complete" | "visibility" | "other";

export function operatorMutationClass(raw: string): Mutation | null {
  if (!URL.canParse(raw)) return null;
  const path = new URL(raw).pathname;
  const round = /^\/api\/v1\/operator\/rounds\/[^/]+\/(prepare|schedule|publish-results|advance|complete-event|retry-results|question)$/.exec(path);
  if (round) {
    const operation = round[1];
    if (operation === "complete-event") return "complete";
    if (operation === "publish-results") return "publish";
    return operation === "prepare" || operation === "schedule" || operation === "advance" ? operation : "other";
  }
  if (path === "/api/v1/operator/result-visibility") return "visibility";
  return null;
}

export function observeOperatorMutations(page: Page, evidence: Pick<Evidence, "event">): () => Promise<void> {
  const started = new WeakMap<Request, {at: number; route: Mutation}>();
  const pending = new Set<Promise<void>>();
  const request = (value: Request): void => {
    const route = operatorMutationClass(value.url());
    if (value.method() === "POST" && route !== null) started.set(value, {at: performance.now(), route});
  };
  const finished = (value: Request): void => {
    const initial = started.get(value);
    if (!initial) return;
    started.delete(value);
    const durationMs = performance.now() - initial.at;
    const observation = value.response().then(response => {
      const status = response?.status();
      evidence.event({event: "operator_http", route: initial.route, durationMs, status,
        outcome: status === undefined ? "transport_error" : status >= 200 && status < 300 ? "success" : "http_error"});
    }).catch(() => evidence.event({event: "operator_http_observation_failed"}));
    pending.add(observation);
    void observation.finally(() => pending.delete(observation));
  };
  const failed = (value: Request): void => {
    const initial = started.get(value);
    if (!initial) return;
    started.delete(value);
    evidence.event({event: "operator_http", route: initial.route,
      durationMs: performance.now() - initial.at, outcome: "transport_error"});
  };
  page.on("request", request);
  page.on("requestfinished", finished);
  page.on("requestfailed", failed);
  return async () => {
    page.off("request", request);
    page.off("requestfinished", finished);
    page.off("requestfailed", failed);
    await Promise.all(pending);
  };
}
