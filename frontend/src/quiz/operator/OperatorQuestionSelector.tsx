import {
  ArrowRightIcon,
  EyeIcon,
  LockKeyIcon,
} from "@phosphor-icons/react";
import { useId, useState } from "react";

import type { OperatorState } from "../../api/operatorContracts";
import "./operatorQuestionSelector.css";

type OperatorQuestionPreview = OperatorState["available_questions"][number];

type OperatorQuestionSelectorProps = {
  canSelectQuestion: boolean;
  currentQuestionId: string;
  isBusy: boolean;
  mode: "advance-round" | "switch-current-round";
  onQuestionChange: (questionId: string) => Promise<void>;
  questions: OperatorQuestionPreview[];
};

function defaultPreviewQuestion(
  questions: OperatorQuestionPreview[],
  currentQuestionId: string,
): OperatorQuestionPreview | null {
  const currentQuestion = questions.find(
    (question) => question.question_id === currentQuestionId,
  );
  const candidates = questions
    .filter((question) => question.question_id !== currentQuestionId)
    .sort((left, right) => left.display_order - right.display_order);
  if (currentQuestion === undefined) {
    return candidates[0] ?? null;
  }
  return (
    candidates.find(
      (question) => question.display_order > currentQuestion.display_order,
    ) ??
    candidates[0] ??
    null
  );
}

export function OperatorQuestionSelector({
  canSelectQuestion,
  currentQuestionId,
  isBusy,
  mode,
  onQuestionChange,
  questions,
}: OperatorQuestionSelectorProps) {
  const isAdvanceMode = mode === "advance-round";
  const previewRegionId = useId();
  const previewTitleId = useId();
  const initialPreview = defaultPreviewQuestion(questions, currentQuestionId);
  const [previewQuestionId, setPreviewQuestionId] = useState(
    initialPreview?.question_id ?? null,
  );
  const previewQuestion =
    questions.find(
      (question) =>
        question.question_id === previewQuestionId &&
        question.question_id !== currentQuestionId,
    ) ?? initialPreview;

  if (previewQuestion === null) {
    return null;
  }

  const confirmButton = (
          <button
            className="primary-button"
            disabled={isBusy || !canSelectQuestion}
            onClick={() => void onQuestionChange(previewQuestion.question_id)}
            type="button"
          >
            {isAdvanceMode
              ? `backupを検証して登録問題 ${previewQuestion.display_order}へ進む`
              : `登録問題 ${previewQuestion.display_order}へ切り替える`}
            <ArrowRightIcon aria-hidden="true" size={20} weight="bold" />
          </button>
  );

  return (
    <div className="operator-question-selection">
      {isAdvanceMode ? confirmButton : null}
    <details className="operator-candidates">
      <summary>
        <span>{isAdvanceMode ? "次の問題" : "問題の候補"} · 登録問題 {previewQuestion.display_order}</span>
        <span className="operator-candidates-label">候補を確認</span>
      </summary>
      <div className="question-selector">

      <div
        aria-label={
          isAdvanceMode
            ? "次のRoundで出題する問題"
            : "プレビューする問題"
        }
        className="question-switcher"
        role="group"
      >
        {questions.map((question) => {
          const isCurrent = question.question_id === currentQuestionId;
          const isPreview = question.question_id === previewQuestion.question_id;
          return (
            <button
              aria-pressed={isPreview}
              aria-controls={isCurrent ? undefined : previewRegionId}
              className={`question-select-button${
                isCurrent ? " question-current" : ""
              }`}
              disabled={isBusy || isCurrent}
              key={question.question_id}
              onClick={() => setPreviewQuestionId(question.question_id)}
              type="button"
            >
              <span>登録問題 {question.display_order}</span>
              <strong className="sr-only">{question.prompt}</strong>
              <small className="sr-only">
                {isCurrent
                  ? isAdvanceMode
                    ? "出題済み"
                    : "現在の出題"
                  : isPreview
                    ? "プレビュー中"
                    : "プレビューする"}
              </small>
            </button>
          );
        })}
      </div>

      <section
        aria-labelledby={previewTitleId}
        aria-live="polite"
        aria-atomic="true"
        className="question-preview"
        id={previewRegionId}
      >
        <div className="question-preview-heading">
          <div>
            <p className="operator-eyebrow">
              <EyeIcon aria-hidden="true" size={16} />候補のプレビュー
            </p>
            <h3 id={previewTitleId}>
              登録問題 {previewQuestion.display_order} {previewQuestion.prompt}
            </h3>
          </div>
          <span className="operator-only-badge">
            <LockKeyIcon aria-hidden="true" size={17} weight="bold" />
            運営専用
          </span>
        </div>

        <div className="operator-options" aria-label="プレビュー選択肢">
          {previewQuestion.options.map((option) => (
            <div
              className={
                option.is_correct ? "operator-correct-option" : undefined
              }
              key={option.option_id}
            >
              <strong>{option.label}</strong>
              <span>{option.content}</span>
              {option.is_correct ? <small>正解</small> : null}
            </div>
          ))}
        </div>

        <div className="question-preview-footer">
          <p className="muted">
            {isAdvanceMode
              ? "次のRoundを作成する前の候補情報と正解は運営専用です。参加者画面には送信されません。"
              : "切替前の候補情報と正解は運営専用です。参加者画面には送信されません。"}
          </p>
          {!isAdvanceMode ? confirmButton : null}
        </div>
        {!canSelectQuestion ? (
          <p className="muted question-preview-blocked">
            {isAdvanceMode
              ? "現在の状態では次のRoundへ進めません。"
              : "問題の切替は、問題を確定して配信する前だけ行えます。"}
          </p>
        ) : null}
      </section>
      </div>
    </details>
    </div>
  );
}
