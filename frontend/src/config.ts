import { z } from "zod";

export const appConfigSchema = z.object({
  VITE_API_BASE_URL: z
    .string()
    .regex(/^\/(?!\/)[A-Za-z0-9._~-]+(?:\/[A-Za-z0-9._~-]+)*$/u)
    .default("/api/v1"),
  VITE_WS_BASE_URL: z
    .string()
    .regex(/^\/(?!\/)[A-Za-z0-9._~-]+(?:\/[A-Za-z0-9._~-]+)*$/u)
    .default("/api/v1"),
});

export type AppConfig = z.infer<typeof appConfigSchema>;

export function parseAppConfig(value: unknown): AppConfig {
  return appConfigSchema.parse(value);
}

export const appConfig: AppConfig = parseAppConfig(import.meta.env);
