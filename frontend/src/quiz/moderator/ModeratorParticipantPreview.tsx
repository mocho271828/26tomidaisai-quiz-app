import "./ModeratorParticipantPreview.css";

export function ModeratorParticipantPreview() {
  return (
    <section className="moderator-participant-preview" aria-label="参加者画面のプレビュー">
      <div className="moderator-preview-heading">
        <h2>参加者画面</h2>
        <p>参加者と同じ画面</p>
      </div>
      <div className="moderator-preview-shell" role="region" aria-label="参加者画面の表示内容">
        <iframe className="moderator-preview-frame" src="/?moderator-preview=1" title="参加者画面" />
      </div>
      <p className="moderator-preview-note">未参加なら自動登録します。回答は通常の参加者として集計されます。</p>
    </section>
  );
}
