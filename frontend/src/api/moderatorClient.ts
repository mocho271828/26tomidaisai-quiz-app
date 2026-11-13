import { requestJson } from "./client";
import { participantCommonStateSchema, type ParticipantCommonState } from "./realtimeContracts";

export function getModeratorParticipantPreview(signal: AbortSignal): Promise<ParticipantCommonState> {
  return requestJson("/operator/participant-preview", participantCommonStateSchema, { signal });
}
