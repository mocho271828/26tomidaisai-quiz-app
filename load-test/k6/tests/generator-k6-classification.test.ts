import { describe, expect, it } from "vitest";

import {
  classifyHttpStatus,
  classifyWebSocketCloseCode,
} from "../src/generator/k6-classification.js";

describe("k6 HTTP status classification", () => {
  it("200だけをsuccessに分類する", () => {
    expect(classifyHttpStatus(200)).toBe("success");
  });

  it.each([400, 401, 418, 499])("4xx %i をclientに分類する", (status) => {
    expect(classifyHttpStatus(status)).toBe("client");
  });

  it.each([500, 503, 599])("5xx %i をserverに分類する", (status) => {
    expect(classifyHttpStatus(status)).toBe("server");
  });

  it.each([
    0,
    100,
    199,
    201,
    204,
    299,
    300,
    399,
    600,
    -1,
    200.5,
    Number.NaN,
    Number.POSITIVE_INFINITY,
  ])("成功・client・serverのいずれでもない %s をunknownに分類する", (status) => {
    expect(classifyHttpStatus(status)).toBe("unknown");
  });
});

describe("k6 WebSocket close classification", () => {
  it.each([
    [1_000, "1000"],
    [1_003, "1003"],
    [1_008, "1008"],
    [1_013, "1013"],
    [4_401, "4401"],
  ] as const)("close code %i を専用bucket %sへ分類する", (code, bucket) => {
    expect(classifyWebSocketCloseCode(code)).toBe(bucket);
  });

  it.each([0, 999, 1_001, 1_009, 4_400, 4_402, Number.NaN])(
    "固定集合外のclose code %sをotherへ分類する",
    (code) => {
      expect(classifyWebSocketCloseCode(code)).toBe("other");
    },
  );
});
