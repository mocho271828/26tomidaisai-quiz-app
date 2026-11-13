import {
  ArrowClockwiseIcon,
  HourglassMediumIcon,
  LockKeyIcon,
  ShieldCheckIcon,
  TimerIcon,
} from "@phosphor-icons/react";

import type { TommyMascotVariant } from "./TommyMascot";
import { TommyMascot } from "./TommyMascot";
import { ParticipantIdTicket } from "./ParticipantIdTicket";
import { useDisplayTime, type DisplayClock } from "../_hooks/useDisplayTime";
import { remainingSeconds } from "../_utils/answerTime";

export type QuizWaitingPhase =
  | "answering"
  | "idle"
  | "preparing"
  | "scheduled"
  | "validating";

export type QuizWaitingStageProps = {
  autoLabel?: string;
  countdown?: string | null;
  countdownClock?: DisplayClock & { until: number };
  description: string;
  displayHeading?: string;
  footer: string;
  isAutoRefresh?: boolean;
  kicker: string;
  phase: QuizWaitingPhase;
  presentation?: "participant";
  participantLayout?: "ticket" | "ready" | "recovery";
  publicLabel?: string;
  roundLabel: string;
  speech?: string;
  title: string;
};

const mascotVariants: Record<QuizWaitingPhase, TommyMascotVariant> = {
  answering: "thinking",
  idle: "celebrating",
  preparing: "greeting",
  scheduled: "running",
  validating: "surprised",
};

function ParticipantCountdown({ value, clock }: { value: string; clock?: DisplayClock & { until: number } }) {
  const now = useDisplayTime(clock ?? { now: 0 }, clock?.until ?? 0);
  const countdown = clock?.readNow === undefined ? value : remainingSeconds(Math.max(0, clock.until - now));
  return (
    <div aria-label={`回答開始まで残り${countdown}秒`} aria-live="off"
      className="participant-waiting-countdown" role="timer">
      <span>回答開始まで</span><strong>{countdown}</strong><span>秒</span>
    </div>
  );
}

function PhaseIcon({ phase }: { phase: QuizWaitingPhase }) {
  if (phase === "scheduled" || phase === "answering") {
    return <TimerIcon aria-hidden="true" size={20} weight="bold" />;
  }
  if (phase === "validating") {
    return <ShieldCheckIcon aria-hidden="true" size={20} weight="bold" />;
  }
  return <HourglassMediumIcon aria-hidden="true" size={20} weight="bold" />;
}

export function QuizWaitingStage({
  autoLabel = "自動で切り替わります",
  countdown: countdownValue,
  countdownClock,
  description,
  displayHeading,
  footer,
  isAutoRefresh = true,
  kicker,
  phase,
  presentation,
  participantLayout,
  publicLabel,
  roundLabel,
  speech,
  title,
}: QuizWaitingStageProps) {
  const titleId = `quiz-waiting-title-${phase}`;

  if (presentation === "participant") {
    const hasCountdown = countdownValue !== undefined && countdownValue !== null;

    return (
      <section
        aria-labelledby={titleId}
        className={`participant-waiting-screen participant-waiting-screen-${hasCountdown ? "countdown" : phase}`}
      >
        {hasCountdown ? <span className="participant-round-badge">{roundLabel}</span> : null}
        <h2 className="participant-waiting-heading" id={titleId}>
          {(hasCountdown ? displayHeading ?? title : speech ?? displayHeading ?? title)
            .split("\n").map((line, index) => <span key={index}>{line}</span>)}
        </h2>
        {hasCountdown ? (
          <ParticipantCountdown value={countdownValue} clock={countdownClock} />
        ) : null}
        <div className="participant-waiting-character">
          <TommyMascot size="large" variant={mascotVariants[phase]} />
        </div>
        <div className="participant-waiting-footer">
          {!hasCountdown && participantLayout === "ready" ? (
            <p className="participant-waiting-status-label">司会の合図をお待ちください。</p>
          ) : null}
          {publicLabel !== undefined ? <ParticipantIdTicket publicLabel={publicLabel} /> : null}
        </div>
        <p className="sr-only">{description}</p>
        <p className="sr-only">{footer}</p>
      </section>
    );
  }

  return (
    <section
      className={`quiz-waiting-stage quiz-waiting-stage-${phase}`}
      aria-labelledby={titleId}
    >
      <div className="waiting-stage-topline">
        <span className="waiting-stage-phase">
          <PhaseIcon phase={phase} />
          {roundLabel}
        </span>
        <span className="waiting-stage-auto">
          {isAutoRefresh ? (
            <ArrowClockwiseIcon
              aria-hidden="true"
              className="waiting-stage-refresh-icon"
              size={17}
              weight="bold"
            />
          ) : (
            <ShieldCheckIcon aria-hidden="true" size={17} weight="bold" />
          )}
          {autoLabel}
        </span>
      </div>

      <div className="waiting-stage-main">
        <div className="waiting-stage-mascot">
          <TommyMascot size="large" variant={mascotVariants[phase]} />
        </div>
        <div className="waiting-stage-copy">
          <p className="waiting-stage-status">{kicker}</p>
          <h2 id={titleId}>{title}</h2>
          <p>{description}</p>
        </div>
        {countdownValue !== undefined && countdownValue !== null ? (
          <div
            className="waiting-stage-countdown"
            aria-label={`回答開始まで残り${countdownValue}秒`}
            aria-live="off"
            role="timer"
          >
            <strong>{countdownValue}</strong>
            <span>秒</span>
          </div>
        ) : null}
      </div>

      <p className="waiting-stage-footer">
        <LockKeyIcon aria-hidden="true" size={20} weight="bold" />
        <span>{footer}</span>
      </p>
    </section>
  );
}

export function QuizStageAnnouncer({ message }: { message: string }) {
  return (
    <p
      className="sr-only quiz-stage-announcer"
      aria-atomic="true"
      aria-live="polite"
      data-testid="quiz-stage-announcer"
      role="status"
    >
      {message}
    </p>
  );
}
