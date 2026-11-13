import { useState } from "react";

import type {
  OperatorQuestion,
  OperatorRoundStatistics,
} from "../../api/operatorContracts";
import type { OperatorRankingState } from "./useOperatorRanking";
import "../_components/operatorStatistics.css";

type OperatorStatisticsProps = {
  isResultFinalized: boolean;
  options: OperatorQuestion["options"];
  rankingState: OperatorRankingState;
  statistics: OperatorRoundStatistics;
};

const responseTimeFormatter = new Intl.NumberFormat("ja-JP", {
  maximumFractionDigits: 2,
  minimumFractionDigits: 2,
});

function formatResponseTime(milliseconds: number | null): string {
  if (milliseconds === null) {
    return "回答なし";
  }
  return `${responseTimeFormatter.format(milliseconds / 1000)}秒`;
}

function OperatorRanking({
  rankingState,
}: {
  rankingState: OperatorRankingState;
}) {
  const [search, setSearch] = useState("");
  const query = search.trim().toUpperCase();
  const rows = rankingState.latestRanking?.rows ?? [];
  const filteredRows = rows.filter((row) =>
    row.public_label.toUpperCase().includes(query),
  );

  return (
    <section
      className="operator-card operator-ranking"
      aria-labelledby="operator-ranking-statistics-title"
    >
      <div className="operator-statistics-heading">
        <h2 id="operator-ranking-statistics-title">総合ランキング</h2>
        <span className="operator-statistics-badge">運営専用</span>
      </div>
      <p className="operator-ranking-description">
        {rankingState.status === "ready"
          ? "最新の集計結果 · 練習を除く"
          : "結果の自動集計後に更新します。"}
      </p>
      {rankingState.status === "idle" ? (
        <p className="operator-statistics-empty">
          今回の順位は未確定です。
        </p>
      ) : rankingState.status === "loading" ? (
        <p className="operator-statistics-empty" role="status">
          総合ランキングを読み込んでいます…
        </p>
      ) : rankingState.status === "error" ? (
        <p className="operator-statistics-empty operator-statistics-error" role="alert">
          総合ランキングを取得できませんでした。時間をおいて画面を再読み込みしてください。
        </p>
      ) : rows.length === 0 ? (
        <p className="operator-statistics-empty">
          総合ランキングはまだありません。
        </p>
      ) : (
        <>
          <div className="operator-ranking-toolbar">
            <label className="sr-only" htmlFor="operator-ranking-search">
              参加者IDで絞り込み
            </label>
            <input
              autoComplete="off"
              id="operator-ranking-search"
              maxLength={3}
              onChange={(event) => setSearch(event.currentTarget.value)}
              placeholder="IDで絞り込み"
              spellCheck={false}
              type="search"
              value={search}
            />
            <span className="operator-ranking-count" role="status">
              {query.length === 0
                ? `全${rows.length.toLocaleString("ja-JP")}人`
                : `${filteredRows.length.toLocaleString("ja-JP")} / ${rows.length.toLocaleString("ja-JP")}人`}
            </span>
          </div>
          {filteredRows.length === 0 ? (
            <p className="operator-statistics-empty">
              該当するIDはありません。
            </p>
          ) : (
            <div
              className="operator-ranking-scroll"
              tabIndex={0}
              role="region"
              aria-label="総合ランキング一覧"
            >
              <table aria-label="順位別の合計得点">
                <thead>
                  <tr>
                    <th scope="col">順位</th>
                    <th scope="col">ID</th>
                    <th scope="col">総合得点</th>
                    <th scope="col">正解数</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredRows.map((row) => (
                    <tr
                      className={row.overall_rank <= 3 ? "operator-ranking-top" : undefined}
                      key={row.public_label}
                    >
                      <td>{row.overall_rank.toLocaleString("ja-JP")}<small>位</small></td>
                      <th scope="row">{row.public_label}</th>
                      <td>{row.total_points.toLocaleString("ja-JP")}<small>点</small></td>
                      <td>{row.correct_count.toLocaleString("ja-JP")}<small>問</small></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </section>
  );
}

export function OperatorStatistics({
  isResultFinalized,
  options,
  rankingState,
  statistics,
}: OperatorStatisticsProps) {
  const statisticsByOption = new Map(
    statistics.options.map((option) => [option.option_id, option]),
  );
  const correctAnswerCount = options.reduce(
    (total, option) => total + (option.is_correct
      ? statisticsByOption.get(option.option_id)?.answer_count ?? 0
      : 0),
    0,
  );
  const correctAnswerRate = statistics.answer_count === 0
    ? "回答なし"
    : `${Math.round(correctAnswerCount / statistics.answer_count * 100)}%`;

  return (
    <>
      <section
        className="operator-card operator-distribution"
        aria-labelledby="operator-option-statistics-title"
      >
        <div className="operator-statistics-heading">
          <h2 id="operator-option-statistics-title">回答分布</h2>
          <span className="operator-statistics-badge">
            {isResultFinalized ? "確定" : "未確定"}
          </span>
        </div>
        {options.length === 0 ? (
          <p className="operator-statistics-empty">選択肢はありません。</p>
        ) : (
          <ul
            className="operator-distribution-rows"
            aria-label="選択肢別の回答数と平均速度"
          >
            {options.map((option) => {
              const optionStatistics = statisticsByOption.get(option.option_id);
              const answerCount = optionStatistics?.answer_count ?? 0;
              const percentage = statistics.answer_count === 0
                ? 0
                : Math.round(answerCount / statistics.answer_count * 100);
              return (
                <li className="operator-distribution-row" key={option.option_id}>
                  <span className="operator-distribution-label">{option.label}</span>
                  <span className="operator-distribution-name">
                    {option.content}
                    {option.is_correct ? <small>正解</small> : null}
                  </span>
                  <span className="operator-distribution-track" aria-hidden="true">
                    <span style={{ width: `${percentage}%` }} />
                  </span>
                  <strong>{answerCount.toLocaleString("ja-JP")}件</strong>
                  <small>{percentage}%</small>
                  <span className="operator-distribution-average">
                    平均回答時間 {formatResponseTime(optionStatistics?.average_response_ms ?? null)}
                  </span>
                </li>
              );
            })}
          </ul>
        )}
        <dl className="operator-statistics-summary" aria-atomic="true" aria-live="polite">
          <div><dt>回答数</dt><dd>{statistics.answer_count.toLocaleString("ja-JP")}件</dd></div>
          <div><dt>正答率</dt><dd>{isResultFinalized ? correctAnswerRate : "未確定"}</dd></div>
          <div><dt>平均回答時間</dt><dd>{formatResponseTime(statistics.average_response_ms)}</dd></div>
        </dl>
        {statistics.answer_count === 0 ? (
          <p className="operator-distribution-note">
            回答が保存されると、件数と平均速度を表示します。
          </p>
        ) : null}
      </section>
      <OperatorRanking rankingState={rankingState} />
    </>
  );
}
