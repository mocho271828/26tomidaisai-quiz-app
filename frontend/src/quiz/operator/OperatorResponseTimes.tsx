import { useState } from "react";

import { ResponseTimeStatistics, type ResponseTimeAudience } from "../_components/ResponseTimeStatistics";
import { roundLabel } from "../_utils/roundPresentation";
import { useOperatorResponseTimes } from "./useOperatorResponseTimes";
import "./operatorResponseTimes.css";

export function OperatorResponseTimes({ snapshotVersion }: { snapshotVersion: number | null }) {
  const state = useOperatorResponseTimes(snapshotVersion);
  const [audience, setAudience] = useState<ResponseTimeAudience>("all");
  const [search, setSearch] = useState("");
  const rounds = state.data?.rounds ?? [];
  const selectedRound = rounds.find((round) => round.round_id === state.data?.selected_round_id);
  const hasRounds = rounds.length > 0;
  const isLoading = state.status === "loading";
  const areFiltersDisabled = state.status === "idle" || isLoading;
  const isSelectedRoundMissing = state.roundId !== null && !rounds.some((round) => round.round_id === state.roundId);

  return (
    <section
      className="operator-card operator-ranking operator-response-times"
      aria-labelledby="operator-response-times-title"
    >
      <div className="operator-statistics-heading">
        <h2 id="operator-response-times-title">回答速度</h2>
        <span className="operator-statistics-badge">運営専用</span>
      </div>
      <p className="operator-ranking-description">
        確定済みの問題を、{state.data?.timing_basis === "client_monotonic_v1" ? "開始から選択までの端末計測時間" : "開始からサーバー受付までの時間"}で比較します。練習も確認できます。
      </p>
      <div className="operator-response-times-filters">
        <label htmlFor="operator-response-times-round">
          問題
          <select
            disabled={areFiltersDisabled}
            id="operator-response-times-round"
            onChange={(event) => state.selectRound(event.currentTarget.value || null)}
            value={state.roundId ?? ""}
          >
            <option value="">最新の確定済み問題</option>
            {isSelectedRoundMissing ? <option value={state.roundId ?? ""}>選択した問題</option> : null}
            {rounds.map((round) => (
              <option key={round.round_id} value={round.round_id}>
                {roundLabel(round.round_number)}
              </option>
            ))}
          </select>
        </label>
      </div>
      {state.status === "idle" ? (
        <p className="operator-statistics-empty">問題の結果が確定すると表示します。</p>
      ) : isLoading ? (
        <p className="operator-statistics-empty" role="status">回答速度を読み込んでいます…</p>
      ) : state.status === "error" ? (
        <div className="operator-response-times-error">
          <p className="operator-statistics-error" role="alert">回答速度を取得できませんでした。</p>
          <button className="secondary-button" onClick={state.retry} type="button">再試行</button>
        </div>
      ) : state.data === null || !hasRounds ? (
        <p className="operator-statistics-empty">確定済みの問題はまだありません。</p>
      ) : (
        <>
          {selectedRound ? (
            <h3 className="operator-response-times-question">
              {roundLabel(selectedRound.round_number)} · {selectedRound.prompt}
            </h3>
          ) : null}
          <ResponseTimeStatistics
            data={state.data}
            audience={audience}
            search={search}
            searchId="operator-response-times-search"
            onAudienceChange={setAudience}
            onSearchChange={setSearch}
          />
        </>
      )}
    </section>
  );
}
