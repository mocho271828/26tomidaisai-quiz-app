import { useLayoutEffect, useRef } from "react";
import type { ServerClock } from "../_hooks/useServerClock";
import { useTimeBoundary } from "../_hooks/useDisplayTime";
import { useRoundClock } from "./useRoundClock";
import { observeBrowser } from "../_utils/browserObservation";
import { isPracticeRound, roundLabel } from "../_utils/roundPresentation";

import type { ParticipantViewState } from "./participantState";
import {
  ErrorNotice,
  InfoNotice,
  PageFrame,
} from "../_components/PageFrame";
import { ParticipantOutcomeCard, ParticipantPendingOutcomeCard } from "../_components/QuizResult";
import { ResultTabs } from "../_components/ResultTabs";
import {
  QuizStageAnnouncer,
  QuizWaitingStage,
} from "../_components/QuizWaitingStage";
import { TommyMascot } from "../_components/TommyMascot";
import { ParticipantIdTicket } from "../_components/ParticipantIdTicket";
import { ParticipantFinishedStage } from "./ParticipantFinishedStage";
import { ParticipantAwardStage } from "../_components/ParticipantAwardStage";
import { ParticipantAnswerDistribution } from "../_components/ParticipantAnswerDistribution";
import { ParticipantRankingCard } from "./ParticipantRankingCard";
import { ParticipantQuestion } from "./ParticipantQuestion";
import { ParticipantHeader } from "./ParticipantHeader";
import { ParticipantSubmissionStatus } from "./ParticipantSubmissionStatus";
import type { PendingSubmission } from "./pendingSubmission";
import type { ParticipantSubmission } from "./participantUi";
import { participantWaitingStageProps } from "../_utils/participantWaitingStage";

type ParticipantJoinedViewProps = {
  isStateFresh: boolean;
  onSendAnswer: (pending: PendingSubmission) => Promise<void>;
  onVerifySubmission: (target: ParticipantSubmission) => Promise<void>;
  pageError: string | null;
  projection: ParticipantViewState;
  serverClock: ServerClock;
  submission: ParticipantSubmission | null;
};

export function ParticipantJoinedView({
  isStateFresh,
  onSendAnswer,
  onVerifySubmission,
  pageError,
  projection,
  serverClock,
  submission,
}: ParticipantJoinedViewProps) {
  const shellRef = useRef<HTMLDivElement>(null);
  const displayedQuestionRoundRef = useRef<string | null>(null);
  const currentRound = projection.current_round;
  const roundClock = useRoundClock(currentRound, serverClock);
  const latestResult = projection.latest_result;
  const isCompleted = projection.event.event_state === "Completed";
  const ceremony = projection.award_ceremony;
  const awardee = ceremony?.phase === "Announced"
    ? ceremony.top_rows.find((row) => row.public_label === projection.participant.public_label) : undefined;
  const question = currentRound?.question;
  const openAt = currentRound?.open_at ?? null;
  const deadline = currentRound?.standard_close_at ?? null;
  const canUseCurrentState = isStateFresh && serverClock.isSynchronized && (openAt === null || roundClock.isSynchronized);
  const isWithinAnswerWindow =
    canUseCurrentState &&
    openAt !== null &&
    deadline !== null &&
    roundClock.now >= Date.parse(openAt) &&
    roundClock.now < Date.parse(deadline);
  const currentSubmission =
    submission?.roundId === currentRound?.round_id ? submission : null;
  useTimeBoundary(roundClock, [
    openAt === null ? Number.NaN : Date.parse(openAt),
    deadline === null ? Number.NaN : Date.parse(deadline),
    openAt === null || currentSubmission?.clientElapsedMs == null ? Number.NaN
      : Date.parse(openAt) + currentSubmission.clientElapsedMs + projection.event.arrival_grace_ms,
  ]);
  // Scheduledの開始・締切は予約済み時刻で切り替え、次の状態取得を待たない。
  const isTimedRound =
    currentRound?.state === "Scheduled" || currentRound?.state === "Open";
  const isAnswering =
    !isCompleted &&
    isTimedRound &&
    isWithinAnswerWindow &&
    question !== undefined;
  const shouldShowLatestResult =
    !isCompleted &&
    latestResult !== null &&
    (currentRound === null ||
      currentRound.state === "PreparingAssets");
  const displayedRound = shouldShowLatestResult ? latestResult : currentRound;
  const waitingStageProps = participantWaitingStageProps({
    startDelaySeconds: projection.event.start_delay_seconds,
    isAnswering,
    isSynchronized: canUseCurrentState,
    now: roundClock.now,
    round: currentRound,
  });
  const isCollecting = !isCompleted && (
    currentRound?.state === "ReadyToReveal" ||
    currentRound?.state === "AwaitingValidation" ||
    currentRound?.state === "Reveal" ||
    (isTimedRound &&
      serverClock.isSynchronized &&
      deadline !== null &&
      roundClock.now >= Date.parse(deadline))
  );
  // 選択後の保存確認に時刻同期は不要。再同期失敗で受付DOMを消さず、操作だけを無効にする。
  const isConfirmingAnswer = !isCompleted && !isCollecting && isTimedRound &&
    !canUseCurrentState &&
    ((currentSubmission !== null && displayedQuestionRoundRef.current === currentRound?.round_id) ||
      projection.answer?.round_id === currentRound?.round_id);
  const shellMode = isCompleted
    ? "completed"
    : isAnswering || isConfirmingAnswer
      ? "answering"
      : shouldShowLatestResult
        ? "result"
        : isCollecting
          ? "collecting"
          : currentRound?.state === "Scheduled"
            ? "scheduled"
            : "waiting";
  const displayedRoundNumber = displayedRound?.round_number ?? 0;
  const displayedResultStatus = shouldShowLatestResult ? latestResult.result.outcome?.status : undefined;
  const submissionStatus = currentSubmission?.status;
  const currentRoundId = currentRound?.round_id;
  // 同期anchorから一度変換し、100ms tickごとの計測イベントと描画遅延による締切延長を避ける。
  const answerDeadlineMonotonicMs = canUseCurrentState ? roundClock.deadlinePerformanceMs : undefined;
  useLayoutEffect(() => {
    if (isAnswering && currentRoundId !== undefined) displayedQuestionRoundRef.current = currentRoundId;
    const accepted = shellRef.current?.querySelector(".answer-state-accepted");
    const acceptedStyle = accepted === null || accepted === undefined ? null : getComputedStyle(accepted);
    const notice = shellRef.current?.querySelector(".submission-accepted");
    const noticeStyle = notice === null || notice === undefined ? null : getComputedStyle(notice);
    observeBrowser({
      kind: "participant_display", round_number: displayedRoundNumber, phase: shellMode,
      synchronized: serverClock.isSynchronized, has_question: question !== undefined,
      ...(answerDeadlineMonotonicMs === undefined ? {} : {answer_deadline_monotonic_ms: answerDeadlineMonotonicMs}),
      ...(submissionStatus === undefined ? {} : { submission_status: submissionStatus }),
      answer_dom_present: accepted?.isConnected === true,
      // badge自体のaria-hiddenは装飾の重複読み上げ防止。背景化は親の属性と見た目で判定する。
      answer_dom_visible: accepted?.isConnected === true && accepted.getClientRects().length > 0 &&
        accepted.parentElement?.closest('[aria-hidden="true"], [hidden], [inert]') === null &&
        acceptedStyle?.display !== "none" && acceptedStyle?.visibility !== "hidden" &&
        acceptedStyle?.visibility !== "collapse" && acceptedStyle?.opacity !== "0",
      // 集計中の背景Questionと、利用者へ表示する保存確認文を別々に計測する。
      answer_notice_visible: notice?.isConnected === true && notice.getClientRects().length > 0 &&
        notice.closest('[aria-hidden="true"], [hidden], [inert]') === null &&
        noticeStyle?.display !== "none" && noticeStyle?.visibility !== "hidden" &&
        noticeStyle?.visibility !== "collapse" && noticeStyle?.opacity !== "0",
      result_dom_present: shellRef.current?.querySelector(".participant-score-result")?.isConnected === true,
    });
  }, [displayedRoundNumber, displayedResultStatus, shellMode, serverClock.isSynchronized, question, submissionStatus, isAnswering, currentRoundId, answerDeadlineMonotonicMs]);
  const stageAnnouncement =
    isCompleted
      ? ceremony?.phase === "Finished"
        ? "イベントは以上となります. ご参加ありがとうございました"
        : ceremony?.phase === "Announced"
          ? awardee === undefined ? "最終ランキングを発表しました" : `あなたは${awardee.overall_rank}位!! ステージ上にお越しください!!`
          : "最終結果発表!!"
      : shouldShowLatestResult
      ? latestResult.result.outcome === null
        ? "正答を発表しました。あなたの結果を確認しています"
        : latestResult.result.outcome.status === "Unanswered"
          ? "正答を発表しました。未回答です"
          : latestResult.result.outcome.is_correct
            ? "正答を発表しました。正解です"
            : "正答を発表しました。不正解です"
      : isCollecting
        ? "正解発表をお待ちください"
      : isConfirmingAnswer
        ? "回答の保存状態を確認しています"
        : waitingStageProps?.title ??
        (isAnswering
          ? "回答受付を開始しました"
          : "進行状態を確認しています");
  const canResend = canUseCurrentState && currentSubmission?.clientElapsedMs != null && openAt !== null &&
    deadline !== null && roundClock.now < Date.parse(openAt) + currentSubmission.clientElapsedMs + projection.event.arrival_grace_ms &&
    roundClock.now < Date.parse(deadline) + projection.event.arrival_grace_ms;
  const participantNotices = (
    <>
      {!isCompleted && !shouldShowLatestResult && displayedRound !== null && isPracticeRound(displayedRound.round_number) ? (
        <InfoNotice>
          練習問題の得点はランキングに加算されません
        </InfoNotice>
      ) : null}
      {pageError !== null ? <ErrorNotice>{pageError}</ErrorNotice> : null}
    </>
  );

  return (
    <PageFrame
      audience="参加者"
      layout="answering"
      rootClassName="participant-live-page"
      title={projection.event.event_title}
    >
      <div
        className={`participant-live-shell participant-live-shell-${shellMode}`}
        ref={shellRef}
      >
        <QuizStageAnnouncer message={stageAnnouncement} />
        <ParticipantHeader publicLabel={projection.participant.public_label} />
        {isCollecting ? null : participantNotices}

        {isCompleted ||
        isCollecting ||
        isConfirmingAnswer ||
        waitingStageProps === null ||
        shouldShowLatestResult ? null : (
          <QuizWaitingStage {...waitingStageProps} publicLabel={projection.participant.public_label}
            countdownClock={openAt === null ? undefined : { ...roundClock, until: Date.parse(openAt) }} />
        )}

        {isCompleted ? ceremony?.phase === "Finished"
          ? <ParticipantFinishedStage ranking={projection.ranking} isRankingPublic={projection.result_visibility.personal_ranking} />
          : <ParticipantAwardStage ceremony={ceremony} publicLabel={projection.participant.public_label} /> : null}

        {shouldShowLatestResult ? (
          <div className="participant-result-stage">
            <ResultTabs
              roundNumber={latestResult.round_number}
              answer={
                latestResult.result.outcome === null ? (
                  <ParticipantPendingOutcomeCard question={latestResult.question} result={latestResult.result} />
                ) : <ParticipantOutcomeCard
                  roundNumber={latestResult.round_number}
                  totalPoints={projection.ranking?.total_points}
                  question={latestResult.question}
                  result={{ ...latestResult.result, outcome: latestResult.result.outcome }}
                  selectedOptionId={
                    latestResult.selected_option_id ?? undefined
                  }
                />
              }
              distribution={
                latestResult.result.distribution === null ? undefined : (
                  <ParticipantAnswerDistribution
                    question={latestResult.question}
                    result={latestResult.result}
                    selectedOptionId={
                      latestResult.selected_option_id ?? undefined
                    }
                  />
                )
              }
              key={latestResult.round_id}
              ranking={
                projection.ranking !== undefined ? (
                  <>
                    <h2 className="sr-only">{roundLabel(latestResult.round_number)}終了時点</h2>
                    <ParticipantRankingCard
                      ranking={projection.ranking}
                      publicLabel={projection.participant.public_label}
                    />
                  </>
                ) : undefined
              }
            />
          </div>
        ) : null}

        {!isCompleted &&
        (isAnswering || isCollecting || isConfirmingAnswer) &&
        currentRound !== null &&
        question !== undefined ? (
          <div className="participant-question-stage" aria-hidden={isCollecting || undefined} inert={isCollecting}>
            <ParticipantQuestion
              answer={projection.answer}
              isAnswering={isAnswering}
              onSendAnswer={onSendAnswer}
              question={question}
              round={currentRound}
              roundClock={roundClock}
              submission={currentSubmission}
            />
          </div>
        ) : null}

        {isCollecting ? (
          <div className="participant-collection-overlay">
            <section className="participant-collection-card" aria-label="正解発表待ち">
              <div className="participant-collection-heading">
                {participantNotices}
                <h2 className="participant-waiting-heading">正解発表を<br />お待ちください</h2>
                <p className="sr-only" role="status">正解発表をお待ちください</p>
              </div>
              <div className="participant-waiting-character">
                <TommyMascot size="large" variant="smirking" />
              </div>
              <div className="participant-waiting-footer">
                {currentSubmission !== null ? (
                  <ParticipantSubmissionStatus
                    isAnswerSendDisabled={!canResend}
                    submission={currentSubmission}
                    onSendAnswer={onSendAnswer}
                    onVerifySubmission={onVerifySubmission}
                  />
                ) : null}
                <ParticipantIdTicket publicLabel={projection.participant.public_label} />
              </div>
            </section>
          </div>
        ) : currentSubmission !== null && currentRound !== null ? (
          <ParticipantSubmissionStatus
            isAnswerSendDisabled={!canResend}
            submission={currentSubmission}
            onSendAnswer={onSendAnswer}
            onVerifySubmission={onVerifySubmission}
          />
        ) : null}

      </div>
    </PageFrame>
  );
}
