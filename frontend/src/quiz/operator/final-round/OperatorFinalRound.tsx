import type { OperatorState } from "../../../api/operatorContracts";
import { OperatorConfirmationAction } from "../_components/OperatorConfirmationAction";
import type { OperatorFinalRoundControl } from "./useOperatorFinalRound";

export function OperatorFinalRound({ projection, control, isBusy }: {
  projection: OperatorState;
  control: OperatorFinalRoundControl;
  isBusy: boolean;
}) {
  const round = projection.current_round;
  if (round === null) return null;
  const isFinal = control.value?.final_round_id === round.round_id;
  const isDisabled = isBusy || control.status !== "ready" ||
    projection.event_state !== "InProgress" || round.state === "Voided";
  const questionNumber = projection.available_questions.find(
    (question) => question.question_id === round.question.question_id,
  )?.display_order;

  return (
    <section className="operator-completion" aria-labelledby="operator-final-round-title">
      <h3 id="operator-final-round-title">最終問題の指定</h3>
      <p>進行時間に合わせて最終問題を指定してください。司会者用画面のランキング TOP5には、常に直近の確定結果を表示します。</p>
      {control.status === "error" ? (
        <>
          <p role="status">最終問題の指定を確認できませんでした。</p>
          <button className="secondary-button" disabled={isBusy} onClick={control.retry} type="button">指定を再確認</button>
        </>
      ) : control.status === "ready" ? (
        <p role="status">{isFinal ? "この問題が最終問題です。" : "この問題は最終問題に指定されていません。"}</p>
      ) : <p role="status">最終問題の指定を確認しています…</p>}
      <OperatorConfirmationAction
        triggerLabel={isFinal ? "指定を変更…" : "この問題を最終問題にする"}
        title={isFinal ? "最終問題の指定を解除しますか？" : "最終問題に指定しますか？"}
        confirmLabel={isFinal ? "最終問題の指定を解除" : "この問題を最終問題に指定"}
        isDisabled={isDisabled} onConfirm={() => void control.onChange(!isFinal)}>
        <p className="operator-confirmation-question">
          {questionNumber === undefined ? "現在の問題" : `登録問題 ${questionNumber}`}：{round.question.prompt}
        </p>
        <p>{isFinal ? "この問題の最終問題指定を解除します。" : "この問題を最終問題として記録します。"}</p>
        <p>この操作ではイベントは終了しません。</p>
      </OperatorConfirmationAction>
    </section>
  );
}
