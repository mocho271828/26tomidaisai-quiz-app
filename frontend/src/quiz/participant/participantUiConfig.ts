import {
  parseParticipantUiConfig,
  type ParticipantUiConfig,
} from "./participantUiConfigSchema";

export {
  parseParticipantUiConfig,
  type ParticipantUiConfig,
} from "./participantUiConfigSchema";

export const participantUiConfig: ParticipantUiConfig =
  parseParticipantUiConfig(import.meta.env, window.location.hostname);
