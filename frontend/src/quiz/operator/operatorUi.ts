import type { OperatorState, ResultVisibilityTarget } from "../../api/operatorContracts";
import type { RoundState } from "../../api/contracts";

export type RoundOperation = "prepare" | "schedule" | "publish-results";

export const visibilityTargetLabels: Record<ResultVisibilityTarget, string> = {
  distribution: "回答分布",
  personal_ranking: "本人順位",
  overall_ranking: "全体順位",
  rankings: "自分の順位・みんなの順位",
};

export function canPublishRoundResults(projection: OperatorState): boolean {
  const round = projection.current_round;
  return projection.event_state === "InProgress" && round?.state === "ReadyToReveal" &&
    round.result_publication === "Withheld" && round.can_publish_results;
}

export const backupStatusMessages: Partial<Record<RoundState, string>> = {
  RoundBackupPending:
    "前のRoundのbackupを作成・検証しています。完了するまで次の操作へ進めません。",
  RoundBackupRetrying:
    "前のRoundのbackupを再検証しています。完了するまで次の操作へ進めません。",
  RoundBackupFailed:
    "前のRoundのbackup検証に失敗しました。下の操作から再検証してください。次の進行はまだ確定していません。",
};

const roundStateLabels: Record<RoundState, string> = {
  PreparingAssets: "問題確認", ReadyToSchedule: "開始待機", Scheduled: "開始予約済み",
  Open: "回答受付中", ReadyToReveal: "公開待ち",
  AwaitingValidation: "集計中", VoidCandidate: "集計エラー", Reveal: "公開済み",
  Voided: "無効", RoundBackupPending: "記録保存中", RoundBackupRetrying: "記録保存中",
  RoundBackupFailed: "記録保存エラー", RoundBackupValidated: "記録保存済み",
};

export function operatorStateLabel(projection: OperatorState): string {
  if (projection.event_state === "Completed" || projection.event_state === "Archived") return "終了済み";
  return projection.current_round === null ? "問題なし" : roundStateLabels[projection.current_round.state];
}

export function operationLabel(
  operation: RoundOperation,
  startDelaySeconds: number,
): string {
  const labels: Record<RoundOperation, string> = {
    "publish-results": "正解・結果の公開",
    prepare: "問題を確定して配信",
    schedule: `${startDelaySeconds}秒後に開始予約`,
  };
  return labels[operation];
}
