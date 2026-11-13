import type {
  ParticipantLatestResult, ParticipantRanking, ParticipantState,
  PersonalResultDelivery, PersonalResultResponse, PersonalResultRevision,
} from "../../api/contracts";
import type { ParticipantCommonState } from "../../api/realtimeContracts";
import { chooseAudienceProjection, type AudienceProjectionVersion } from "../_hooks/projectionOrder";

// 本人結果未取得は、DBが確認したUnansweredと区別する。
type ParticipantViewResult = Omit<ParticipantLatestResult, "result"> & {
  result: Omit<ParticipantLatestResult["result"], "outcome"> & {
    outcome: ParticipantLatestResult["result"]["outcome"] | null;
  };
};

export type ParticipantViewState = Omit<ParticipantState, "latest_result"> & {
  latest_result: ParticipantViewResult | null;
  ranking?: ParticipantRanking;
  received_result_revision?: PersonalResultRevision;
};

export function personalResultRevision(common: ParticipantCommonState): PersonalResultRevision {
  return {
    event_id: common.event.event_id,
    event_version: common.event.event_version,
    result_round_id: common.latest_result?.round_id ?? null,
    result_round_version: common.latest_result?.version ?? null,
    ranking_snapshot_version: common.latest_ranking_snapshot_version,
    result_visibility: common.result_visibility,
  };
}

export function resultRevisionKey(revision: PersonalResultRevision): string {
  return [revision.event_id, revision.event_version, revision.result_round_id,
    revision.result_round_version, revision.ranking_snapshot_version,
    revision.result_visibility.distribution, revision.result_visibility.personal_ranking,
    revision.result_visibility.overall_ranking].join(":");
}

export function personalStateKey(common: ParticipantCommonState): string {
  return resultRevisionKey(personalResultRevision(common));
}

export function applyParticipantCommonState(
  current: ParticipantViewState,
  common: ParticipantCommonState,
  previousCommon: ParticipantCommonState | null = null,
): ParticipantViewState | null {
  if (previousCommon !== null && chooseAudienceProjection(previousCommon, common) !== common) return null;
  // 本人GETの取得時刻と共通cacheの構築時刻は別系列として比較する。
  const versionCandidate = { ...common, projected_at: current.projected_at };
  if (chooseAudienceProjection<AudienceProjectionVersion>(current, versionCandidate) !== versionCandidate) return null;
  const previousResult = current.latest_result?.round_id === common.latest_result?.round_id &&
    current.latest_result?.version === common.latest_result?.version ? current.latest_result : null;
  const outcome = previousResult?.result.outcome ?? null;
  const ranking = common.result_visibility.personal_ranking &&
    current.ranking?.snapshot_version === common.latest_ranking_snapshot_version
    ? { ...current.ranking, top_rows: common.result_visibility.overall_ranking ? current.ranking.top_rows : null }
    : undefined;
  return {
    ...common,
    projected_at: Date.parse(current.projected_at) > Date.parse(common.projected_at)
      ? current.projected_at : common.projected_at,
    participant: current.participant,
    latest_result: common.latest_result === null ? null : {
      ...common.latest_result,
      selected_option_id: previousResult?.selected_option_id ?? null,
      result: {
        ...common.latest_result.result,
        distribution: common.result_visibility.distribution ? common.latest_result.result.distribution : null,
        outcome: outcome?.status === "Scored" && !common.result_visibility.personal_ranking
          ? { ...outcome, speed_rank: null, speed_cohort_size: null } : outcome,
      },
    },
    answer: current.answer?.round_id === common.current_round?.round_id ? current.answer : undefined,
    ranking,
    received_result_revision: current.received_result_revision,
  };
}

export function applyParticipantPersonalState(
  current: ParticipantViewState | null,
  bootstrap: ParticipantState,
  common: ParticipantCommonState | null,
): ParticipantViewState | null {
  const initial: ParticipantViewState = {
    ...bootstrap,
    latest_result: bootstrap.latest_result === null ? null : {
      ...bootstrap.latest_result,
      selected_option_id: null,
      result: { ...bootstrap.latest_result.result, outcome: null },
    },
  };
  if (current === null) return initial;
  if (bootstrap.event.event_id !== current.event.event_id ||
    bootstrap.event.event_version < current.event.event_version) return null;
  const selected = chooseAudienceProjection<ParticipantViewState>(current, initial);
  const canMerge = bootstrap.event.event_version === current.event.event_version &&
    bootstrap.current_round?.round_id === current.current_round?.round_id;
  if (selected === current && !canMerge) return null;
  const source = selected === initial ? bootstrap : { ...bootstrap, current_round: selected.current_round };
  const preserved = applyParticipantCommonState(current, source) ?? selected;
  const next = { ...preserved, participant: bootstrap.participant,
    answer: bootstrap.answer ?? preserved.answer };
  if (common === null || common.event.event_version !== next.event.event_version) return next;
  return applyParticipantCommonState(next, { ...common, current_round: next.current_round }) ?? next;
}

export function applyPersonalResultDelivery(
  current: ParticipantViewState,
  delivery: PersonalResultDelivery,
): ParticipantViewState | null {
  if (delivery.revision !== null && resultRevisionKey(delivery.revision) !== personalStateKey(current)) return null;
  if (delivery.status === "none" && current.latest_result !== null) return null;
  return { ...current, personal_result_delivery: delivery };
}

export function applyParticipantResult(
  current: ParticipantViewState,
  response: PersonalResultResponse,
): ParticipantViewState | null {
  const { revision, status } = response.delivery;
  if (status !== "ready" || revision === null || response.result === null ||
    resultRevisionKey(revision) !== personalStateKey(current)) return null;
  const result = response.result;
  const next: ParticipantViewState = {
    ...current,
    personal_result_delivery: response.delivery,
    received_result_revision: revision,
    ranking: current.result_visibility.personal_ranking ? result.ranking ?? undefined : undefined,
    latest_result: current.latest_result === null ? null : {
      ...current.latest_result,
      selected_option_id: result.selected_option_id,
      result: { ...current.latest_result.result, outcome: result.outcome },
    },
  };
  return applyParticipantCommonState(next, { ...current, personal_result_delivery: response.delivery });
}
