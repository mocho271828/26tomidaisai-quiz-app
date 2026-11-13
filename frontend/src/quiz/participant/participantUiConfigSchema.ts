import { z } from "zod";

const optionalIsoDateTimeSchema = z.preprocess(
  (value) => (value === "" || value === undefined ? null : value),
  z.iso.datetime({ offset: true }).nullable(),
);

function isSafeExitUrl(value: string, appHostname?: string): boolean {
  try {
    const parsed = new URL(value);
    return (
      parsed.protocol === "https:" &&
      parsed.username === "" &&
      parsed.password === "" &&
      parsed.hostname.replace(/\.+$/u, "") !== appHostname?.toLowerCase().replace(/\.+$/u, "")
    );
  } catch {
    return false;
  }
}

export type ParticipantUiConfig = {
  eventStartAt: string | null;
  homeUrl: string | null;
  surveyUrl: string | null;
};

export function parseParticipantUiConfig(
  value: unknown,
  appHostname?: string,
): ParticipantUiConfig {
  const optionalLink = z.preprocess(
    (input) => (input === "" || input === undefined ? null : input),
    z.string().min(1).max(2_048)
      .refine((url) => isSafeExitUrl(url, appHostname), "external HTTPS URL required")
      .nullable(),
  );
  const environment = z.object({
    VITE_EVENT_START_AT: optionalIsoDateTimeSchema.default(null),
    VITE_HOME_URL: optionalLink.default(null),
    VITE_SURVEY_URL: optionalLink.default(null),
  }).parse(value);
  return {
    eventStartAt: environment.VITE_EVENT_START_AT,
    homeUrl: environment.VITE_HOME_URL,
    surveyUrl: environment.VITE_SURVEY_URL,
  };
}
