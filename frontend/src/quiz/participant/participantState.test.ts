import { describe, expect, it } from "vitest";

import type { ParticipantState, PersonalResultResponse } from "../../api/contracts";
import type { ParticipantCommonState } from "../../api/realtimeContracts";
import {
  applyParticipantCommonState, applyParticipantPersonalState, applyParticipantResult,
  applyPersonalResultDelivery, personalResultRevision, personalStateKey,
} from "./participantState";

const id = (suffix: string): string => `71000000-0000-4000-8000-0000000000${suffix}`;
const bootstrap: ParticipantState = {
    award_ceremony: null,
  projected_at: "2026-10-01T00:00:02Z",
  event: {
  timing_basis: "client_monotonic_v1", arrival_grace_ms: 2000, clock_tolerance_ms: 500, client_ahead_tolerance_ms: 2500,  event_id: id("01"), event_state: "InProgress", event_title: "Quiz", event_version: 3, start_delay_seconds: 3 },
  participant: { public_label: "ABC", is_ranking_eligible: true },
  current_round: {
    round_id: id("02"), round_number: 2, version: 3, state_version: 13, state: "Open",
    open_at: "2026-10-01T00:00:00Z", standard_close_at: "2026-10-01T00:00:10Z",
    question: { question_id: id("03"), prompt: "問題", options: [{ option_id: id("04"), label: "A", content: "選択肢" }] },
  },
  latest_result: null,
  result_visibility: { distribution: true, personal_ranking: true, overall_ranking: true },
  latest_ranking_snapshot_version: null,
  personal_result_delivery: { status: "none", revision: null },
};
function answered(): ParticipantState {
  return { ...bootstrap, answer: {client_elapsed_ms: 2000, timing_basis: "client_monotonic_v1",  answer_id: id("05"), round_id: id("02"), option_id: id("04"), client_answer_id: id("06"), received_at: "2026-10-01T00:00:01Z" } };
}
function published(): ParticipantCommonState {
  const common: ParticipantCommonState = {
    ...bootstrap, event: { ...bootstrap.event, event_version: 4 }, current_round: null,
    latest_result: {
      ...bootstrap.current_round!, state: "Reveal", version: 4, state_version: 16,
      question: bootstrap.current_round!.question!,
      result: { publication: "Published", correct_option_id: id("04"), distribution: { valid_answer_count: 1, options: [] } },
    },
    latest_ranking_snapshot_version: 2,
  };
  return { ...common, personal_result_delivery: { status: "pending", revision: personalResultRevision(common) } };
}
function result(common = published()): PersonalResultResponse {
  return {
    delivery: { status: "ready", revision: personalResultRevision(common) },
    result: {
      selected_option_id: id("04"),
      outcome: {
      timing_basis: "server_received_v1",  status: "Scored", is_correct: true, total_points: 450, base_points: 400, speed_points: 50, elapsed_ms: 5000, scoring_policy_version: "v1", speed_cohort: "Correct", speed_rank: 1, speed_cohort_size: 1 },
      ranking: { snapshot_version: 2, overall_rank: 1, total_points: 450, correct_count: 1, top_rows: [{ public_label: "ABC", overall_rank: 1, total_points: 450, correct_count: 1 }] },
    },
  };
}
const initial = () => applyParticipantPersonalState(null, bootstrap, null)!;
const waiting = () => applyParticipantCommonState(initial(), published())!;
const loaded = () => applyParticipantResult(waiting(), result())!;

describe("participant bootstrap, common state and personal results", () => {
  it("共通更新に本人回答がなくても同じRoundの保存済み回答を保持する", () => {
    const state = applyParticipantPersonalState(null, answered(), null)!;
    const next = applyParticipantCommonState(state, bootstrap);
    expect(next?.answer).toEqual(state.answer);
  });

  it("bootstrapが時計phaseより古くても同versionの回答を反映する", () => {
    const common = { ...bootstrap, current_round: { ...bootstrap.current_round!, state: "ReadyToReveal" as const, state_version: 14 } };
    const advanced = applyParticipantCommonState(initial(), common)!;
    const next = applyParticipantPersonalState(advanced, answered(), common);
    expect(next?.current_round?.state).toBe("ReadyToReveal");
    expect(next?.answer).toEqual(answered().answer);
  });

  it("共通結果だけでは未回答や0点を作らず、同じ版の本人結果でだけ表示する", () => {
    expect(waiting().latest_result?.result.outcome).toBeNull();
    expect(waiting().latest_result?.selected_option_id).toBeNull();
    expect(waiting().ranking).toBeUndefined();
    expect(loaded().latest_result?.result.outcome).toEqual(result().result?.outcome);
    expect(loaded().ranking).toEqual(result().result?.ranking);
  });

  it("公開設定を即時マスクし、古い本人HTTPとWSで非公開項目を復活させない", () => {
    const common = published();
    common.event = { ...common.event, event_version: 5 };
    common.result_visibility = { distribution: false, personal_ranking: false, overall_ranking: false };
    const hidden = applyParticipantCommonState(loaded(), common)!;
    expect(hidden.latest_result?.result.distribution).toBeNull();
    expect(hidden.latest_result?.result.outcome).toMatchObject({ total_points: 450, speed_rank: null, speed_cohort_size: null });
    expect(hidden.ranking).toBeUndefined();
    expect(applyParticipantResult(hidden, result())).toBeNull();
    expect(applyParticipantPersonalState(hidden, { ...bootstrap, ...published() }, common)).toBeNull();
  });

  it("全体順位非公開では本人順位を保持してTOP3だけ消す", () => {
    const common = published();
    common.result_visibility = { ...common.result_visibility, overall_ranking: false };
    const hidden = applyParticipantCommonState(loaded(), common)!;
    expect(hidden.ranking?.overall_rank).toBe(1);
    expect(hidden.ranking?.top_rows).toBeNull();
  });

  it("次Round準備中も同じ版の直前結果を保持し、別結果版へは持ち越さない", () => {
    const common = published();
    common.event = { ...common.event, event_version: 5 };
    common.current_round = { ...bootstrap.current_round!, round_id: id("07"), version: 5, state_version: 20, state: "PreparingAssets" };
    const next = applyParticipantCommonState(loaded(), common)!;
    expect(next.latest_result?.result.outcome).toEqual(result().result?.outcome);
    common.latest_result = { ...common.latest_result!, version: 5 };
    expect(applyParticipantCommonState(next, common)?.latest_result?.result.outcome).toBeNull();
  });

  it("bootstrap再取得で取得済みの結果を消さず、ready通知だけで取得済みにしない", () => {
    const common = published();
    const next = applyParticipantPersonalState(loaded(), { ...bootstrap, ...common }, common)!;
    expect(next.latest_result?.result.outcome).toEqual(result().result?.outcome);
    const ready = applyPersonalResultDelivery(waiting(), result().delivery)!;
    expect(ready.received_result_revision).toBeUndefined();
    expect(ready.latest_result?.result.outcome).toBeNull();
  });

  it("共通同士の順序と開催/結果版/順位版の一致を確認する", () => {
    const common = { ...bootstrap, projected_at: "2026-10-01T00:00:01Z" };
    expect(applyParticipantCommonState(initial(), common)).not.toBeNull();
    expect(applyParticipantCommonState(initial(), common, { ...common, projected_at: "2026-10-01T00:00:03Z" })).toBeNull();
    const wrong = result();
    wrong.delivery.revision = { ...wrong.delivery.revision!, ranking_snapshot_version: 3 };
    expect(applyParticipantResult(waiting(), wrong)).toBeNull();
    expect(applyPersonalResultDelivery(waiting(), wrong.delivery)).toBeNull();
    const timed = { ...bootstrap, current_round: { ...bootstrap.current_round!, state_version: 15 } };
    expect(personalStateKey(timed)).toBe(personalStateKey(bootstrap));
  });

  it("本人結果や通常順位が非公開でも共通表彰を同期し、古い復帰状態で巻き戻さない", () => {
    const announced: ParticipantCommonState = {
      ...published(), event: { ...bootstrap.event, event_state: "Completed", event_version: 6 },
      result_visibility: { distribution: false, personal_ranking: false, overall_ranking: false },
      award_ceremony: { phase: "Announced", top_rows: [{ public_label: "ABC", overall_rank: 5 }] },
    };
    const next = applyParticipantCommonState(loaded(), announced)!;
    expect(next.award_ceremony).toEqual(announced.award_ceremony);
    expect(next.ranking).toBeUndefined();
    expect(applyParticipantPersonalState(next, bootstrap, null)).toBeNull();
    expect(applyParticipantResult(next, result())).toBeNull();
    const recovered = applyParticipantPersonalState(null, { ...bootstrap, ...announced }, null)!;
    expect(recovered.award_ceremony).toEqual(announced.award_ceremony);
    const finished: ParticipantCommonState = { ...announced, event: { ...announced.event, event_version: 7 }, award_ceremony: { phase: "Finished", top_rows: null } };
    const final = applyParticipantCommonState(next, finished)!;
    expect(final.award_ceremony).toEqual({ phase: "Finished", top_rows: null });
    expect(applyParticipantCommonState(final, announced)).toBeNull();
  });
});
