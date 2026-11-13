import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    alias: {
      "k6/http": fileURLToPath(new URL("./tests/stubs/k6-http.ts", import.meta.url)),
      "k6/metrics": fileURLToPath(
        new URL("./tests/stubs/k6-metrics.ts", import.meta.url),
      ),
    },
  },
  test: {
    environment: "node",
    include: ["tests/**/*.test.ts"],
  },
});
