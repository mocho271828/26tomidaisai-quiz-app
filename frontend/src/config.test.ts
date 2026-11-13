import { describe, expect, it } from "vitest";

import { parseAppConfig } from "./config";

describe("app config", () => {
  it("同一originのslash開始・末尾slashなしbase pathを受理する", () => {
    const config = parseAppConfig({
      VITE_API_BASE_URL: "/api/v1",
      VITE_WS_BASE_URL: "/api/v1",
    });

    expect(config.VITE_API_BASE_URL).toBe("/api/v1");
  });

  it.each(["//evil.example", "/", "/api/", "/api?next=//evil.example"])(
    "危険または連結不能なbase path %s を拒否する",
    (basePath) => {
      expect(() =>
        parseAppConfig({
          VITE_API_BASE_URL: basePath,
          VITE_WS_BASE_URL: "/api/v1",
        }),
      ).toThrow();
    },
  );
});
