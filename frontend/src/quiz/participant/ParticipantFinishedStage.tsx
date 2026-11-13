import type { ParticipantRanking } from "../../api/contracts";
import arrowRight from "../../assets/participant-2026/arrow-right.svg";
import { TommyMascot } from "../_components/TommyMascot";
import { participantUiConfig } from "./participantUiConfig";

type ParticipantFinishedStageProps = {
  ranking?: ParticipantRanking;
  isRankingPublic?: boolean;
};

export function ParticipantFinishedStage({ ranking, isRankingPublic = false }: ParticipantFinishedStageProps) {
  const canShowRanking = isRankingPublic && ranking !== undefined;
  const fallback = isRankingPublic ? "—" : "非公開";
  return (
    <section aria-labelledby="participant-finished-title" className="participant-finished-stage participant-finished-screen">
      <h2 id="participant-finished-title">最終結果</h2>
      <div className="participant-final-metrics">
        <div><p>総合得点</p><strong className={canShowRanking ? undefined : "participant-final-unavailable"}>{canShowRanking ? ranking.total_points.toLocaleString("ja-JP") : fallback}{canShowRanking ? <small>pt</small> : null}</strong></div>
        <div><p>最終順位</p><strong className={canShowRanking ? undefined : "participant-final-unavailable"}>{canShowRanking ? ranking.overall_rank : fallback}{canShowRanking ? <small>位</small> : null}</strong></div>
      </div>
      {isRankingPublic && ranking === undefined ? <p className="participant-final-pending" role="status">結果待ち</p> : null}
      <div className="participant-finished-mascot"><TommyMascot size="large" variant="applauding" /></div>
      <p className="participant-finished-message">ご参加ありがとうございました</p>
      <div className="participant-finished-actions">
        {participantUiConfig.surveyUrl === null ? null : <a className="participant-primary-button participant-finished-survey" href={participantUiConfig.surveyUrl}>来場者アンケートに回答する<img alt="" src={arrowRight} /></a>}
        {participantUiConfig.homeUrl === null ? null : <a className="text-button participant-finished-home" href={participantUiConfig.homeUrl}>富大祭のページを見る</a>}
      </div>
    </section>
  );
}
