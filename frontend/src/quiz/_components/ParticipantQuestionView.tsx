import type { Question } from "../../api/contracts";
import selectedIcon from "../../assets/participant-2026/check-selected.svg";
import { ParticipantTimer, type ParticipantTimerProps } from "./ParticipantTimer";
import { ParticipantRoundHeader } from "./ParticipantRoundHeader";

type ParticipantQuestionViewProps = {
  question: Question;
  roundNumber: number;
  timer: ParticipantTimerProps;
  selectedAnswer?: {
    optionId: string;
    status: "submitting" | "accepted" | "unknown" | "not-stored" | "rejected";
  };
  answerInstruction?: string;
  showAnswerHint: boolean;
  onSelectOption?: (optionId: string) => void;
};

/** 回答計測・送信を持たず、参加者画面と閲覧プレビューで共通の表示を描画する。 */
export function ParticipantQuestionView({
  question, roundNumber, timer, selectedAnswer, answerInstruction, showAnswerHint, onSelectOption,
}: ParticipantQuestionViewProps) {
  return (
    <section className="participant-question">
      <ParticipantRoundHeader roundNumber={roundNumber} />
      <h2 className="participant-question-prompt" id="question-prompt">{question.prompt}</h2>
      <ParticipantTimer {...timer} />
      <fieldset
        className="answer-fieldset"
        aria-labelledby="question-prompt"
      >
        <legend className="sr-only">回答を1つ選択</legend>
        {answerInstruction === undefined ? null : (
          <p className="answer-instruction sr-only">{answerInstruction}</p>
        )}
        <div className="answer-grid answer-list">
          {question.options.map((option) => {
            const isSelected = selectedAnswer?.optionId === option.option_id;
            const answerStateId = `answer-status-${option.option_id}`;
            return (
              <button
                aria-describedby={
                  isSelected && selectedAnswer !== undefined
                    ? answerStateId
                    : undefined
                }
                aria-pressed={isSelected}
                className={
                  isSelected
                    ? "answer-button selected-answer"
                    : "answer-button"
                }
                disabled={onSelectOption === undefined}
                key={option.option_id}
                onClick={onSelectOption === undefined ? undefined : () => onSelectOption(option.option_id)}
                type="button"
              >
                <span className="answer-label">{option.label}</span>
                <span className="answer-content">{option.content}</span>
                {isSelected && selectedAnswer !== undefined ? (
                  <span
                    aria-hidden="true"
                    className={`answer-state answer-state-${selectedAnswer.status}`}
                    id={answerStateId}
                  >
                    <img alt="" src={selectedIcon} />
                    <span className="sr-only">
                    {selectedAnswer.status === "submitting"
                      ? "送信中"
                      : selectedAnswer.status === "accepted"
                        ? "受付済み"
                        : selectedAnswer.status === "unknown"
                          ? "確認中"
                          : selectedAnswer.status === "not-stored"
                            ? "再送可"
                            : "受付不可"}
                    </span>
                  </span>
                ) : null}
              </button>
            );
          })}
        </div>
      </fieldset>
      {showAnswerHint ? <p className="participant-answer-hint">答えを1つ選んでください。</p> : null}
    </section>
  );
}
