import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ParticipantState } from "../../api/contracts";
import { QuizWaitingStage } from "../_components/QuizWaitingStage";
import { participantWaitingStageProps } from "./participantWaitingStage";

const round: NonNullable<ParticipantState["current_round"]> = {
  round_id: "60000000-0000-4000-8000-000000000002",
  round_number: 3,
  version: 74,
  state_version: 12,
  state: "Scheduled",
  question: { question_id: "60000000-0000-4000-8000-000000000003", prompt: "架空問題", options: [{ option_id: "60000000-0000-4000-8000-000000000004", label: "A", content: "架空選択肢" }] },
  open_at: "2026-09-23T05:00:10Z",
  standard_close_at: "2026-09-23T05:00:20Z",
};

const waitingContext = {
  isAnswering: false,
  isSynchronized: true,
  now: Date.parse("2026-09-23T05:00:04Z"),
  round,
  startDelaySeconds: 10,
};

function renderWaiting(context = waitingContext) {
  const props = participantWaitingStageProps(context);
  if (props === null) {
    throw new Error("Expected a waiting stage");
  }
  return { ...render(<QuizWaitingStage {...props} publicLabel="ABC" />), props };
}

describe("参加者の開始待機", () => {
  it.each(["PreparingAssets", "ReadyToSchedule", "Scheduled", "Open", "VoidCandidate"] as const)(
    "%sの待機中も割り当てられたIDを下部カードへ表示する",
    (state) => {
      renderWaiting({ ...waitingContext, isSynchronized: state !== "Open", round: { ...round, state } });
      const ticket = screen.getByText("あなたの参加者ID").parentElement;
      expect(ticket).toHaveClass("participant-ticket");
      expect(ticket).toHaveTextContent("ABC");
      expect(ticket?.closest(".participant-waiting-footer")).not.toBeNull();
    },
  );

  it("IDをまだ取得していない待機には仮のカードを表示しない", () => {
    const props = participantWaitingStageProps(waitingContext);
    if (props === null) throw new Error("Expected a waiting stage");
    render(<QuizWaitingStage {...props} />);
    expect(screen.queryByText("あなたの参加者ID")).not.toBeInTheDocument();
  });

  it("APIの出題番号と同期済み残り時間を表示し、残り時間を更新する", () => {
    const { rerender } = renderWaiting();
    expect(screen.getByText("第2問")).toBeVisible();
    expect(screen.queryByText("第74問")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "まもなくスタート！" })).toBeVisible();
    expect(screen.getByRole("timer")).toHaveAccessibleName("回答開始まで残り6.0秒");
    expect(screen.getByRole("timer")).toHaveTextContent("回答開始まで6.0秒");
    expect(screen.getByRole("timer")).toHaveAttribute("aria-live", "off");

    const nextProps = participantWaitingStageProps({
      ...waitingContext,
      now: Date.parse("2026-09-23T05:00:08.500Z"),
    });
    if (nextProps === null) {
      throw new Error("Expected a waiting stage");
    }
    rerender(<QuizWaitingStage {...nextProps} publicLabel="ABC" />);
    expect(screen.getByRole("timer")).toHaveTextContent("回答開始まで1.5秒");
  });

  it("時刻未同期時はカウントダウンを出さず確認中であることを表示する", () => {
    renderWaiting({ ...waitingContext, isSynchronized: false });
    expect(screen.queryByRole("timer")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "しばらく お待ちください" })).toBeVisible();
  });

  it("開始時刻を過ぎても回答に必要な条件が揃わなければ負の秒数を表示しない", () => {
    const { props } = renderWaiting({
      ...waitingContext,
      now: Date.parse("2026-09-23T05:00:11Z"),
    });
    expect(props.countdown).toBeUndefined();
    expect(screen.queryByRole("timer")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "しばらく お待ちください" })).toBeVisible();
  });
  it.each(["ReadyToSchedule", "Scheduled", "Open"] as const)("%sでも問題未受信なら受信中を表示する", (state) => {
    const missingQuestion = { ...round };
    delete missingQuestion.question;
    const { props } = renderWaiting({ ...waitingContext, round: { ...missingQuestion, state } });
    expect(props.title).toBe("問題データを受信しています");
    expect(screen.queryByRole("timer")).not.toBeInTheDocument();
    expect(screen.queryByText("まもなくスタート！")).not.toBeInTheDocument();
  });

  it("配信確定でも時刻未同期を準備完了扱いにしない", () => {
    renderWaiting({ ...waitingContext, isSynchronized: false, round: { ...round, state: "ReadyToSchedule" } });
    expect(screen.getByRole("heading", { name: "しばらく お待ちください" })).toBeVisible();
    expect(screen.queryByText("まもなくスタート！")).not.toBeInTheDocument();
  });

});
