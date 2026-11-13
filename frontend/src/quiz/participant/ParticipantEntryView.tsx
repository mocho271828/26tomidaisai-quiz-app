import type { FormEvent } from "react";

import arrowRight from "../../assets/participant-2026/arrow-right.svg";
import { ErrorNotice, PageFrame } from "../_components/PageFrame";
import { TommyMascot } from "../_components/TommyMascot";
import { PRACTICE_ROUND_COUNT } from "../_utils/roundPresentation";
import { ParticipantHeader } from "./ParticipantHeader";
import { formatEventStartCountdown } from "./participantUi";
import { participantUiConfig } from "./participantUiConfig";
import type { ServerClock } from "../_hooks/useServerClock";
import { useDisplayTime } from "../_hooks/useDisplayTime";

type ParticipantEntryViewProps = {
  isJoinDisabled: boolean;
  isJoining: boolean;
  onJoin: (event: FormEvent<HTMLFormElement>) => void;
  onRetrySessionCheck: () => void;
  pageError: string | null;
  phase: "checking" | "join";
  serverClock: ServerClock;
};

function EventCountdown({ clock }: { clock: ServerClock }) {
  const until = participantUiConfig.eventStartAt === null ? Number.NEGATIVE_INFINITY : Date.parse(participantUiConfig.eventStartAt);
  const now = useDisplayTime(clock, until, clock.isSynchronized);
  const countdown = clock.isSynchronized ? formatEventStartCountdown(participantUiConfig.eventStartAt, now) : null;
  return countdown === null ? null : <p className="participant-event-countdown" role="timer" aria-live="off">{countdown}</p>;
}

export function ParticipantEntryView({
  isJoinDisabled, isJoining, onJoin, onRetrySessionCheck, pageError, phase, serverClock,
}: ParticipantEntryViewProps) {
  return (
    <PageFrame audience="参加者" layout="answering" rootClassName="participant-entry-page" title="富大祭2026 ゲストクイズ">
      <div className="participant-entry-screen">
        <ParticipantHeader />
        {phase === "checking" ? (
          <section className="participant-entry-loading" aria-busy={pageError === null}>
            <h2>{pageError === null ? <>しばらく<br />お待ちください</> : <>クイズを<br />開けませんでした</>}</h2>
            <div className="participant-entry-mascot"><TommyMascot size="large" variant={pageError === null ? "hurrying" : "surprised"} /></div>
            {pageError === null ? (
              <p className="sr-only" role="status">参加状態を確認しています</p>
            ) : (
              <>
                <ErrorNotice>{pageError}</ErrorNotice>
                <button className="participant-primary-button" onClick={onRetrySessionCheck} type="button">
                  参加状態を再確認<img alt="" src={arrowRight} />
                </button>
              </>
            )}
          </section>
        ) : (
          <form className="participant-entry-form" onSubmit={onJoin}>
            <h2>ゲストクイズ</h2>
            <EventCountdown clock={serverClock} />
            <div className="participant-entry-mascot"><TommyMascot size="large" variant="greeting" /></div>
            <div className="participant-quiz-format">
              <span><strong>4択</strong>で回答</span><span>練習<strong>{PRACTICE_ROUND_COUNT}問</strong>＋本番</span>
            </div>
            {pageError !== null ? <ErrorNotice>{pageError}</ErrorNotice> : null}
            <button className="participant-primary-button participant-join-button" disabled={isJoinDisabled} type="submit">
              {isJoining ? "参加しています…" : "参加する"}<img alt="" src={arrowRight} />
            </button>
            <p className="participant-entry-ranking-note">IDはランキングに表示されます。</p>
          </form>
        )}
      </div>
    </PageFrame>
  );
}
