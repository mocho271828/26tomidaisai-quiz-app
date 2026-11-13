export function ParticipantIdTicket({ publicLabel }: { publicLabel: string }) {
  return (
    <div className="participant-ticket">
      <span>あなたの参加者ID</span><strong>{publicLabel}</strong>
    </div>
  );
}
