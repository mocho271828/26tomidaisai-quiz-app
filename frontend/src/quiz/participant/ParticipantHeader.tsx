import { FestivalLogo } from "../_components/FestivalLogo";

type ParticipantHeaderProps = { publicLabel?: string };

export function ParticipantHeader({ publicLabel }: ParticipantHeaderProps) {
  return (
    <header className="participant-header">
      <FestivalLogo />
      {publicLabel === undefined ? (
        <span className="participant-event-badge">ゲスト企画</span>
      ) : (
        <div className="participant-header-id"><span>あなたのID</span><strong>{publicLabel}</strong></div>
      )}
    </header>
  );
}
