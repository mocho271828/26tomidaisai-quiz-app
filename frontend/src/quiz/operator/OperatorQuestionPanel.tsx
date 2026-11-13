import { CheckCircleIcon, ListChecksIcon, LockKeyIcon, PlayIcon } from "@phosphor-icons/react";

import type { OperatorState } from "../../api/operatorContracts";
import { ErrorNotice, InfoNotice } from "../_components/PageFrame";
import { formatDateTime } from "../_components/format";
import { PRACTICE_ROUND_COUNT } from "../_utils/roundPresentation";
import { OperatorQuestionSelector } from "./OperatorQuestionSelector";
import { backupStatusMessages, canPublishRoundResults, type RoundOperation } from "./operatorUi";
import { OperatorFinalRound } from "./final-round/OperatorFinalRound";
import type { OperatorFinalRoundControl } from "./final-round/useOperatorFinalRound";
import { OperatorConfirmationAction } from "./_components/OperatorConfirmationAction";
import { OperatorAwardCeremony } from "./award-ceremony/OperatorAwardCeremony";
import type { OperatorAwardCeremonyControl } from "./award-ceremony/useOperatorAwardCeremony";

type OperatorQuestionPanelProps = {
  awardCeremony: OperatorAwardCeremonyControl;
  finalRound: OperatorFinalRoundControl;
  projection: OperatorState;
  isBusy: boolean;
  onAdvanceRound: (questionId: string) => Promise<void>;
  onCompleteEvent: () => Promise<void>;
  onQuestionChange: (questionId: string) => Promise<void>;
  onRetryResults: () => Promise<void>;
  onRoundOperation: (operation: RoundOperation) => Promise<void>;
};

export function OperatorQuestionPanel({
  projection, isBusy, finalRound, awardCeremony, onAdvanceRound, onCompleteEvent,
  onQuestionChange, onRetryResults, onRoundOperation,
}: OperatorQuestionPanelProps) {
  const round = projection.current_round;
  const isEventComplete = ["Completed", "Archived"].includes(projection.event_state);
  const isAdvanceState = !isEventComplete && (round?.state === "Reveal" || round?.state === "RoundBackupFailed");
  const isBackupInProgress = round?.state === "RoundBackupPending" || round?.state === "RoundBackupRetrying";
  const usedQuestionIds = new Set(projection.used_question_ids);
  const unusedQuestions = projection.available_questions.filter(
    (question) => question.question_id !== round?.question.question_id && !usedQuestionIds.has(question.question_id),
  );
  const currentRoundQuestions = projection.available_questions.filter(
    (question) => question.question_id === round?.question.question_id || !usedQuestionIds.has(question.question_id),
  );
  const currentQuestionPreview = projection.available_questions.find(
    (question) => question.question_id === round?.question.question_id,
  );
  const nextRoundQuestions = currentQuestionPreview === undefined
    ? unusedQuestions : [currentQuestionPreview, ...unusedQuestions];
  const backupStatusMessage = round === null ? undefined : backupStatusMessages[round.state];
  // 確認した対象・版・操作条件が変わったら再mountし、古い確認とチェックを持ち越さない。
  const confirmationKey = [projection.event_id, projection.event_version, projection.event_state,
    round?.round_id, round?.version, round?.state, round?.result_publication,
    round?.question.question_id, round?.question.version, finalRound.status,
    finalRound.value?.final_round_id, isBusy].join(":");
  const responseRate = projection.participant_count === 0 ? 0
    : Math.min(100, Math.round((round?.statistics.answer_count ?? 0) / projection.participant_count * 100));

  return (
    <section className="operator-card operator-question" aria-label="現在の問題と進行">
      <div className="operator-card-heading">
        <span className="operator-eyebrow">現在の問題</span>
        <span className="operator-badge"><LockKeyIcon aria-hidden="true" size={14} />運営専用</span>
      </div>
      {round === null ? <h2>進行できる問題がありません</h2> : (
        <>
          <h2 className="operator-question-title">{round.question.prompt}</h2>
          <div className="operator-options" aria-label="問題選択肢">
            {round.question.options.map((option) => (
              <div className={option.is_correct ? "operator-correct-option" : undefined} key={option.option_id}>
                <strong>{option.label}</strong><span>{option.content}</span>
                {option.is_correct ? <small><CheckCircleIcon aria-hidden="true" size={14} />正解</small> : null}
              </div>
            ))}
          </div>
          <div className="operator-control">
            {isEventComplete ? (
              <InfoNotice>Eventは完了しました。次の問題へ進む操作はありません。</InfoNotice>
            ) : backupStatusMessage !== undefined ? (
              round.state === "RoundBackupFailed"
                ? <ErrorNotice>{backupStatusMessage}</ErrorNotice>
                : <InfoNotice>{backupStatusMessage}</InfoNotice>
            ) : round.state === "PreparingAssets" ? (
              <><h3>配信前</h3><p>問題を確定して配信した後、別の操作で回答受付を開始します。</p></>
            ) : round.state === "ReadyToSchedule" ? (
              <><h3>参加者の端末に配信済み</h3><p>端末の受信と時刻同期のために時間を取り、会場の開始合図に合わせて操作してください。</p></>
            ) : round.state === "Scheduled" ? (
              <><h3>開始予約済み</h3><p>予約した時刻に回答受付を開始します。</p></>
            ) : round.state === "Open" ? (
              <>
                <h3>回答受付中</h3>
                <div className="operator-response-label"><span>回答率</span><strong>{responseRate}%</strong></div>
                <progress className="operator-response-progress" aria-label="参加者の回答率" max={100} value={responseRate} />
                <p>回答締切後、到着猶予を待って集計します。</p>
              </>
            ) : round.state === "AwaitingValidation" && round.result_publication === "Withheld" ? (
              <><h3>自動集計中</h3><p role="status">回答を検証し、結果を自動集計しています。</p></>
            ) : round.state === "VoidCandidate" && round.result_publication === "Withheld" ? (
              <><h3>集計を完了できませんでした</h3><p role="alert">自動集計を完了できませんでした。原因を確認して再試行してください。{round.result_failure_code === null ? "" : ` エラーコード: ${round.result_failure_code}`}</p></>
            ) : round.state === "ReadyToReveal" ? (
              <><h3>集計完了・未公開</h3><p>統計情報を確認できます。司会の正解発表に合わせて公開してください。</p></>
            ) : round.state === "Reveal" ? (
              <><h3>公開済み</h3><p>参加者には公開設定に従って表示しています。</p></>
            ) : round.state === "Voided" ? <h3>この問題は無効になりました</h3> : null}
            {round.open_at !== null ? (
              <dl className="operator-timeline">
                <div><dt>開始</dt><dd>{formatDateTime(round.open_at)}</dd></div>
                <div><dt>回答締切</dt><dd>{formatDateTime(round.standard_close_at)}</dd></div>
              </dl>
            ) : null}
            {!isEventComplete ? (
              <div className="operator-actions">
                {round.state === "AwaitingValidation" || round.state === "ReadyToReveal" ? (
                  <button className="primary-button" disabled={isBusy || !canPublishRoundResults(projection)} onClick={() => void onRoundOperation("publish-results")} type="button">
                    <CheckCircleIcon aria-hidden="true" size={20} />正解・結果を公開
                  </button>
                ) : null}
                {round.state === "PreparingAssets" ? (
                  <button className="primary-button" disabled={isBusy} onClick={() => void onRoundOperation("prepare")} type="button">
                    <ListChecksIcon aria-hidden="true" size={20} />問題を確定して配信
                  </button>
                ) : null}
                {round.state === "ReadyToSchedule" ? (
                  <button className="primary-button" disabled={isBusy} onClick={() => void onRoundOperation("schedule")} type="button">
                    <PlayIcon aria-hidden="true" size={20} weight="fill" />{projection.start_delay_seconds}秒後に開始予約
                  </button>
                ) : null}
                {round.state === "VoidCandidate" && round.result_publication === "Withheld" ? (
                  <button className="danger-button" disabled={isBusy} onClick={() => void onRetryResults()} type="button">
                    <ListChecksIcon aria-hidden="true" size={20} />結果集計を再試行
                  </button>
                ) : null}
              </div>
            ) : null}
            <OperatorFinalRound key={`final:${confirmationKey}`} projection={projection} control={finalRound} isBusy={isBusy} />
            <OperatorAwardCeremony projection={projection} control={awardCeremony} isBusy={isBusy} />
            {isAdvanceState ? (
              <div className="operator-completion">
                <h3>イベントの終了</h3>
                <p>進行に合わせて、運営が終了するタイミングを決定してください。</p>
                <OperatorConfirmationAction key={`complete:${confirmationKey}`} isDangerous
                  triggerLabel={<><CheckCircleIcon aria-hidden="true" size={20} />イベントを終了…</>}
                  title="イベントを終了しますか？" confirmLabel="イベントを終了する"
                  acknowledgement="終了後は次の問題へ進めないことを確認しました"
                  isDisabled={isBusy} onConfirm={() => void onCompleteEvent()}>
                  <p className="operator-confirmation-question">
                    {currentQuestionPreview === undefined ? "現在の問題" : `登録問題 ${currentQuestionPreview.display_order}`}：{round.question.prompt}
                  </p>
                  <p>未出題の問題は残り{unusedQuestions.length}問です。</p>
                  <p>現在の問題の記録を保存・検証してからイベントを終了します。終了後は次の問題へ進めません。</p>
                  <p>終了すると参加者に「最終結果発表!!」を表示します。表彰対象者の呼び出しとアンケート案内は、その後に個別のボタンで進めます。</p>
                  {finalRound.status === "ready" && finalRound.value?.final_round_id !== round.round_id ? (
                    <p>現在の問題は最終問題に指定されていません。</p>
                  ) : finalRound.status !== "ready" ? <p>最終問題の指定状況を確認できていません。</p> : null}
                </OperatorConfirmationAction>
              </div>
            ) : null}
          </div>
          {!isEventComplete && !isAdvanceState && !isBackupInProgress && currentRoundQuestions.length > 1 ? (
            <OperatorQuestionSelector
              canSelectQuestion={round.state === "PreparingAssets"}
              currentQuestionId={round.question.question_id} isBusy={isBusy}
              key={`${projection.event_id}:${round.round_id}:${round.question.question_id}`}
              mode="switch-current-round" onQuestionChange={onQuestionChange}
              questions={currentRoundQuestions}
            />
          ) : null}
          {isAdvanceState && unusedQuestions.length > 0 ? (
            <OperatorQuestionSelector
              canSelectQuestion currentQuestionId={round.question.question_id} isBusy={isBusy}
              key={`${projection.event_id}:${round.round_id}:advance`}
              mode="advance-round" onQuestionChange={onAdvanceRound}
              questions={nextRoundQuestions}
            />
          ) : null}
        </>
      )}
      <p className="operator-practice-note">最初の{PRACTICE_ROUND_COUNT}問は練習問題です。練習の得点は正答発表時に確認でき、総合ランキングには加算されません。本番は第1問から表示し、出題数と終了のタイミングは運営が決定します。途中参加の方も総合ランキングに含まれます。</p>
    </section>
  );
}
