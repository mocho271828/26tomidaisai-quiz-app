import { CheckCircleIcon, ClockIcon, SignOutIcon, UsersThreeIcon } from "@phosphor-icons/react";

import type { OperatorSession, OperatorState, ResultVisibilityTarget } from "../../api/operatorContracts";
import { ErrorNotice, InfoNotice, LoadingPanel } from "../_components/PageFrame";
import { FestivalLogo } from "../_components/FestivalLogo";
import { OperatorStatistics } from "./OperatorStatistics";
import { OperatorResponseTimes } from "./OperatorResponseTimes";
import { OperatorQuestionPanel } from "./OperatorQuestionPanel";
import { OperatorResultVisibility } from "./OperatorResultVisibility";
import { operatorStateLabel, type RoundOperation } from "./operatorUi";
import type { OperatorRankingState } from "./useOperatorRanking";
import { useOperatorPanelLayout } from "./useOperatorPanelLayout";
import type { OperatorFinalRoundControl } from "./final-round/useOperatorFinalRound";
import type { OperatorAwardCeremonyControl } from "./award-ceremony/useOperatorAwardCeremony";

type OperatorDashboardProps = {
  awardCeremony: OperatorAwardCeremonyControl;
  finalRound: OperatorFinalRoundControl;
  isBusy: boolean;
  onAdvanceRound: (questionId: string) => Promise<void>;
  onCompleteEvent: () => Promise<void>;
  onLogout: () => Promise<void>;
  onQuestionChange: (questionId: string) => Promise<void>;
  onResultVisibility: (
    target: ResultVisibilityTarget,
    visible: boolean,
  ) => Promise<void>;
  onRetryResults: () => Promise<void>;
  onRoundOperation: (operation: RoundOperation) => Promise<void>;
  operationMessage: string | null;
  pageError: string | null;
  projection: OperatorState | null;
  rankingState: OperatorRankingState;
  session: OperatorSession;
};

export function OperatorDashboard(props: OperatorDashboardProps) {
  const { isBusy, onLogout, onResultVisibility, operationMessage,
    pageError, projection, rankingState, session } = props;
  const round = projection?.current_round ?? null;
  const gridRef = useOperatorPanelLayout(projection !== null);

  return (
    <main className="operator-page">
      <div className="operator-container">
        <header className="operator-header">
          <div>
            <FestivalLogo />
            <p className="operator-eyebrow">{projection?.event_title ?? "富大祭2026"}</p>
            <h1>運営</h1>
          </div>
          <div className="operator-header-actions">
            <a className="operator-logout" href="/moderator" target="_blank" rel="noopener noreferrer">司会者用画面</a>
            {projection !== null ? <span className="operator-badge">{operatorStateLabel(projection)}</span> : null}
            <span className="operator-account">{session.operator.username}</span>
            <button className="operator-logout" disabled={isBusy} onClick={() => void onLogout()} type="button">
              <SignOutIcon aria-hidden="true" size={20} />ログアウト
            </button>
          </div>
        </header>
        {pageError !== null ? <ErrorNotice>{pageError}</ErrorNotice> : null}
        {operationMessage !== null ? <InfoNotice>{operationMessage}</InfoNotice> : null}
        {projection === null ? <LoadingPanel label="イベント状態を取得しています" /> : (
          <>
            <section className="operator-metrics" aria-label="イベント概要">
              <div><UsersThreeIcon aria-hidden="true" size={22} /><span>参加者</span><strong>{projection.participant_count.toLocaleString("ja-JP")}<small>人</small></strong></div>
              <div><CheckCircleIcon aria-hidden="true" size={22} /><span>現在問題の回答</span><strong>{round?.statistics.answer_count.toLocaleString("ja-JP") ?? "—"}<small>/ {projection.participant_count.toLocaleString("ja-JP")}人</small></strong></div>
              <div><ClockIcon aria-hidden="true" size={22} /><span>出題状況</span><strong>{projection.used_question_ids.length}<small>/ {projection.available_questions.length}問</small></strong></div>
            </section>
            <div className="operator-dashboard-grid" ref={gridRef}>
              <OperatorQuestionPanel
                awardCeremony={props.awardCeremony}
                finalRound={props.finalRound}
                projection={projection} isBusy={isBusy}
                onAdvanceRound={props.onAdvanceRound} onCompleteEvent={props.onCompleteEvent}
                onQuestionChange={props.onQuestionChange} onRetryResults={props.onRetryResults}
                onRoundOperation={props.onRoundOperation}
              />
              <OperatorResultVisibility isBusy={isBusy} onChange={onResultVisibility} projection={projection} />
              <OperatorStatistics
                options={round?.question.options ?? []} rankingState={rankingState}
                statistics={round?.statistics ?? { answer_count: 0, average_response_ms: null, options: [] }}
                isResultFinalized={round?.state === "ReadyToReveal" || round?.result_publication === "Published"}
              />
              <OperatorResponseTimes snapshotVersion={projection.latest_ranking_snapshot_version} />
            </div>
          </>
        )}
      </div>
    </main>
  );
}
