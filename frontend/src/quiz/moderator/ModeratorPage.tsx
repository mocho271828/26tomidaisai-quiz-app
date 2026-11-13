import { ArrowClockwiseIcon, SignOutIcon } from "@phosphor-icons/react";

import type { OperatorState } from "../../api/operatorContracts";
import { OperatorLogin } from "../_components/OperatorLogin";
import { FestivalLogo } from "../_components/FestivalLogo";
import { ModeratorParticipantPreview } from "./ModeratorParticipantPreview";
import { ModeratorResponseTimes } from "./ModeratorResponseTimes";
import { ModeratorResults } from "./ModeratorResults";
import { useModeratorDetails } from "./useModeratorDetails";
import { useModeratorSession } from "./useModeratorSession";
import { useModeratorState } from "./useModeratorState";
import "../operator/operator.css";
import "./moderator.css";
import "./moderatorLandscape.css";

function ModeratorQuestion({ projection }: {
  projection: OperatorState;
}) {
  const round = projection.current_round;
  return (
    <section className="moderator-card moderator-question" aria-labelledby="moderator-question-title">
      <h2 id="moderator-question-title">今回の問題</h2>
      <div className="moderator-question-content" role="region" aria-label="問題文と選択肢">
        {round === null ? <p className="moderator-result-empty">問題の開始をお待ちください。</p> : (
          <>
            <p className="moderator-question-prompt">{round.question.prompt}</p>
            <ul className="moderator-options">
              {round.question.options.map((option) => (
                <li key={option.option_id} className={option.is_correct ? "moderator-correct-option" : undefined}>
                  <span className="moderator-option-label">{option.label}</span>
                  <span>{option.content}</span>
                  {option.is_correct ? <strong>正解</strong> : null}
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    </section>
  );
}

type DashboardProps = {
  isBusy: boolean;
  sessionError: string | null;
  onLogout: () => Promise<void>;
  onAuthenticationLost: () => void;
};

function ModeratorDashboard({ isBusy, sessionError, onLogout, onAuthenticationLost }: DashboardProps) {
  const { projection, isFresh, error, refresh } = useModeratorState(onAuthenticationLost);
  const details = useModeratorDetails(projection, onAuthenticationLost);
  const notice = sessionError ?? error;
  const isStale = !isFresh && projection !== null;
  const hasDetailsError = details.status === "error";
  const retry = (): void => { details.retry(); void refresh(); };

  return (
    <main className="operator-page moderator-page">
      <div className="moderator-container">
        <header className="moderator-header">
          <div><FestivalLogo /><p>{projection?.event_title ?? "富大祭2026"}</p><h1>司会者用画面</h1></div>
          <button className="operator-logout" disabled={isBusy} type="button" onClick={() => void onLogout()}>
            <SignOutIcon aria-hidden="true" size={22} />ログアウト
          </button>
        </header>
        {notice !== null || isStale || hasDetailsError ? (
          <div className="moderator-notice" role={notice !== null || hasDetailsError ? "alert" : "status"}>
            <div>
              {notice !== null ? <p>{notice}</p> : null}
              {hasDetailsError ? <p>統計・ランキングを読み込めませんでした。</p> : null}
              {isStale ? <p>運営情報は最後に取得した内容です。</p> : null}
            </div>
            <button className="secondary-button" type="button" onClick={hasDetailsError ? retry : () => void refresh()}>
              {hasDetailsError ? "もう一度読み込む" : "運営情報を再取得"}
            </button>
          </div>
        ) : null}
        {projection === null ? (
          <section className="moderator-card moderator-loading" aria-label="情報の確認">
            <p role="status">最新の情報を確認しています。</p>
            <button className="secondary-button" type="button" onClick={retry}>
              <ArrowClockwiseIcon aria-hidden="true" size={22} />もう一度読み込む
            </button>
          </section>
        ) : (
          <div className="moderator-grid">
            <div className="moderator-column moderator-question-column">
              <ModeratorQuestion projection={projection} />
              <ModeratorResponseTimes projection={projection} responseTimes={details.data?.responseTimes ?? null}
                ranking={details.data?.ranking ?? null} />
            </div>
            <div className="moderator-column moderator-results-column">
              <ModeratorResults projection={projection} ranking={details.data?.ranking ?? null}
                responseTimes={details.data?.responseTimes ?? null} />
            </div>
            <div className="moderator-card moderator-preview-card">
              <ModeratorParticipantPreview />
            </div>
          </div>
        )}
      </div>
    </main>
  );
}

export function ModeratorPage() {
  const auth = useModeratorSession();
  if (auth.isChecking || auth.session === null) {
    return <OperatorLogin title="司会者ログイン" isChecking={auth.isChecking} isBusy={auth.isBusy}
      pageError={auth.error} username={auth.username} password={auth.password}
      onUsernameChange={auth.setUsername} onPasswordChange={auth.setPassword} onSubmit={auth.onLogin} />;
  }
  return <ModeratorDashboard key={auth.session.operator.account_id} isBusy={auth.isBusy}
    sessionError={auth.error} onLogout={auth.onLogout} onAuthenticationLost={auth.onAuthenticationLost} />;
}
