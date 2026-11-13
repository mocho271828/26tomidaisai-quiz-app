import type { ParticipantState } from "../../api/contracts";
import type { QuizWaitingStageProps } from "../_components/QuizWaitingStage";
import { roundLabel as formatRoundLabel } from "./roundPresentation";

import { remainingSeconds } from "./answerTime";

function countdown(deadline: string, now: number): string {
  const remainingMilliseconds = Date.parse(deadline) - now;
  if (remainingMilliseconds <= 0) {
    return "0.0";
  }
  return remainingSeconds(remainingMilliseconds);
}

export function participantWaitingStageProps({
  isAnswering,
  isSynchronized,
  now,
  round,
  startDelaySeconds,
}: {
  isAnswering: boolean;
  isSynchronized: boolean;
  now: number;
  round: ParticipantState["current_round"];
  startDelaySeconds: number;
}): QuizWaitingStageProps | null {
  if (round === null) {
    return {
      description: "クイズが始まると、自動で画面が切り替わります。",
      footer: "問題文と選択肢は、開始の合図まで表示されません。",
      kicker: "参加できました",
      displayHeading: "エントリー完了！",
      phase: "idle",
      presentation: "participant",
      roundLabel: "待機中",
      participantLayout: "ticket",
      title: "開始まで、もう少し待ってね！",
    };
  }
  if (isAnswering) {
    return null;
  }

  const roundLabel = formatRoundLabel(round.round_number);
  if (round.state === "PreparingAssets") {
    return {
      description:
        "問題データを安全に読み込んでいます。準備が整うと、開始の合図を待つ画面へ進みます。",
      footer: "準備中も、未公開の問題文と正答は表示されません。",
      kicker: "問題準備中",
      displayHeading: "次の問題を\nお待ちください",
      participantLayout: "ticket",
      phase: "preparing",
      presentation: "participant",
      roundLabel,
      title: "次の問題を準備しています",
    };
  }
  if ((round.state === "ReadyToSchedule" || round.state === "Scheduled" || round.state === "Open") && round.question === undefined) {
    return {
      description: "問題データの受信を確認しています。通信が戻ると自動で続行します。",
      footer: "未受信の間は回答できません。",
      displayHeading: "しばらく\nお待ちください", participantLayout: "recovery",
      kicker: "問題受信中", phase: "preparing", presentation: "participant", roundLabel,
      title: "問題データを受信しています",
    };
  }
  if (round.state === "ReadyToSchedule" && !isSynchronized) {
    return {
      description: "問題データを受信しました。この端末の時刻をサーバーに合わせています。",
      footer: "時刻の確認が終わるまで、この画面でお待ちください。",
      displayHeading: "しばらく\nお待ちください", participantLayout: "recovery",
      kicker: "時刻同期中", phase: "preparing", presentation: "participant", roundLabel,
      title: "開始時刻を安全に確認しています",
    };
  }
  if (round.state === "ReadyToSchedule") {
    return {
      description: `運営が開始すると、${startDelaySeconds}秒のカウントダウンが始まります。`,
      footer: "この画面のまま、開始の合図をお待ちください。",
      kicker: "まもなくスタート",
      displayHeading: "まもなくスタート！",
      participantLayout: "ready",
      phase: "preparing",
      presentation: "participant",
      roundLabel,
      speech: `${roundLabel}\nもうすぐ始まるよ!!`,
      title: `${roundLabel}もうすぐ始まるよ!!`,
    };
  }
  if (
    !isSynchronized &&
    (round.state === "Scheduled" || round.state === "Open")
  ) {
    return {
      description:
        "全員が公平にスタートできるよう、サーバーの時刻とこの端末を合わせています。",
      footer: "時刻の確認が終わるまで、問題文は表示されません。",
      kicker: "時刻同期中",
      displayHeading: "しばらく\nお待ちください",
      participantLayout: "recovery",
      phase: "scheduled",
      presentation: "participant",
      roundLabel,
      title: "開始時刻を安全に確認しています",
    };
  }
  if (
    round.state === "Scheduled" &&
    round.open_at !== null &&
    now < Date.parse(round.open_at)
  ) {
    return {
      countdown: countdown(round.open_at, now),
      description:
        "カウントダウンが0になると問題文と選択肢が表示されます。回答の準備をしてください。",
      footer: "問題文は開始時刻まで表示されません。",
      kicker: "回答開始まで",
      displayHeading: "まもなくスタート！",
      phase: "scheduled",
      presentation: "participant",
      roundLabel,
      speech: `${roundLabel}\nもうすぐ始まるよ!!`,
      title: `${roundLabel}もうすぐ始まるよ!!`,
    };
  }
  if (round.state === "AwaitingValidation" || round.state === "ReadyToReveal" || round.state === "Reveal" ||
    ((round.state === "Scheduled" || round.state === "Open") && round.standard_close_at !== null &&
      now >= Date.parse(round.standard_close_at))) {
    return {
      description: "司会の正解発表をお待ちください。",
      footer: "発表に合わせて、自動で画面が切り替わります。",
      kicker: "正解発表待ち",
      phase: "validating",
      roundLabel,
      title: "正解発表をお待ちください",
    };
  }
  return {
    description:
      "運営が進行状況を確認しています。案内があるまで、この画面のままお待ちください。",
    footer: "未公開の問題文・正答・得点は表示されません。",
    kicker: "進行確認中",
    displayHeading: "しばらく\nお待ちください",
    participantLayout: "recovery",
    phase: "idle",
    presentation: "participant",
    roundLabel,
    title: "次の案内をお待ちください",
  };
}
