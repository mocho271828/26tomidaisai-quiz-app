import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type {
  ParticipantOutcome,
  Question,
  RoundState,
} from "../../api/contracts";
import type { ParticipantViewState as ParticipantState } from "./participantState";
import { ParticipantJoinedView } from "./ParticipantJoinedView";
import type { ParticipantSubmission } from "./participantUi";
import type { BrowserObservation } from "../_utils/browserObservation";

const ids = {
  event: "60000000-0000-4000-8000-000000000001",
  optionA: "60000000-0000-4000-8000-000000000004",
  optionB: "60000000-0000-4000-8000-000000000005",
  question: "60000000-0000-4000-8000-000000000003",
  round: "60000000-0000-4000-8000-000000000002",
  nextRound: "60000000-0000-4000-8000-000000000012",
} as const;

const now = Date.parse("2026-08-08T01:00:02Z");

const question: Question = {
  options: [
    { content: "3", label: "A", option_id: ids.optionA },
    { content: "4", label: "B", option_id: ids.optionB },
  ],
  prompt: "2 + 2 は？",
  question_id: ids.question,
};

function participantProjection(state: RoundState): ParticipantState {
  const isPreparing =
    state === "PreparingAssets" || state === "ReadyToSchedule";
  const isScheduled = state === "Scheduled";
  const isReadyToReveal = state === "ReadyToReveal";
  const isClosed =
    isReadyToReveal || state === "AwaitingValidation" || state === "Reveal";

  return {
    award_ceremony: null,
    projected_at: "2026-08-08T01:00:02Z",
    event: {
      timing_basis: "client_monotonic_v1", arrival_grace_ms: 2000, clock_tolerance_ms: 500, client_ahead_tolerance_ms: 2500,
      event_id: ids.event,
      event_state: "InProgress",
      event_title: "富大祭クイズ",
      event_version: 2,
      start_delay_seconds: 3,
    },
    participant: {
      is_ranking_eligible: true,
      public_label: "ABC",
    },
    current_round: {
      open_at: isPreparing
        ? null
        : isScheduled
          ? "2026-08-08T01:00:05Z"
          : "2026-08-08T01:00:00Z",
      question,
      round_id: ids.round,
      round_number: 2,
      standard_close_at: isPreparing
        ? null
        : isClosed
          ? "2026-08-08T01:00:01Z"
          : "2026-08-08T01:00:07Z",
      state,
      state_version: 12,
      version: 3,
    },
    latest_result: null,
    result_visibility: { distribution: false, personal_ranking: false, overall_ranking: false },
    latest_ranking_snapshot_version: null,
    personal_result_delivery: { status: "none", revision: null },
  };
}

function publishedProjection(outcome: ParticipantOutcome): ParticipantState {
  return {
    ...participantProjection("AwaitingValidation"),
    current_round: null,
    latest_result: {
      open_at: "2026-08-08T01:00:00Z",
      question,
      result: {
        correct_option_id: ids.optionB,
        distribution: {
          valid_answer_count: 5,
          options: [
            {
              content: "3",
              is_correct: false,
              label: "A",
              option_id: ids.optionA,
              response_percentage: 40,
              valid_response_count: 2,
            },
            {
              content: "4",
              is_correct: true,
              label: "B",
              option_id: ids.optionB,
              response_percentage: 60,
              valid_response_count: 3,
            },
          ],
        },
        outcome,
        publication: "Published",
      },
      round_id: ids.round,
      round_number: 2,
      selected_option_id: null,
      standard_close_at: "2026-08-08T01:00:05Z",
      state: "Reveal",
      state_version: 15,
      version: 3,
    },
  };
}

function joinedView(
  projection: ParticipantState,
  submission: ParticipantSubmission | null = null,
  onVerifySubmission = vi.fn(async () => undefined),
  isSynchronized = true,
) {
  return (
    <ParticipantJoinedView
      isStateFresh={true}
      onSendAnswer={vi.fn(async () => undefined)}
      onVerifySubmission={onVerifySubmission}
      pageError={null}
      projection={projection}
      serverClock={{ isSynchronized, now, monotonicOffsetMs: performance.now() - now }}
      submission={submission}
    />
  );
}

function renderJoinedView(
  projection: ParticipantState,
  submission: ParticipantSubmission | null = null,
) {
  return render(joinedView(projection, submission));
}

describe("ParticipantJoinedView", () => {
  it.each(["unknown", "accepted"] as const)("選択後の%sは時刻再同期失敗で受付DOMを消さず操作を無効にする", (status) => {
    const projection = participantProjection("Open");
    const submission: ParticipantSubmission = {
      clientElapsedMs: 2000, timingBasis: "client_monotonic_v1",
      clientAnswerId: "pending-answer", optionId: ids.optionA, roundId: ids.round,
      roundVersion: 3, receivedAt: null,
      status, message: "保存状態を確認しています。",
    };
    const verify = vi.fn(async () => undefined);
    const { rerender, container } = render(joinedView(projection, submission, verify));
    const selected = screen.getByRole("button", { name: /A 3/u });
    const state = container.querySelector(`.answer-state-${status}`);

    rerender(joinedView(projection, submission, verify, false));

    expect(screen.getByRole("heading", { name: question.prompt })).toBeVisible();
    expect(screen.getByRole("button", { name: /A 3/u })).toBe(selected);
    expect(state?.isConnected).toBe(true);
    expect(selected).toBeDisabled();
    expect(screen.getByRole("button", { name: "B 4" })).toBeDisabled();
    expect(screen.queryByRole("heading", { name: "しばらく お待ちください" })).not.toBeInTheDocument();
  });

  it("時刻未同期でもAPIから取得した保存済み回答の受付を表示する", () => {
    const projection = participantProjection("Open");
    projection.answer = {
      client_elapsed_ms: 2000, timing_basis: "client_monotonic_v1",
      answer_id: ids.optionA, client_answer_id: ids.optionB, round_id: ids.round,
      option_id: ids.optionA, received_at: new Date(now).toISOString(),
    };
    const { container } = render(joinedView(projection, null, undefined, false));

    expect(container.querySelector(".answer-state-accepted")).toBeVisible();
    expect(screen.getByRole("button", { name: /A 3/u })).toBeDisabled();
  });

  it("初回時刻未同期かつ未回答の問は従来どおり問題を表示しない", () => {
    render(joinedView(participantProjection("Open"), null, undefined, false));

    expect(screen.queryByText(question.prompt)).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "しばらく お待ちください" })).toBeVisible();
  });

  it("再読み込みしたpendingだけでは時刻未同期の開始前問題を表示しない", () => {
    const submission: ParticipantSubmission = {
      clientElapsedMs: 2000, timingBasis: "client_monotonic_v1",
      clientAnswerId: "pending-answer", optionId: ids.optionA, roundId: ids.round,
      roundVersion: 3, receivedAt: null,
      status: "unknown", message: "保存状態を確認しています。",
    };
    render(joinedView(participantProjection("Scheduled"), submission, undefined, false));

    expect(screen.queryByText(question.prompt)).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "しばらく お待ちください" })).toBeVisible();
  });

  it.each((["ReadyToSchedule", "Scheduled", "Open"] as const).flatMap((state) => [
    { state, roundNumber: 1, label: "第1問（練習）", isPractice: true },
    { state, roundNumber: 2, label: "第1問", isPractice: false },
    { state, roundNumber: 3, label: "第2問", isPractice: false },
    { state, roundNumber: 9, label: "第8問", isPractice: false },
  ]))(
    "$stateの実出題$roundNumberを$labelとして案内する",
    ({ state, roundNumber, label, isPractice }) => {
      const projection = participantProjection(state);
      projection.current_round!.round_number = roundNumber;

      renderJoinedView(projection);

      expect(screen.getByText(label)).toBeVisible();
      if (state === "Open") expect(screen.getByText(label)).toHaveAttribute("data-round-number", `${roundNumber}`);
      const practiceNotice = screen.queryByText("練習問題の得点はランキングに加算されません");
      if (isPractice) expect(practiceNotice).toBeVisible();
      else expect(practiceNotice).not.toBeInTheDocument();
    },
  );

  it("練習1問目の得点を表示し、本番第1問の準備中も総合点と区別する", async () => {
    const user = userEvent.setup();
    const projection = publishedProjection({
      timing_basis: "server_received_v1",
      status: "Scored",
      is_correct: true,
      total_points: 470,
      base_points: 400, speed_points: 70, elapsed_ms: 3000,
      scoring_policy_version: "v1",
      speed_cohort: "Correct",
      speed_rank: 1,
      speed_cohort_size: 2,
    });
    projection.latest_result!.round_number = 1;
    projection.current_round = {
      ...participantProjection("PreparingAssets").current_round!,
      round_id: ids.nextRound,
      round_number: 2,
    };
    projection.ranking = {
      correct_count: 0,
      overall_rank: 1,
      snapshot_version: 1,
      top_rows: null,
      total_points: 0,
    };

    renderJoinedView(projection);

    expect(screen.getByText("第1問 · 練習")).toBeVisible();
    expect(screen.getByText("470")).toBeVisible();
    await user.click(screen.getByRole("tab", { name: "ランキング" }));
    expect(screen.getByText("ID ABC · 0 pt")).toBeVisible();
    expect(screen.getByText(
      /練習問題の得点は含みません/,
    )).toBeVisible();
  });

  it("本番問題を配信したら練習得点を消し、本番第1問の得点だけを表示する", () => {
    const practice = publishedProjection({
      timing_basis: "server_received_v1",
      status: "Scored", is_correct: true, total_points: 470,
      base_points: 400, speed_points: 70, elapsed_ms: 3000,
      scoring_policy_version: "v1", speed_cohort: "Correct", speed_rank: 1, speed_cohort_size: 2,
    });
    practice.latest_result!.round_number = 1;
    practice.current_round = {
      ...participantProjection("PreparingAssets").current_round!,
      round_id: ids.nextRound,
      round_number: 2,
    };
    const { rerender } = renderJoinedView(practice);
    expect(screen.getByText("470")).toBeVisible();
    expect(screen.getByText("第1問 · 練習")).toBeVisible();

    rerender(joinedView({
      ...practice,
      current_round: { ...practice.current_round, state: "ReadyToSchedule" },
    }));
    expect(screen.getByRole("heading", { name: "第1問 もうすぐ始まるよ!!" })).toBeVisible();
    expect(screen.queryByText("470")).not.toBeInTheDocument();
    expect(screen.queryByRole("tab")).not.toBeInTheDocument();

    const official = publishedProjection({
      timing_basis: "server_received_v1",
      status: "Scored", is_correct: true, total_points: 450,
      base_points: 400, speed_points: 50, elapsed_ms: 5000,
      scoring_policy_version: "v1", speed_cohort: "Correct", speed_rank: 2, speed_cohort_size: 2,
    });
    official.latest_result!.round_id = ids.nextRound;
    official.ranking = {
      correct_count: 1, overall_rank: 2, snapshot_version: 2, top_rows: null, total_points: 450,
    };
    rerender(joinedView(official));
    expect(screen.getByText("第1問")).toBeVisible();
    expect(screen.getByText("今回の得点").parentElement).toHaveTextContent("450pt");
    expect(screen.getByText("総合得点").parentElement).toHaveTextContent("450 pt");
    expect(screen.queryByText("470")).not.toBeInTheDocument();
    expect(screen.queryByText(/今回の得点は総合に加算されません/)).not.toBeInTheDocument();
  });

  it("途中参加でもAPIで公開された本人順位を表示する", async () => {
    const user = userEvent.setup();
    const projection = publishedProjection({ status: "Unanswered" });
    projection.participant.is_ranking_eligible = false;
    projection.ranking = {
      correct_count: 1,
      overall_rank: 2,
      snapshot_version: 3,
      top_rows: null,
      total_points: 450,
    };

    renderJoinedView(projection);

    expect(screen.queryByText(/ランキング対象外/)).not.toBeInTheDocument();
    expect(screen.queryByText(/これは練習問題です/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "ランキング" }));
    expect(screen.getByText("ID ABC · 450 pt")).toBeVisible();
  });

  it.each([
    "PreparingAssets",
    "ReadyToSchedule",
    "Scheduled",
    "Open",
    "ReadyToReveal",
    "AwaitingValidation",
    "Reveal",
  ] as const)("%sの本番番号には更新versionではなく実出題番号から練習1問を除いた番号を表示する", (state) => {
    const projection = participantProjection(state);
    projection.current_round = {
      ...projection.current_round!,
      round_number: 3,
      version: 74,
    };

    renderJoinedView(projection);

    if (state === "PreparingAssets") {
      expect(screen.getByRole("heading", { name: "次の問題を お待ちください" })).toBeVisible();
    } else {
      expect(screen.getByText("第2問")).toBeVisible();
    }
    expect(screen.queryByText(/第74問/)).not.toBeInTheDocument();
  });

  it("結果の再表示でも終了時点の見出しに直近結果のround_numberを使う", async () => {
    const user = userEvent.setup();
    const projection = publishedProjection({ status: "Unanswered" });
    projection.latest_result = {
      ...projection.latest_result!,
      round_number: 3,
      version: 74,
    };
    projection.ranking = {
      correct_count: 2,
      overall_rank: 3,
      snapshot_version: 4,
      top_rows: null,
      total_points: 200,
    };
    const { unmount } = renderJoinedView(projection);
    await user.click(screen.getByRole("tab", { name: "ランキング" }));
    expect(screen.getByRole("heading", { name: "第2問終了時点" })).toBeVisible();
    expect(screen.queryByText(/第74問/)).not.toBeInTheDocument();

    unmount();
    renderJoinedView({
      ...projection,
      current_round: {
        ...participantProjection("PreparingAssets").current_round!,
        round_id: ids.nextRound,
        round_number: 4,
        version: 80,
      },
    });
    await user.click(screen.getByRole("tab", { name: "ランキング" }));
    expect(screen.getByRole("heading", { name: "第2問終了時点" })).toBeVisible();
    expect(screen.queryByText(/第(?:5|74|80)問/)).not.toBeInTheDocument();
  });

  it.each([
    ["PreparingAssets", "次の問題を お待ちください"],
    ["ReadyToSchedule", "第1問 もうすぐ始まるよ!!"],
    ["Scheduled", "まもなくスタート！"],
  ] as const)(
    "%sでは開始前の案内だけを表示し問題を早期開示しない",
    (state, heading) => {
      renderJoinedView(participantProjection(state));

      expect(screen.getByRole("heading", { name: heading })).toBeVisible();
      expect(screen.queryByText("2 + 2 は？")).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "A 3" })).not.toBeInTheDocument();
      expect(screen.queryByRole("tab", { name: "マイスコア" })).not.toBeInTheDocument();
      if (state === "Scheduled") {
        expect(screen.getByRole("timer")).toHaveAttribute("aria-live", "off");
      }
    },
  );

  it("Scheduledの投影でもopen_at以降は状態更新を待たず問題を表示する", () => {
    const projection = participantProjection("Scheduled");
    projection.current_round!.open_at = "2026-08-08T01:00:01Z";
    projection.current_round!.standard_close_at = "2026-08-08T01:00:07Z";

    renderJoinedView(projection);

    expect(screen.getByRole("heading", { name: question.prompt })).toBeVisible();
    expect(screen.getByRole("button", { name: "A 3" })).toBeEnabled();
    expect(screen.getByLabelText("経過時間1.0秒")).toBeVisible();
  });

  it.each([
    "ReadyToReveal",
    "AwaitingValidation",
    "Reveal",
  ] as const)("%sでは集計overlayの背面に問題と操作不能な選択肢を残す", (state) => {
    renderJoinedView(participantProjection(state));

    const collection = screen.getByRole("region", { name: "正解発表待ち" });
    expect(within(collection).getByRole("heading", { name: "正解発表を お待ちください" })).toBeVisible();
    const ticket = within(collection).getByText("あなたの参加者ID").parentElement;
    expect(ticket).toHaveTextContent("ABC");
    expect(ticket?.closest("[inert], [aria-hidden=true]")).toBeNull();
    expect(screen.getByTestId("quiz-stage-announcer")).toHaveTextContent("正解発表をお待ちください");
    expect(screen.queryByText(/補助回答|自動集計|回答を集計/)).not.toBeInTheDocument();
    expect(screen.getByTestId("quiz-stage-announcer")).toHaveAttribute(
      "aria-atomic",
      "true",
    );
    const prompt = screen.getByText(question.prompt);
    const backdrop = prompt.closest("[inert]");
    expect(backdrop).toHaveAttribute("aria-hidden", "true");
    expect(screen.queryByRole("heading", { name: question.prompt })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "A 3" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("button", { hidden: true })).toHaveLength(question.options.length);
    for (const answer of screen.getAllByRole("button", { hidden: true })) {
      expect(answer).toBeDisabled();
    }
    expect(screen.queryByRole("tab", { name: "マイスコア" })).not.toBeInTheDocument();
  });

  it.each(["Scheduled", "Open"] as const)("%sのまま締切に到達した場合も選択肢を無効化して集計overlayを表示する", (state) => {
    const projection = participantProjection(state);
    projection.current_round!.open_at = "2026-08-08T01:00:00Z";
    const { rerender } = render(joinedView(projection));
    expect(screen.getByRole("button", { name: "A 3" })).toBeEnabled();
    projection.current_round!.standard_close_at = new Date(now).toISOString();

    rerender(joinedView(projection));

    expect(screen.getByRole("region", { name: "正解発表待ち" })).toBeVisible();
    expect(screen.getByText(question.prompt).closest("[inert]"))
      .toHaveAttribute("aria-hidden", "true");
    expect(screen.queryByRole("button", { name: "A 3" })).not.toBeInTheDocument();
    for (const answer of screen.getAllByRole("button", { hidden: true })) {
      expect(answer).toBeDisabled();
    }
  });

  it.each(["ReadyToReveal", "AwaitingValidation", "Reveal"] as const)("%sでも保存済み確認文は背景Questionではなく集計cardへ表示する", (state) => {
    const submission: ParticipantSubmission = {
      clientElapsedMs: 2000, timingBasis: "client_monotonic_v1",
      clientAnswerId: "pending-answer", receivedAt: "2026-08-08T01:00:03Z",
      message: "回答は保存済みです。変更はできません。", optionId: ids.optionA,
      roundId: ids.round, roundVersion: 3, status: "accepted",
    };
    render(joinedView(participantProjection(state), submission));
    const notice = within(screen.getByRole("region", { name: "正解発表待ち" })).getByText(submission.message);
    expect(notice).toBeVisible();
    expect(notice.closest("[aria-hidden=true], [inert]")).toBeNull();
    expect(screen.queryByRole("button", { name: "A 3" })).not.toBeInTheDocument();
  });

  it("本人結果待ちから実結果へ切り替わったとき、問番号が同じでも結果DOMを再観測する", () => {
    const ready = publishedProjection({ status: "Unanswered" });
    const waiting = { ...ready, latest_result: { ...ready.latest_result!,
      result: { ...ready.latest_result!.result, outcome: null } } };
    const observe = vi.fn();
    window.addEventListener("quiz:observation", observe);
    try {
      const { rerender } = render(joinedView(waiting));
      expect(screen.getByRole("heading", { name: "結果待ち" })).toBeVisible();
      observe.mockClear();
      rerender(joinedView(ready));
      expect(screen.getByRole("heading", { name: "今回は未回答でした" })).toBeVisible();
      expect(observe).toHaveBeenCalledTimes(1);
      const event = observe.mock.calls[0]?.[0] as CustomEvent<BrowserObservation>;
      expect(event.detail).toMatchObject({ kind: "participant_display", phase: "result", result_dom_present: true });
    } finally {
      window.removeEventListener("quiz:observation", observe);
    }
  });

  it("背景Questionの受付は非可視、集計cardの保存確認文は可視として別々に計測する", () => {
    const rectangle = new DOMRect(0, 0, 100, 20);
    const rectangles = Object.assign([rectangle], { item: (index: number) => index === 0 ? rectangle : null });
    const rectSpy = vi.spyOn(HTMLElement.prototype, "getClientRects").mockReturnValue(rectangles);
    const observe = vi.fn();
    window.addEventListener("quiz:observation", observe);
    const submission: ParticipantSubmission = {
      clientElapsedMs: 2000, timingBasis: "client_monotonic_v1",
      clientAnswerId: "pending-answer", receivedAt: "2026-08-08T01:00:03Z",
      message: "回答は保存済みです。変更はできません。", optionId: ids.optionA,
      roundId: ids.round, roundVersion: 3, status: "accepted",
    };
    try {
      render(joinedView(participantProjection("AwaitingValidation"), submission));
      const event = observe.mock.calls.at(-1)?.[0] as CustomEvent<BrowserObservation>;
      expect(event.detail).toMatchObject({ kind: "participant_display", phase: "collecting",
        submission_status: "accepted", answer_dom_present: true, answer_dom_visible: false,
        answer_notice_visible: true });
    } finally {
      window.removeEventListener("quiz:observation", observe);
      rectSpy.mockRestore();
    }
  });

  it("回答中のbadge自体のaria-hiddenは装飾属性として扱い、見える受付を可視計測する", () => {
    const rectangle = new DOMRect(0, 0, 100, 20);
    const rectangles = Object.assign([rectangle], { item: (index: number) => index === 0 ? rectangle : null });
    const rectSpy = vi.spyOn(HTMLElement.prototype, "getClientRects").mockReturnValue(rectangles);
    const observe = vi.fn();
    window.addEventListener("quiz:observation", observe);
    const submission: ParticipantSubmission = {
      clientElapsedMs: 2000, timingBasis: "client_monotonic_v1",
      clientAnswerId: "pending-answer", receivedAt: "2026-08-08T01:00:01Z",
      message: "回答は保存済みです。変更はできません。", optionId: ids.optionA,
      roundId: ids.round, roundVersion: 3, status: "accepted",
    };
    try {
      const { container } = render(joinedView(participantProjection("Open"), submission));
      expect(container.querySelector(".answer-state-accepted")).toHaveAttribute("aria-hidden", "true");
      const event = observe.mock.calls.at(-1)?.[0] as CustomEvent<BrowserObservation>;
      expect(event.detail).toMatchObject({ kind: "participant_display", phase: "answering",
        answer_dom_present: true, answer_dom_visible: true, answer_notice_visible: true });
    } finally {
      window.removeEventListener("quiz:observation", observe);
      rectSpy.mockRestore();
    }
  });

  it("同期anchor由来の締切を固定で観測し、100ms表示tickでは再送しない", () => {
    const observe = vi.fn();
    window.addEventListener("quiz:observation", observe);
    const props = joinedView(participantProjection("Open")).props;
    const monotonicOffsetMs = 1000 - now;
    try {
      const view = render(<ParticipantJoinedView {...props}
        serverClock={{isSynchronized: true, now, monotonicOffsetMs}} />);
      const event = observe.mock.calls.at(-1)?.[0] as CustomEvent<BrowserObservation>;
      expect(event.detail).toMatchObject({kind: "participant_display", answer_deadline_monotonic_ms: 6000});
      const count = observe.mock.calls.length;
      for (let tick = 1; tick <= 20; tick++) view.rerender(<ParticipantJoinedView {...props}
        serverClock={{isSynchronized: true, now: now + tick * 100, monotonicOffsetMs}} />);
      expect(observe).toHaveBeenCalledTimes(count);
      view.rerender(<ParticipantJoinedView {...props} serverClock={{isSynchronized: false, now}} />);
      const unsynchronized = observe.mock.calls.at(-1)?.[0] as CustomEvent<BrowserObservation>;
      expect(unsynchronized.detail).not.toHaveProperty("answer_deadline_monotonic_ms");
    } finally { window.removeEventListener("quiz:observation", observe); }
  });

  it("集計overlay内で送信結果が不明な回答の保存状態を確認できる", async () => {
    const user = userEvent.setup();
    const submission: ParticipantSubmission = {
      clientElapsedMs: 2000, timingBasis: "client_monotonic_v1",
      clientAnswerId: "pending-answer",
      receivedAt: null,
      message: "保存状態を確認してください。",
      optionId: ids.optionA,
      roundId: ids.round,
      roundVersion: 3,
      status: "unknown",
    };
    const verify = vi.fn(async () => undefined);
    render(joinedView(participantProjection("AwaitingValidation"), submission, verify));

    const collection = screen.getByRole("region", { name: "正解発表待ち" });
    expect(within(collection).getByText(submission.message)).toBeVisible();
    const verifyButton = within(collection).getByRole("button", { name: "保存状態を確認" });
    expect(within(collection).getByText("あなたの参加者ID").parentElement).toHaveTextContent("ABC");
    expect(verifyButton.closest("[inert]")).toBeNull();
    await user.click(verifyButton);
    expect(verify).toHaveBeenCalledOnce();
    expect(verify).toHaveBeenCalledWith(submission);
  });

  it("集計中は接続バッジを表示せずエラー通知をoverlay内で伝える", () => {
    const projection = participantProjection("AwaitingValidation");
    projection.participant.is_ranking_eligible = false;
    const pageError = "通信に失敗しました。再接続しています。";
    render(
      <ParticipantJoinedView
        isStateFresh={true}
        onSendAnswer={vi.fn(async () => undefined)}
        onVerifySubmission={vi.fn(async () => undefined)}
        pageError={pageError}
        projection={projection}
        serverClock={{ isSynchronized: true, now, monotonicOffsetMs: performance.now() - now }}
        submission={null}
      />,
    );

    const collection = screen.getByRole("region", { name: "正解発表待ち" });
    expect(screen.queryByText(/接続確認中|接続OK|リアルタイム接続中|1秒ごとに更新中/))
      .not.toBeInTheDocument();
    expect(within(collection).getByRole("alert")).toHaveTextContent(pageError);
    for (const message of [
      pageError,
    ]) {
      expect(within(collection).getByText(message)).toBeVisible();
      expect(screen.getAllByText(message)).toHaveLength(1);
    }
    expect(screen.queryByText(/ランキング対象外/)).not.toBeInTheDocument();
  });

  it("current Roundも公開結果もないときは最初の問題を準備中と案内する", () => {
    renderJoinedView({
      ...participantProjection("PreparingAssets"),
      current_round: null,
    });

    expect(
      screen.getByRole("heading", { name: "エントリー完了！" }),
    ).toBeVisible();
  });

  it("表彰終了後は最終問の結果より既存の終了画面を優先する", () => {
    const projection = publishedProjection({ status: "Unanswered" });
    projection.event = {
      ...projection.event,
      event_state: "Completed",
    };
    projection.award_ceremony = { phase: "Finished", top_rows: null };

    renderJoinedView(projection);

    expect(
      screen.getByRole("heading", { name: "最終結果" }),
    ).toBeVisible();
    expect(screen.getByRole("main")).toHaveClass("participant-live-page");
    expect(screen.getByText("ご参加ありがとうございました")).toBeVisible();
    expect(
      screen.queryByText("アンケート・ホームリンクは準備中です。"),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "マイスコア" })).not.toBeInTheDocument();
  });

  it("開催終了後は最終結果発表を待ち、運営発表で本人結果未取得でも5位の本人を呼び出す", () => {
    const projection = publishedProjection({ status: "Unanswered" });
    projection.event = { ...projection.event, event_state: "Completed" };
    projection.award_ceremony = { phase: "Waiting", top_rows: null };
    const { rerender } = renderJoinedView(projection);
    expect(screen.getByRole("heading", { name: "最終結果発表!!" })).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("最終結果発表!!");
    expect(screen.queryByText("ご参加ありがとうございました")).not.toBeInTheDocument();
    expect(screen.queryByRole("tab")).not.toBeInTheDocument();

    rerender(joinedView({ ...projection, award_ceremony: { phase: "Announced", top_rows: [
      { public_label: "ABC", overall_rank: 5 },
    ] } }));
    expect(screen.getByRole("heading", { name: /あなたは.*5.*位!!/u })).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("あなたは5位!! ステージ上にお越しください!!");
    expect(screen.queryByRole("link")).not.toBeInTheDocument();

    rerender(joinedView({ ...projection, award_ceremony: { phase: "Finished", top_rows: null } }));
    expect(screen.getByText("ご参加ありがとうございました")).toBeVisible();
    expect(screen.queryByText(/ステージ上に/)).not.toBeInTheDocument();
  });

  it.each([
    ["Scheduled", false, "しばらく お待ちください"],
    ["Open", false, "しばらく お待ちください"],
    ["VoidCandidate", true, "しばらく お待ちください"],
  ] as const)("%sのfallback案内を表示する", (state, isSynchronized, heading) => {
    const projection = participantProjection("Scheduled");
    projection.current_round!.open_at = "2026-08-08T01:00:01Z";
    render(
      <ParticipantJoinedView
        isStateFresh={true}
        onSendAnswer={vi.fn(async () => undefined)}
        onVerifySubmission={vi.fn(async () => undefined)}
        pageError={null}
        projection={{
          ...projection,
          current_round: { ...projection.current_round!, state },
        }}
        serverClock={{ isSynchronized, now, monotonicOffsetMs: performance.now() - now }}
        submission={null}
      />,
    );

    expect(screen.getByRole("heading", { name: heading })).toBeVisible();
    expect(screen.queryByRole("button", { name: "A 3" })).not.toBeInTheDocument();
    expect(screen.queryByText(question.prompt)).not.toBeInTheDocument();
  });

  it("公開結果をマイスコア・みんなの回答・ランキングのタブで表示する", async () => {
    const user = userEvent.setup();
    const projection: ParticipantState = {
      ...publishedProjection({ status: "Unanswered" }),
      ranking: {
        correct_count: 2,
        overall_rank: 3,
        snapshot_version: 4,
        top_rows: null,
        total_points: 1200,
      },
    };
    renderJoinedView(projection);

    expect(screen.getByRole("region", { name: "マイスコア・未回答" })).toBeVisible();
    await user.click(screen.getByRole("tab", { name: "みんなの回答" }));
    expect(screen.getByRole("heading", { name: "みんなの回答" })).toBeVisible();
    await user.click(screen.getByRole("tab", { name: "ランキング" }));
    expect(screen.getByText("位").parentElement).toHaveTextContent("3位");
    expect(screen.getByText("あなたの総合順位")).toBeVisible();
    expect(screen.getByText("ID ABC · 1,200 pt")).toBeVisible();
  });

  it("次Roundの準備中は直前結果を残し、準備完了で開始待機へ切り替わる", () => {
    const projection = publishedProjection({ status: "Unanswered" });
    projection.current_round = {
      ...participantProjection("PreparingAssets").current_round!,
      round_id: ids.nextRound,
      round_number: 3,
      version: 74,
    };

    const { rerender } = renderJoinedView(projection);

    expect(screen.getByRole("region", { name: "マイスコア・未回答" })).toBeVisible();
    expect(
      screen.queryByRole("heading", { name: "次の問題を準備しています" }),
    ).not.toBeInTheDocument();

    rerender(joinedView({
      ...projection,
      current_round: { ...projection.current_round, state: "ReadyToSchedule" },
    }));

    expect(screen.getByRole("heading", { name: "第2問 もうすぐ始まるよ!!" })).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("第2問もうすぐ始まるよ!!");
    expect(screen.queryByRole("region", { name: "マイスコア・未回答" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab")).not.toBeInTheDocument();
    expect(screen.queryByRole("timer")).not.toBeInTheDocument();
    expect(screen.queryByText(question.prompt)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "A 3" })).not.toBeInTheDocument();
    expect(screen.queryByText(/第(?:3|74)問/)).not.toBeInTheDocument();
  });

  it.each(["ReadyToSchedule", "Scheduled", "Open"] as const)(
    "次Roundが%sになると直前結果より現在の進行を優先する",
    (state) => {
      const projection = publishedProjection({ status: "Unanswered" });
      projection.current_round = {
        ...participantProjection(state).current_round!,
        round_id: ids.nextRound,
        round_number: 3,
        version: 4,
      };

      renderJoinedView(projection);

      expect(screen.queryByRole("region", { name: "マイスコア・未回答" })).not.toBeInTheDocument();
      if (state === "ReadyToSchedule") {
        expect(screen.getByRole("heading", { name: "第2問 もうすぐ始まるよ!!" })).toBeVisible();
        expect(screen.queryByRole("timer")).not.toBeInTheDocument();
      } else if (state === "Scheduled") {
        expect(screen.getByRole("timer")).toBeVisible();
      } else {
        expect(screen.getByRole("heading", { name: "2 + 2 は？" })).toBeVisible();
      }
    },
  );

  it("本人の保存済み回答だけを回答分布へ表示する", async () => {
    const user = userEvent.setup();
    const projection = publishedProjection({
      timing_basis: "server_received_v1",
      is_correct: false,
      scoring_policy_version: "v1",
      speed_cohort: "AllValid",
      speed_cohort_size: 5,
      speed_rank: 2,
      status: "Scored",
      total_points: 0,
      base_points: 0, speed_points: 0, elapsed_ms: 2500,
    });
    projection.latest_result!.selected_option_id = ids.optionA;
    renderJoinedView(projection);

    await user.click(screen.getByRole("tab", { name: "みんなの回答" }));
    expect(
      within(screen.getByRole("tabpanel")).getByText("あなたの回答"),
    ).toBeVisible();
  });

  it("Publishedでは未保存のlocal選択を本人回答として表示しない", async () => {
    const user = userEvent.setup();
    renderJoinedView(publishedProjection({ status: "Unanswered" }), {
      clientElapsedMs: 2000, timingBasis: "client_monotonic_v1",
      clientAnswerId: "local-answer",
      receivedAt: null,
      message: "回答は保存されていません。",
      optionId: ids.optionA,
      roundId: ids.round,
      roundVersion: 3,
      status: "not-stored",
    });

    await user.click(screen.getByRole("tab", { name: "みんなの回答" }));
    expect(within(screen.getByRole("tabpanel")).queryByText("あなたの回答")).not.toBeInTheDocument();
  });

  it("回答画面は問題文・タイマー・選択肢の順にテキストで表示する", () => {
    renderJoinedView(participantProjection("Open"));

    const answer = screen.getByRole("group", { name: question.prompt });
    const prompt = screen.getByRole("heading", { name: question.prompt });
    const timer = screen.getByLabelText("経過時間2.0秒");
    expect(prompt).toBeVisible();
    expect(prompt.compareDocumentPosition(timer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(timer.compareDocumentPosition(answer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(answer).queryByRole("img")).not.toBeInTheDocument();
  });
});

describe("参加者の独立した結果表示", () => {
  it("APIの開始待機秒を案内に使う", () => {
    const projection = participantProjection("ReadyToSchedule");
    projection.event.start_delay_seconds = 3;
    renderJoinedView(projection);
    expect(screen.getByText("運営が開始すると、3秒のカウントダウンが始まります。")).toBeInTheDocument();
    expect(screen.queryByText(/5秒のカウントダウン/)).not.toBeInTheDocument();
  });

  it("分布を隠しても本人順位を表示し、再公開時に分布タブが戻る", () => {
    const visible = publishedProjection({ status: "Unanswered" });
    if (visible.latest_result === null) {
      throw new Error("published fixture requires latest_result");
    }
    const hidden: ParticipantState = {
      ...visible,
      latest_result: {
        ...visible.latest_result,
        result: { ...visible.latest_result.result, distribution: null },
      },
      ranking: { snapshot_version: 1, top_rows: null, overall_rank: 2, total_points: 500, correct_count: 1 },
    };
    const { rerender } = render(joinedView(hidden));
    expect(screen.queryByRole("tab", { name: "みんなの回答" })).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "ランキング" })).toBeVisible();
    expect(screen.queryByText(/正答率|有効回答|番目の速さ/)).not.toBeInTheDocument();

    rerender(joinedView(visible));
    expect(screen.getByRole("tab", { name: "みんなの回答" })).toBeVisible();
    expect(screen.queryByRole("tab", { name: "ランキング" })).not.toBeInTheDocument();
  });
});
