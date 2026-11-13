import { useState } from "react";
import type { AwardCeremony } from "../../api/contracts";

const AWARD_PAGE_SIZE = 5;

type ParticipantAwardStageProps = {
  ceremony: AwardCeremony | null;
  publicLabel?: string;
};

export function ParticipantAwardStage({ ceremony, publicLabel }: ParticipantAwardStageProps) {
  const [page, setPage] = useState(0);
  const rows = ceremony?.phase === "Announced" ? ceremony.top_rows : null;
  const awardee = rows?.find((row) => row.public_label === publicLabel);
  const pageCount = Math.max(1, Math.ceil((rows?.length ?? 0) / AWARD_PAGE_SIZE));
  const currentPage = Math.min(page, pageCount - 1);

  if (rows === null) {
    return (
      <section className="participant-award-stage participant-award-waiting" aria-labelledby="participant-award-title">
        <h2 id="participant-award-title">最終結果発表!!</h2>
      </section>
    );
  }

  if (awardee !== undefined) {
    return (
      <section className="participant-award-stage participant-award-winner" aria-labelledby="participant-award-title">
        <h2 id="participant-award-title"><span>あなたは</span><strong>{awardee.overall_rank}<small>位!!</small></strong></h2>
        <p className="participant-award-call">ステージ上に<br />お越しください!!</p>
      </section>
    );
  }

  return (
    <section className="participant-award-stage participant-award-ranking" aria-labelledby="participant-award-title">
      <h2 id="participant-award-title">最終ランキング</h2>
      {rows.length === 0 ? <p>ランキング対象者はいません。</p> : (
        <table className="participant-award-table" aria-label="最終順位・参加者ID">
          <thead><tr><th scope="col">順位</th><th scope="col">ID</th></tr></thead>
          <tbody>{rows.slice(currentPage * AWARD_PAGE_SIZE, (currentPage + 1) * AWARD_PAGE_SIZE).map((row) => (
            <tr key={row.public_label}>
              <td>{row.overall_rank}位</td><th scope="row">{row.public_label}</th>
            </tr>
          ))}</tbody>
        </table>
      )}
      {pageCount <= 1 ? null : (
        <nav className="participant-ranking-pagination participant-award-pagination" aria-label="表彰対象者のページ切り替え">
          <button type="button" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>前へ</button>
          <span aria-live="polite">{currentPage + 1} / {pageCount} ページ · 全{rows.length}人</span>
          <button type="button" disabled={currentPage === pageCount - 1} onClick={() => setPage(currentPage + 1)}>次へ</button>
        </nav>
      )}
    </section>
  );
}
