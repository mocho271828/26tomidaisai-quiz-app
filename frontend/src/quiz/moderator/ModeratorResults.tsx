import { useState } from "react";

import type { OperatorLatestRanking, OperatorState } from "../../api/operatorContracts";
import type { OperatorResponseTimes } from "../../api/operatorResponseTimeContracts";
import { roundLabel } from "../_utils/roundPresentation";
import { ModeratorPagination } from "./ModeratorPagination";
import { formatModeratorResponseTime, isModeratorRoundFinalized } from "./moderatorPresentation";

const RANKING_PAGE_SIZE = 5;
type ModeratorResultsProps = {
  projection: OperatorState;
  ranking: OperatorLatestRanking | null;
  responseTimes: OperatorResponseTimes | null;
};

function ResponseTimeSummary({ data, hasSnapshot }: { data: OperatorResponseTimes | null; hasSnapshot: boolean }) {
  const selectedRound = data?.rounds.find((round) => round.round_id === data.selected_round_id);
  if (data === null || selectedRound === undefined) {
    return <p className="moderator-statistics-note">{hasSnapshot ? "回答時間を確認しています。" : "回答時間は集計後に表示します。"}</p>;
  }
  const group = data.correct_answers;
  return (
    <>
      <p className="moderator-statistics-note">{roundLabel(selectedRound.round_number)}・正答者{group.answer_count.toLocaleString("ja-JP")}人の回答時間</p>
      <dl className="moderator-time-summary" aria-label="正答者の回答時間の統計">
        <div><dt>平均</dt><dd>{formatModeratorResponseTime(group.average_response_ms)}</dd></div>
        <div><dt>最速</dt><dd>{formatModeratorResponseTime(group.fastest_response_ms)}</dd></div>
        <div><dt>最遅</dt><dd>{formatModeratorResponseTime(group.slowest_response_ms)}</dd></div>
        <div><dt>中央値</dt><dd>{formatModeratorResponseTime(group.median_response_ms)}</dd></div>
      </dl>
    </>
  );
}

function AnswerDistribution({ round, responseTimes, hasSnapshot }: {
  round: OperatorState["current_round"];
  responseTimes: OperatorResponseTimes | null;
  hasSnapshot: boolean;
}) {
  const statisticsByOption = new Map(round?.statistics.options.map((option) => [option.option_id, option]));

  return (
    <section className="moderator-card moderator-distribution" aria-labelledby="moderator-distribution-title">
      <h2 id="moderator-distribution-title">回答分布</h2>
      <ResponseTimeSummary data={responseTimes} hasSnapshot={hasSnapshot} />
      {round === null || !isModeratorRoundFinalized(round) ? (
        <p className="moderator-result-empty">集計完了をお待ちください。</p>
      ) : round.statistics.answer_count === 0 ? (
        <p className="moderator-result-empty">回答はありません。</p>
      ) : (
        <>
          <p className="moderator-result-note">今回の回答・全 {round.statistics.answer_count.toLocaleString("ja-JP")}件</p>
          <ul className="moderator-distribution-rows" aria-label="選択肢別の回答数と割合">
            {round.question.options.map((option) => {
              const statistics = statisticsByOption.get(option.option_id);
              if (statistics === undefined) return null;
              const percentage = Math.round(statistics.answer_count / round.statistics.answer_count * 100);
              return (
                <li className="moderator-distribution-row" key={option.option_id} aria-label={`${option.label} ${option.content}`}>
                  <span className="moderator-option-label">{option.label}</span>
                  <div className="moderator-distribution-track" aria-hidden="true"><span style={{ width: `${percentage}%` }} /></div>
                  <strong className="moderator-distribution-count">{statistics.answer_count.toLocaleString("ja-JP")}件</strong>
                  <span className="moderator-distribution-percentage">{percentage}%</span>
                </li>
              );
            })}
          </ul>
        </>
      )}
    </section>
  );
}

function Ranking({ ranking, hasRankingSnapshot }: {
  ranking: OperatorLatestRanking | null;
  hasRankingSnapshot: boolean;
}) {
  const [page, setPage] = useState(0);
  const rows = ranking?.rows.filter((row) => row.overall_rank <= 5)
    .sort((left, right) => left.overall_rank - right.overall_rank || left.public_label.localeCompare(right.public_label));
  const pageCount = Math.max(1, Math.ceil((rows?.length ?? 0) / RANKING_PAGE_SIZE));
  const currentPage = Math.min(page, pageCount - 1);

  return (
    <section className="moderator-card moderator-final-ranking" aria-labelledby="moderator-ranking-title">
      <h2 id="moderator-ranking-title">ランキング TOP5</h2>
      {rows === undefined ? (
        <p className="moderator-result-empty">{hasRankingSnapshot ? "ランキングを確認しています。" : "ランキングはまだありません。"}</p>
      ) : rows.length === 0 ? (
        <p className="moderator-result-empty">ランキング対象者はいません。</p>
      ) : (
        <>
          <div className="moderator-ranking-list" role="region" aria-label="ランキング一覧">
            <table className="moderator-ranking-table" aria-label="順位・参加者ID・得点">
              <thead><tr><th scope="col">順位</th><th scope="col">ID</th><th scope="col">総合得点</th></tr></thead>
              <tbody>
                {rows.slice(currentPage * RANKING_PAGE_SIZE, (currentPage + 1) * RANKING_PAGE_SIZE).map((row) => (
                  <tr key={row.public_label}>
                    <td>{row.overall_rank}位</td><th scope="row">{row.public_label}</th>
                    <td>{row.total_points.toLocaleString("ja-JP")}点</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <ModeratorPagination page={currentPage} pageCount={pageCount} count={rows.length}
            label="ランキングのページ切り替え" onPageChange={setPage} />
        </>
      )}
    </section>
  );
}

export function ModeratorResults({ projection, ranking, responseTimes }: ModeratorResultsProps) {
  // 次問の準備中も確定順位を保ち、新しい集計版の取得中に旧版を表示しない。
  const latestRanking = ranking?.snapshot_version === projection.latest_ranking_snapshot_version ? ranking : null;
  const latestResponseTimes = latestRanking !== null && responseTimes?.selected_round_id === latestRanking.round_id ? responseTimes : null;

  return (
    <>
      <AnswerDistribution round={projection.current_round} responseTimes={latestResponseTimes}
        hasSnapshot={projection.latest_ranking_snapshot_version !== null} />
      <Ranking key={`${projection.event_id}:${projection.latest_ranking_snapshot_version}`} ranking={latestRanking}
        hasRankingSnapshot={projection.latest_ranking_snapshot_version !== null} />
    </>
  );
}
