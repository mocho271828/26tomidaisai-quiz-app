export interface StubHttpParams {
  readonly headers?: Readonly<Record<string, string>>;
  readonly jar?: unknown;
  readonly redirects?: number;
  readonly responseCallback?: unknown;
  readonly responseType?: "binary" | "none" | "text";
  readonly tags?: Readonly<Record<string, string>>;
  readonly timeout?: number | string;
}

export interface StubHttpCall {
  readonly method: string;
  readonly url: string;
  readonly body: unknown;
  readonly params: StubHttpParams;
}

export interface StubHttpResponse {
  readonly status: number;
  readonly body: ArrayBuffer | string;
  readonly headers: Readonly<Record<string, string>>;
}

type StubHttpResponseInput = Omit<StubHttpResponse, "headers"> & {
  readonly headers?: Readonly<Record<string, string>>;
};

type StubHttpOutcome =
  | { readonly kind: "response"; readonly response: StubHttpResponse }
  | { readonly kind: "transport_failure" };

const calls: StubHttpCall[] = [];
const outcomes: StubHttpOutcome[] = [];

export const expectedStatusesCallback = Object.freeze({
  kind: "k6_expected_statuses",
  statuses: Object.freeze([200]),
});

export function resetHttpStub(): void {
  calls.length = 0;
  outcomes.length = 0;
}

export function enqueueHttpResponse(response: StubHttpResponseInput): void {
  outcomes.push({
    kind: "response",
    response: { ...response, headers: response.headers ?? {} },
  });
}

export function enqueueHttpTransportFailure(): void {
  outcomes.push({ kind: "transport_failure" });
}

export function readHttpCalls(): readonly StubHttpCall[] {
  return calls;
}

function request(
  method: string,
  url: string,
  body: unknown,
  params: StubHttpParams = {},
): StubHttpResponse {
  calls.push({ method, url, body, params });
  const outcome = outcomes.shift();
  if (outcome === undefined || outcome.kind === "transport_failure") {
    throw new Error("k6_http_stub_transport_failure");
  }
  return outcome.response;
}

function get(url: string, params: StubHttpParams = {}): StubHttpResponse {
  return request("GET", url, null, params);
}

function expectedStatuses(...statuses: readonly number[]): unknown {
  if (statuses.length !== 1 || statuses[0] !== 200) {
    throw new Error("k6_http_stub_expected_statuses_invalid");
  }
  return expectedStatusesCallback;
}

export class CookieJar {
  readonly scope = "fresh-test-jar";
}

export default { CookieJar, expectedStatuses, get, request };
