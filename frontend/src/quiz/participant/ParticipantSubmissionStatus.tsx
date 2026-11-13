import checkIcon from "../../assets/participant-2026/check-success.svg";
import warningIcon from "../../assets/participant-2026/warning.svg";
import type { PendingSubmission } from "./pendingSubmission";
import type { ParticipantSubmission } from "./participantUi";

export function ParticipantSubmissionStatus({
  isAnswerSendDisabled = false,
  submission,
  onSendAnswer,
  onVerifySubmission,
}: {
  isAnswerSendDisabled?: boolean;
  submission: ParticipantSubmission;
  onSendAnswer: (pending: PendingSubmission) => Promise<void>;
  onVerifySubmission: (target: ParticipantSubmission) => Promise<void>;
}) {
  return (
    <section
      className={`submission-status submission-${submission.status}`}
      aria-live="assertive"
    >
      <div className="submission-status-message">
        {submission.status === "accepted" ? <img alt="" src={checkIcon} /> :
          submission.status === "not-stored" || submission.status === "rejected"
            ? <img alt="" src={warningIcon} /> : null}
        <strong>{submission.status === "accepted" ? "回答済み" :
          submission.status === "submitting" ? "回答を送信中" :
          submission.status === "unknown" ? "回答を確認中" : "回答できませんでした"}</strong>
      </div>
      <p className={submission.status === "accepted" ? "sr-only" : "submission-status-detail"}>
        {submission.message}
      </p>
      {submission.status === "unknown" ? (
        <button
          className="secondary-button"
          onClick={() => void onVerifySubmission(submission)}
          type="button"
        >
          保存状態を確認
        </button>
      ) : null}
      {submission.status === "not-stored" ? (
        <button
          className="primary-button"
          disabled={isAnswerSendDisabled || submission.clientElapsedMs === null}
          onClick={() => {
            if (submission.clientElapsedMs === null) return;
            void onSendAnswer({
              round_id: submission.roundId,
              round_version: submission.roundVersion,
              option_id: submission.optionId,
              client_answer_id: submission.clientAnswerId,
              client_elapsed_ms: submission.clientElapsedMs,

            });
          }}
          type="button"
        >
          同じ回答を再送
        </button>
      ) : null}
    </section>
  );
}
