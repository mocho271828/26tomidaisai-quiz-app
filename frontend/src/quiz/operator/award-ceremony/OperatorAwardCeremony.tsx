import type { OperatorState } from "../../../api/operatorContracts";
import { OperatorConfirmationAction } from "../_components/OperatorConfirmationAction";
import type { OperatorAwardCeremonyControl } from "./useOperatorAwardCeremony";

type Props = {
  projection: OperatorState;
  control: OperatorAwardCeremonyControl;
  isBusy: boolean;
};

export function OperatorAwardCeremony({ projection, control, isBusy }: Props) {
  if (projection.event_state !== "Completed") return null;
  const phase = projection.award_ceremony_phase;
  const isDisabled = isBusy || control.isSynchronizing;
  const confirmationKey = [projection.event_id, projection.event_version, phase, isBusy].join(":");

  return (
    <section className="operator-completion" aria-labelledby="operator-award-ceremony-title">
      <h3 id="operator-award-ceremony-title">最終結果発表・表彰</h3>
      {control.isSynchronizing ? (
        <><p role="status">表示状態を確認しています。</p><button className="secondary-button" type="button" disabled={isBusy} onClick={() => void control.retry()}>表示状態を再確認</button></>
      ) : phase === "Waiting" ? (
        <>
          <p>参加者の画面は「最終結果発表!!」です。司会の合図に合わせて表彰対象者を呼び出してください。</p>
          <OperatorConfirmationAction key={confirmationKey} triggerLabel="表彰対象者を呼び出す…"
            title="表彰対象者を呼び出しますか？" confirmLabel="呼び出し・最終ランキングを一斉公開"
            isDisabled={isDisabled} onConfirm={() => void control.onChange("Announced")}>
            <p>1位から5位の全員に本人の順位と「ステージ上にお越しください!!」を表示します。同率5位も全員が対象です。</p>
            <p>それ以外の参加者には1位から5位のランキングを表示します。司会の合図を確認して公開してください。</p>
          </OperatorConfirmationAction>
        </>
      ) : phase === "Announced" ? (
        <>
          <p>表彰対象者への呼び出しと、その他の参加者への最終ランキングを表示中です。</p>
          <OperatorConfirmationAction key={confirmationKey} triggerLabel="アンケート・ホームページ案内へ…"
            title="表彰画面から終了案内へ進みますか？" confirmLabel="アンケート・ホームページ案内を表示"
            isDisabled={isDisabled} onConfirm={() => void control.onChange("Finished")}>
            <p>全員の表彰・ランキング画面を終了し、アンケートとホームページへの案内へ切り替えます。</p>
          </OperatorConfirmationAction>
        </>
      ) : phase === "Finished" ? <p>アンケート・ホームページへの案内を表示中です。</p>
        : <p role="alert">表彰の表示状態を確認できません。</p>}
    </section>
  );
}
