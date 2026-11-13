import type { ZodType } from "zod";

import { apiErrorSchema, type ApiError } from "./contracts";
import { appConfig } from "../config";
import { beginRequestObservation } from "./requestObservation";
import { cancellationReason } from "./cancellation";
import type { ApiObservationOperation } from "../quiz/_utils/browserObservation";

export class ApiResponseError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, error: ApiError) {
    super(error.message);
    this.name = "ApiResponseError";
    this.status = status;
    this.code = error.code;
  }
}

export class ApiContractError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "ApiContractError";
  }
}

export class ApiTransportError extends Error {
  constructor(cause: unknown) {
    super("APIへ接続できませんでした。", { cause });
    this.name = "ApiTransportError";
  }
}

type JsonRequestInit = Omit<RequestInit, "body"> & {
  body?: unknown;
  observationOperation?: ApiObservationOperation;
};

function apiPath(path: string): string {
  return `${appConfig.VITE_API_BASE_URL}${path}`;
}

async function fetchApi(path: string, init: JsonRequestInit): Promise<Response> {
  const requestInit = { ...init };
  delete requestInit.observationOperation;
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }

  try {
    return await fetch(apiPath(path), {
      ...requestInit,
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
      credentials: "same-origin",
      headers,
    });
  } catch (error: unknown) {
    if (error instanceof DOMException && (error.name === "AbortError" || error.name === "TimeoutError")) {
      throw error;
    }
    throw new ApiTransportError(error);
  }
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch (error: unknown) {
    if (error instanceof DOMException && (error.name === "AbortError" || error.name === "TimeoutError")) {
      throw error;
    }
    throw new ApiContractError(
      `API応答をJSONとして解釈できませんでした（HTTP ${response.status}）。`,
      { cause: error },
    );
  }
}

async function throwResponseError(response: Response): Promise<never> {
  const parsed = apiErrorSchema.safeParse(await readJson(response));
  if (!parsed.success) {
    throw new ApiContractError(
      `APIエラー応答が契約に一致しません（HTTP ${response.status}）。`,
    );
  }
  throw new ApiResponseError(response.status, parsed.data);
}

export async function requestJson<T>(
  path: string,
  schema: ZodType<T>,
  init: JsonRequestInit = {},
): Promise<T> {
  const observation = beginRequestObservation(init.observationOperation, apiPath(path));
  const headers = new Headers(init.headers);
  if (observation !== undefined) {
    // ページ内連番だけを渡し、headers受信前の中断もbrowser/runnerで対応付ける。
    headers.set("X-Quiz-Observation-Sequence", String(observation.requestSequence));
  }
  try {
    const response = await fetchApi(path, { ...init, headers });
    observation?.headers(response);
    if (!response.ok) {
      return await throwResponseError(response);
    }
    const value = await readJson(response);
    observation?.body();
    const parsed = schema.safeParse(value);
    if (!parsed.success) {
      throw new ApiContractError(
        `API成功応答が契約に一致しません（HTTP ${response.status}）。`,
      );
    }
    observation?.settled("validated");
    return parsed.data;
  } catch (error: unknown) {
    const timedOut = (error instanceof DOMException && error.name === "TimeoutError") ||
      (init.signal?.aborted === true && init.signal.reason instanceof DOMException && init.signal.reason.name === "TimeoutError");
    observation?.settled(timedOut ? "timeout" : error instanceof ApiResponseError ? "http_error"
      : error instanceof ApiContractError ? "contract_error"
        : error instanceof DOMException && error.name === "AbortError" ? "aborted" : "transport_error",
    error instanceof ApiResponseError ? error.code : undefined, cancellationReason(init.signal));
    throw error;
  }
}

export async function requestEmpty(
  path: string,
  init: JsonRequestInit = {},
): Promise<void> {
  const response = await fetchApi(path, init);
  if (!response.ok) {
    return throwResponseError(response);
  }
  if (response.status !== 204) {
    throw new ApiContractError(
      `空のAPI応答に想定外のstatusが返りました（HTTP ${response.status}）。`,
    );
  }
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiResponseError) {
    return error.message;
  }
  if (error instanceof ApiTransportError) {
    return error.message;
  }
  if (error instanceof ApiContractError) {
    return "受信データを安全に確認できませんでした。画面を更新してください。";
  }
  return "処理を完了できませんでした。";
}
