import type { ParticipantState, Question } from "../../api/contracts";
import { ParticipantQuestionView } from "../_components/ParticipantQuestionView";
import { createClientUuid } from "../_utils/clientUuid";
import type { PendingSubmission } from "./pendingSubmission";
import type { ParticipantSubmission } from "./participantUi";
import type { RoundClock } from "./useRoundClock";

export function ParticipantQuestion({
  answer,
  isAnswering,
  onSendAnswer,
  question,
  round,
  roundClock,
  submission,
}: {
  answer: ParticipantState["answer"];
  isAnswering: boolean;
  onSendAnswer: (pending: PendingSubmission) => Promise<void>;
  question: Question;
  round: NonNullable<ParticipantState["current_round"]>;
  roundClock: RoundClock;
  submission: ParticipantSubmission | null;
}) {
  const openAt = round.open_at;
  const deadline = round.standard_close_at;
  const currentAnswer =
    answer?.round_id === round.round_id
      ? answer
      : undefined;
  const selectedOptionId =
    currentAnswer?.option_id ?? submission?.optionId;
  const isAnswerLocked =
    currentAnswer !== undefined || submission !== null;
  const timingBasis = currentAnswer?.timing_basis ?? submission?.timingBasis;
  const clientElapsedMs = currentAnswer?.client_elapsed_ms ?? submission?.clientElapsedMs;
  const receivedAt = currentAnswer?.received_at ?? submission?.receivedAt;
  const elapsedMs = timingBasis === "client_monotonic_v1"
    ? clientElapsedMs ?? Number.NaN
    : receivedAt != null && openAt !== null ? Date.parse(receivedAt) - Date.parse(openAt) : Number.NaN;
  const hasAnswerTime = Number.isFinite(elapsedMs) && elapsedMs >= 0;
  const timerLabel = !isAnswerLocked ? "経過時間" : !hasAnswerTime ? "回答時間不明"
    : timingBasis === "client_monotonic_v1" ? "回答時間" : "受付までの時間";
  const selectedAnswerStatus =
    submission?.status ??
    (currentAnswer === undefined ? null : "accepted");

  const answerInstruction = !isAnswerLocked ? undefined
    : !hasAnswerTime ? "正式な回答時間を確認できないため、所要時間は表示できません。"
      : timingBasis === "client_monotonic_v1" ? "問題開始から選択までの端末計測時間です。採点にも同じ時間を使います。"
        : "過去の記録として、通信時間を含むサーバー受付までの時間を表示しています。";
  const selectOption = (optionId: string): void => {
    const elapsed = roundClock.captureElapsedMs();
    if (elapsed === null || !isAnswering || isAnswerLocked || openAt === null) return;
    void onSendAnswer({
      round_id: round.round_id,
      round_version: round.version,
      option_id: optionId,
      client_answer_id: createClientUuid(),
      client_elapsed_ms: elapsed,
    });
  };

  return (
    <ParticipantQuestionView
      question={question}
      roundNumber={round.round_number}
      timer={{ openAt, deadline, now: roundClock.now, readNow: roundClock.readNow,
        answer: isAnswerLocked ? { label: timerLabel, elapsedMs: hasAnswerTime ? elapsedMs : null } : undefined }}
      selectedAnswer={selectedOptionId === undefined || selectedAnswerStatus === null
        ? undefined : { optionId: selectedOptionId, status: selectedAnswerStatus }}
      answerInstruction={answerInstruction}
      showAnswerHint={!isAnswerLocked}
      onSelectOption={isAnswering && !isAnswerLocked ? selectOption : undefined}
    />
  );
}
