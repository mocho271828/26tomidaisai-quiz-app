import react from "@vitejs/plugin-react";
import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";

import { parseParticipantUiConfig } from "./src/quiz/participant/participantUiConfigSchema";

export default defineConfig(({ mode }) => {
  const environment = loadEnv(mode, process.cwd(), "");
  parseParticipantUiConfig(environment, environment.AWS_REVIEW_DOMAIN_NAME);

  return {
    plugins: [react()],
    server: {
      proxy: {
        "/api": "http://localhost:8000",
        "/ws": { target: "ws://localhost:8000", ws: true },
      },
    },
    test: {
      environment: "jsdom",
      setupFiles: "./src/test/setup.ts",
      css: true,
    },
  };
});
