import { TommyMascot } from "./TommyMascot";
import { roundLabel } from "../_utils/roundPresentation";

export function ParticipantRoundHeader({ roundNumber }: { roundNumber: number }) {
  return (
    <header className="participant-round-header">
      <strong className="participant-round-badge" data-round-number={roundNumber}>
        {roundLabel(roundNumber)}
      </strong>
      <TommyMascot size="small" variant="thinking" />
    </header>
  );
}
