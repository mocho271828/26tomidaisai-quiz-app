import type { PublishedResult, Question } from "../../api/contracts";
import distributionCheck from "../../assets/participant-2026/distribution-check.svg";

type ParticipantAnswerDistributionProps = {
  question: Question;
  result: PublishedResult;
  selectedOptionId?: string;
};

export function ParticipantAnswerDistribution({
  question,
  result,
  selectedOptionId,
}: ParticipantAnswerDistributionProps) {
  if (result.distribution === null) {
    return null;
  }
  const resultByOption = new Map(
    result.distribution.options.map((option) => [option.option_id, option]),
  );

  return (
    <section
      aria-label="回答の分布"
      className="participant-distribution-result"
    >
      <h2 className="participant-result-question">{question.prompt}</h2>
      <p className="participant-distribution-count"><strong>{result.distribution.valid_answer_count.toLocaleString("ja-JP")}</strong><span>人が回答</span></p>
      <ul className="participant-distribution-options">
        {question.options.map((option) => {
          const optionResult = resultByOption.get(option.option_id);
          if (optionResult === undefined) {
            return null;
          }
          const isCorrect = option.option_id === result.correct_option_id;
          const isSelected = option.option_id === selectedOptionId;
          return (
            <li
              className={isCorrect ? "result-correct" : undefined}
              key={option.option_id}
            >
              <div className="participant-distribution-heading">
                <strong>{option.label}</strong>
                <span>{option.content}</span>
                <b>{optionResult.response_percentage}%</b>
              </div>
              <div
                aria-label={`${option.label}. ${option.content} の回答割合`}
                aria-valuemax={100}
                aria-valuemin={0}
                aria-valuenow={optionResult.response_percentage}
                className="participant-distribution-track"
                role="progressbar"
              >
                <span style={{ width: `${optionResult.response_percentage}%` }} />
              </div>
              {isCorrect || isSelected ? (
                <small>
                  {isCorrect ? <img alt="" src={distributionCheck} /> : null}
                  {isCorrect ? <span>正解</span> : null}
                  {isCorrect && isSelected ? " · " : null}
                  {isSelected ? <span>あなたの回答</span> : null}
                </small>
              ) : null}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
