type VersionedRound = {
  round_id: string;
  version: number;
  state_version: number;
};

export type AudienceProjectionVersion = {
  projected_at: string;
  event: { event_id: string; event_version: number };
  current_round: VersionedRound | null;
  latest_result: VersionedRound | null;
};

export type OperatorProjectionVersion = {
  projected_at: string;
  event_id: string;
  event_version: number;
  current_round: VersionedRound | null;
};

type ProjectionOrder = readonly [
  eventVersion: number,
  roundVersion: number,
  roundStateVersion: number,
  projectedAt: number,
];

function compareOrder(candidate: ProjectionOrder, current: ProjectionOrder): number {
  for (let index = 0; index < candidate.length; index += 1) {
    const candidatePart = candidate[index];
    const currentPart = current[index];
    if (candidatePart === undefined || currentPart === undefined) {
      continue;
    }
    if (candidatePart !== currentPart) {
      return candidatePart - currentPart;
    }
  }
  return 0;
}

function audienceOrder(value: AudienceProjectionVersion): ProjectionOrder {
  const newestRound = value.current_round ?? value.latest_result;
  return [
    value.event.event_version,
    newestRound?.version ?? 0,
    newestRound?.state_version ?? 0,
    Date.parse(value.projected_at),
  ];
}

function operatorOrder(value: OperatorProjectionVersion): ProjectionOrder {
  return [
    value.event_version,
    value.current_round?.version ?? 0,
    value.current_round?.state_version ?? 0,
    Date.parse(value.projected_at),
  ];
}

export function chooseAudienceProjection<T extends AudienceProjectionVersion>(
  current: T | null,
  candidate: T,
): T {
  if (current === null) {
    return candidate;
  }
  if (candidate.event.event_id !== current.event.event_id) {
    return current;
  }
  if (candidate.event.event_version !== current.event.event_version) {
    return candidate.event.event_version > current.event.event_version
      ? candidate
      : current;
  }
  const currentNewestRound = current.current_round ?? current.latest_result;
  const candidateNewestRound = candidate.current_round ?? candidate.latest_result;
  if (candidateNewestRound?.round_id !== currentNewestRound?.round_id) {
    return current;
  }
  return compareOrder(audienceOrder(candidate), audienceOrder(current)) >= 0
    ? candidate
    : current;
}

export function chooseOperatorProjection<T extends OperatorProjectionVersion>(
  current: T | null,
  candidate: T,
): T {
  if (current === null) {
    return candidate;
  }
  if (candidate.event_id !== current.event_id) {
    return current;
  }
  if (candidate.event_version !== current.event_version) {
    return candidate.event_version > current.event_version ? candidate : current;
  }
  if (candidate.current_round?.round_id !== current.current_round?.round_id) {
    return current;
  }
  return compareOrder(operatorOrder(candidate), operatorOrder(current)) >= 0
    ? candidate
    : current;
}
