import type { GeneratorWebSocketCloseCode } from "./metric-taxonomy.js";

export type GeneratorHttpStatusClass =
  | "client"
  | "server"
  | "success"
  | "unknown";

export type GeneratorWebSocketCloseClass = GeneratorWebSocketCloseCode;

/** HTTP 200だけを公開APIの成功とし、それ以外を応答の確実性で分類する。 */
export function classifyHttpStatus(status: number): GeneratorHttpStatusClass {
  if (!Number.isInteger(status)) {
    return "unknown";
  }
  if (status === 200) {
    return "success";
  }
  if (status >= 400 && status <= 499) {
    return "client";
  }
  if (status >= 500 && status <= 599) {
    return "server";
  }
  return "unknown";
}

/** 任意のclose codeを、runbookで判断できる固定集合へ縮約する。 */
export function classifyWebSocketCloseCode(
  code: number,
): GeneratorWebSocketCloseClass {
  switch (code) {
    case 1_000:
      return "1000";
    case 1_003:
      return "1003";
    case 1_008:
      return "1008";
    case 1_013:
      return "1013";
    case 4_401:
      return "4401";
    default:
      return "other";
  }
}
