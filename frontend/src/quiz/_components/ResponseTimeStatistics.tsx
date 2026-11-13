import type { OperatorResponseTimes as ResponseTimeData } from "../../api/operatorResponseTimeContracts";
import "./operatorStatistics.css";
import "./responseTimeStatistics.css";

type ResponseTimeGroup = ResponseTimeData["all_answers"];
export type ResponseTimeAudience = "all" | "correct";

const responseTimeFormatter = new Intl.NumberFormat("ja-JP", {
  maximumFractionDigits: 3,
  minimumFractionDigits: 3,
});

function formatResponseTime(milliseconds: number | null): string {
  return milliseconds === null
    ? "回答なし"
    : `${responseTimeFormatter.format(milliseconds / 1000)}秒`;
}

function ResponseTimeRanking({
  audience,
  group,
  search,
}: {
  audience: ResponseTimeAudience;
  group: ResponseTimeGroup;
  search: string;
}) {
  const query = search.trim().toUpperCase();
  const rows = group.rows.filter((row) => row.public_label.toUpperCase().includes(query));

  return (
    <>
      <dl className="operator-statistics-summary" aria-label="回答速度の統計">
        <div><dt>回答数</dt><dd>{group.answer_count.toLocaleString("ja-JP")}件</dd></div>
        <div><dt>平均</dt><dd>{formatResponseTime(group.average_response_ms)}</dd></div>
        <div><dt>中央値</dt><dd>{formatResponseTime(group.median_response_ms)}</dd></div>
        <div><dt>最速</dt><dd>{formatResponseTime(group.fastest_response_ms)}</dd></div>
        <div><dt>最遅</dt><dd>{formatResponseTime(group.slowest_response_ms)}</dd></div>
      </dl>
      <p className="operator-response-times-count" role="status">
        {query.length === 0
          ? `全${group.rows.length.toLocaleString("ja-JP")}人`
          : `${rows.length.toLocaleString("ja-JP")} / ${group.rows.length.toLocaleString("ja-JP")}人`}
      </p>
      {group.answer_count === 0 ? (
        <p className="operator-statistics-empty">
          {audience === "correct"
            ? "この条件に一致する正答者はいません。"
            : "この条件に一致する回答はありません。"}
        </p>
      ) : rows.length === 0 ? (
        <p className="operator-statistics-empty">該当するIDはありません。</p>
      ) : (
        <div
          className="operator-ranking-scroll"
          tabIndex={0}
          role="region"
          aria-label="回答速度ランキング一覧"
        >
          <table aria-label={audience === "correct" ? "正答者の回答速度ランキング" : "全回答者の回答速度ランキング"}>
            <thead>
              <tr>
                <th scope="col">順位</th>
                <th scope="col">ID</th>
                <th scope="col">回答時間</th>
                <th scope="col">正誤</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr
                  className={row.rank <= 3 ? "operator-ranking-top" : undefined}
                  key={row.public_label}
                >
                  <td>{row.rank.toLocaleString("ja-JP")}<small>位</small></td>
                  <th scope="row">{row.public_label}</th>
                  <td>{formatResponseTime(row.response_ms)}</td>
                  <td>{row.is_correct ? "正答" : "誤答"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

type ResponseTimeStatisticsProps = {
  data: ResponseTimeData;
  audience: ResponseTimeAudience;
  search: string;
  searchId: string;
  onAudienceChange?: (audience: ResponseTimeAudience) => void;
  onSearchChange: (search: string) => void;
};

export function ResponseTimeStatistics({
  data,
  audience,
  search,
  searchId,
  onAudienceChange,
  onSearchChange,
}: ResponseTimeStatisticsProps) {
  return (
    <>
      {onAudienceChange ? <div className="operator-response-times-audience" role="group" aria-label="ランキングの対象">
        <button
          aria-pressed={audience === "all"}
          className="secondary-button"
          onClick={() => onAudienceChange("all")}
          type="button"
        >
          全回答者
        </button>
        <button
          aria-pressed={audience === "correct"}
          className="secondary-button"
          onClick={() => onAudienceChange("correct")}
          type="button"
        >
          正答者のみ
        </button>
      </div> : null}
      <div className="operator-ranking-toolbar">
        <label className="sr-only" htmlFor={searchId}>回答速度を参加者IDで絞り込み</label>
        <input
          autoComplete="off"
          id={searchId}
          maxLength={3}
          onChange={(event) => onSearchChange(event.currentTarget.value)}
          placeholder="IDで絞り込み"
          spellCheck={false}
          type="search"
          value={search}
        />
      </div>
      <ResponseTimeRanking
        audience={audience}
        group={audience === "correct" ? data.correct_answers : data.all_answers}
        search={search}
      />
      <p className="operator-ranking-description">
        採点上の速度順位とは別の統計です。同じミリ秒は同順位（1位、1位、3位）。ID検索は順位・統計値に影響しません。
      </p>
    </>
  );
}
