import { useState } from "react";
import type { ParticipantRanking } from "../../api/contracts";
import trophyWarning from "../../assets/participant-2026/trophy-warning.svg";

type ParticipantRankingCardProps = {
  publicLabel: string;
  ranking: ParticipantRanking;
};

const RANKING_PAGE_SIZE = 3;

export function ParticipantRankingCard({ publicLabel, ranking }: ParticipantRankingCardProps) {
  const [page, setPage] = useState(0);
  const topRows = ranking.top_rows;
  const pageCount = Math.max(1, Math.ceil((topRows?.length ?? 0) / RANKING_PAGE_SIZE));
  // 再公開時に件数が減った場合も、有効なページへ即座に戻す。
  const currentPage = Math.min(page, pageCount - 1);
  const visibleRows = topRows?.slice(currentPage * RANKING_PAGE_SIZE, (currentPage + 1) * RANKING_PAGE_SIZE);

  return (
    <section aria-labelledby="participant-ranking-title" className="participant-ranking-result">
      <h2 id="participant-ranking-title" className="sr-only">現在のランキング</h2>
      <div className="participant-ranking-summary">
        <p>あなたの総合順位</p>
        <strong>{ranking.overall_rank}<span>位</span></strong>
        <p>ID {publicLabel} · {ranking.total_points.toLocaleString("ja-JP")} pt</p>
      </div>
      {topRows === null ? null : (
        <section className="participant-ranking-top" aria-labelledby="participant-top-title">
          <h3 id="participant-top-title">みんなのトップ3</h3>
          {topRows.length === 0 ? <p className="participant-ranking-empty">公開できる順位はまだありません。</p> : (
            <ol className="participant-ranking-list" start={currentPage * RANKING_PAGE_SIZE + 1}>
              {visibleRows?.map((row) => (
                <li key={row.public_label}>
                  <span aria-label={`${row.overall_rank}位`}>{row.overall_rank}</span>
                  <strong>{row.public_label}</strong>
                  <span className="participant-ranking-points">{row.total_points.toLocaleString("ja-JP")} <small>pt</small></span>
                  {row.overall_rank === 1 ? <img alt="" src={trophyWarning} /> : null}
                </li>
              ))}
            </ol>
          )}
          {pageCount <= 1 ? null : (
            <nav className="participant-ranking-pagination" aria-label="トップ3のページ切り替え">
              <button type="button" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>前へ</button>
              <span aria-live="polite">{currentPage + 1} / {pageCount} ページ · 全{topRows.length}人</span>
              <button type="button" disabled={currentPage === pageCount - 1} onClick={() => setPage(currentPage + 1)}>次へ</button>
            </nav>
          )}
        </section>
      )}
      <div className="participant-ranking-self">
        <span aria-label={`${ranking.overall_rank}位`}>{ranking.overall_rank}</span>
        <strong>{publicLabel}</strong><small>あなた</small>
        <span className="participant-ranking-points">{ranking.total_points.toLocaleString("ja-JP")} <small>pt</small></span>
      </div>
      <p className="participant-ranking-note">練習問題の得点は含みません。<br />同点の場合は、正解数・正解回答時間で順位が決まります。</p>
    </section>
  );
}
