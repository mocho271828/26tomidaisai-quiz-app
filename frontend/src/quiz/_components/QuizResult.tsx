import { elapsedSeconds } from "../_utils/answerTime";
import type { ParticipantOutcome, ParticipantResult, PublishedResult, Question } from "../../api/contracts";
import resultCheck from "../../assets/participant-2026/result-check.svg";
import resultClose from "../../assets/participant-2026/result-close.svg";
import { isPracticeRound } from "../_utils/roundPresentation";

type AnswerDetailsProps = {
  question: Question;
  result: PublishedResult;
  selectedOptionId?: string;
  isPending?: boolean;
  outcome?: ParticipantOutcome;
};

function AnswerDetails({ question, result, selectedOptionId, isPending = false, outcome }: AnswerDetailsProps) {
  const correctOption = question.options.find((option) => option.option_id === result.correct_option_id);
  const selectedOption = question.options.find((option) => option.option_id === selectedOptionId);
  return (
    <dl className="participant-answer-details">
      <div><dt>正解</dt><dd>{correctOption === undefined ? "—" : `${correctOption.label}\u3000${correctOption.content}`}</dd></div>
      <div><dt>あなたの回答</dt><dd>{isPending ? "確認中" : selectedOption === undefined ? "未回答" : `${selectedOption.label}\u3000${selectedOption.content}`}</dd></div>
      {outcome?.status === "Scored" ? (
        <div><dt>回答時間 <small>{outcome.timing_basis === "client_monotonic_v1" ? "開始から選択まで" : "開始から受付まで"}</small></dt><dd>{outcome.elapsed_ms === null ? "記録なし" : `${elapsedSeconds(outcome.elapsed_ms)} 秒`}</dd></div>
      ) : null}
    </dl>
  );
}

function speedRankLabel(outcome: Extract<ParticipantResult, { publication: "Published" }>["outcome"]): string | null {
  if (outcome.status !== "Scored" || outcome.speed_rank === null || outcome.speed_cohort_size === null) return null;
  const cohortLabel = outcome.speed_cohort === "Correct" ? "正解者" : "有効回答者";
  return `${cohortLabel}${outcome.speed_cohort_size}人中 ${outcome.speed_rank}番目の速さ`;
}

export function ParticipantPendingOutcomeCard({ question, result }: AnswerDetailsProps) {
  return (
    <section className="participant-score-result participant-result-pending" role="status">
      <h2 className="participant-outcome-heading">結果待ち</h2>
      <span className="sr-only">あなたの結果を確認しています…</span>
      <AnswerDetails question={question} result={result} isPending />
    </section>
  );
}

export function ParticipantOutcomeCard({ question, result, selectedOptionId, roundNumber, totalPoints }: {
  question: Question;
  result: Extract<ParticipantResult, { publication: "Published" }>;
  selectedOptionId?: string;
  roundNumber?: number;
  totalPoints?: number;
}) {
  const outcome = result.outcome;
  const isScored = outcome.status === "Scored";
  const isCorrect = isScored && outcome.is_correct;
  const outcomeStatus = !isScored ? "unanswered" : isCorrect ? "correct" : "incorrect";
  const outcomeLabel = !isScored ? "未回答" : isCorrect ? "正解！" : "不正解";
  const rankLabel = speedRankLabel(outcome);
  const isPractice = roundNumber !== undefined && isPracticeRound(roundNumber);
  return (
    <section className={`participant-score-result outcome-${outcomeStatus}`} aria-label={`マイスコア・${outcomeLabel}`}>
      <h2 className="participant-outcome-heading">
        <img alt="" src={isCorrect ? resultCheck : resultClose} />
        {!isScored ? "今回は未回答でした" : isCorrect ? "正解！" : "惜しい！"}
      </h2>
      <div className="participant-score-summary">
        <span>今回の得点</span>
        <strong>{(isScored ? outcome.total_points : 0).toLocaleString("ja-JP")}<span>pt</span></strong>
      </div>
      {isScored ? (
        <dl className="participant-score-breakdown">
          <div><dt>正答</dt><dd>{outcome.base_points.toLocaleString("ja-JP")} pt</dd></div>
          <div><dt>速さ</dt><dd>{outcome.speed_points.toLocaleString("ja-JP")} pt</dd></div>
        </dl>
      ) : null}
      {rankLabel === null ? null : <p className="participant-score-note">{rankLabel}</p>}
      <AnswerDetails question={question} result={result} selectedOptionId={isScored ? selectedOptionId : undefined} outcome={outcome} />
      {isPractice ? (
        <p className="participant-practice-result-note">これは練習問題です。<br />今回の得点は総合に加算されません。</p>
      ) : totalPoints === undefined ? null : (
        <div className="participant-total-score"><span>総合得点</span><strong>{totalPoints.toLocaleString("ja-JP")} <small>pt</small></strong></div>
      )}
    </section>
  );
}
