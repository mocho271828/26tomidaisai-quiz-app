import clockIcon from "../../assets/participant-2026/clock-muted.svg";
import { useDisplayTime, type DisplayClock } from "../_hooks/useDisplayTime";
import { elapsedSeconds } from "../_utils/answerTime";

export type ParticipantTimerProps = DisplayClock & {
  openAt: string | null;
  deadline: string | null;
  answer?: { label: string; elapsedMs: number | null };
};

function answerElapsedTime(
  openAt: string | null,
  deadline: string | null,
  now: number,
): string | null {
  if (openAt === null) {
    return null;
  }
  const end = deadline === null ? now : Math.min(Date.parse(deadline), now);
  const elapsedMs = Math.max(0, end - Date.parse(openAt));
  return elapsedSeconds(elapsedMs);
}

function answerWindowProgress(
  openAt: string | null,
  deadline: string | null,
  now: number,
): number {
  if (openAt === null || deadline === null) {
    return 0;
  }
  const start = Date.parse(openAt);
  const duration = Date.parse(deadline) - start;
  if (duration <= 0) {
    return 0;
  }
  const elapsed = now - start;
  return Math.min(100, Math.max(0, (elapsed / duration) * 100));
}

export function ParticipantTimer(timer: ParticipantTimerProps) {
  const { openAt, deadline, answer: answerTiming } = timer;
  const now = useDisplayTime(timer, deadline === null ? Number.NEGATIVE_INFINITY : Date.parse(deadline), answerTiming === undefined);
  const timerLabel = answerTiming?.label ?? "経過時間";
  const timerValue = answerTiming === undefined
    ? answerElapsedTime(openAt, deadline, now)
    : answerTiming.elapsedMs === null ? null : elapsedSeconds(answerTiming.elapsedMs);
  const answerProgress = answerWindowProgress(openAt, deadline,
    answerTiming !== undefined && answerTiming.elapsedMs !== null && openAt !== null
      ? Date.parse(openAt) + answerTiming.elapsedMs : now);
  const answerDuration = openAt !== null && deadline !== null
    ? Math.max(0, (Date.parse(deadline) - Date.parse(openAt)) / 1_000).toFixed(1)
    : null;

  return (
    <div className="participant-timer-card">
      <div className="participant-timer-values">
        <span className="participant-timer-label"><img alt="" src={clockIcon} />{timerLabel}</span>
        <div className="countdown" aria-label={timerValue === null ? timerLabel : `${timerLabel}${timerValue}秒`}>
          <strong>{timerValue ?? "—"}</strong>
          {timerValue !== null ? <span>{answerDuration === null ? "秒" : ` / ${answerDuration} 秒`}</span> : null}
        </div>
      </div>
      {answerTiming === undefined || answerTiming.elapsedMs !== null ? (
        <div className="quiz-stage-progress" aria-hidden="true">
          <span className="quiz-stage-progress-bar" style={{ width: `${answerProgress}%` }} />
        </div>
      ) : null}
    </div>
  );
}
