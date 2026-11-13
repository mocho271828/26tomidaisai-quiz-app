import { useState } from "react";

import type { OperatorLatestRanking, OperatorState } from "../../api/operatorContracts";
import type { OperatorResponseTimes } from "../../api/operatorResponseTimeContracts";
import { ModeratorPagination } from "./ModeratorPagination";
import { formatModeratorResponseTime, isModeratorRoundFinalized } from "./moderatorPresentation";

function FastestCorrectAnswer({ responseTimes, ranking }: {
  responseTimes: OperatorResponseTimes;
  ranking: OperatorLatestRanking;
}) {
  const [page, setPage] = useState(0);
  const winners = responseTimes.correct_answers.rows.filter((row) => row.rank === 1);
  const currentPage = Math.min(page, Math.max(0, winners.length - 1));
  const winner = winners[currentPage];
  if (winner === undefined) return <p className="moderator-result-empty">正答者はいません。</p>;
  const score = ranking.rows.find((row) => row.public_label === winner.public_label)?.total_points;

  return (
    <>
      <dl className="moderator-winner-details" aria-label="最速正答者の記録">
        <div><dt>参加者ID</dt><dd>{winner.public_label}</dd></div>
        <div><dt>回答時間</dt><dd>{formatModeratorResponseTime(winner.response_ms)}</dd></div>
        <div><dt>総合得点</dt><dd>{score === undefined ? "記録なし" : `${score.toLocaleString("ja-JP")}点`}</dd></div>
      </dl>
      <ModeratorPagination page={currentPage} pageCount={winners.length} count={winners.length}
        label="同率1位のページ切り替え" onPageChange={setPage} />
    </>
  );
}

export function ModeratorResponseTimes({ projection, responseTimes, ranking }: {
  projection: OperatorState;
  responseTimes: OperatorResponseTimes | null;
  ranking: OperatorLatestRanking | null;
}) {
  const round = projection.current_round;
  const isCurrentResult = round !== null && isModeratorRoundFinalized(round) &&
    ranking !== null && ranking.snapshot_version === projection.latest_ranking_snapshot_version &&
    ranking.round_id === round.round_id && responseTimes?.selected_round_id === round.round_id;

  return (
    <section className="moderator-card moderator-winner" aria-labelledby="moderator-winner-title">
      <h2 id="moderator-winner-title">今回の1位 <small>最速の正答者</small></h2>
      {isCurrentResult && responseTimes !== null && ranking !== null ? (
        <FastestCorrectAnswer key={`${projection.event_id}:${ranking.snapshot_version}`} responseTimes={responseTimes} ranking={ranking} />
      ) : (
        <p className="moderator-result-empty">{round === null ? "問題の開始をお待ちください。"
          : isModeratorRoundFinalized(round) ? "今回の結果を確認しています。" : "集計完了をお待ちください。"}</p>
      )}
    </section>
  );
}
