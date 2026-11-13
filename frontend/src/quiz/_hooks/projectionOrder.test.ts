import { describe, expect, it } from "vitest";

import {
  chooseAudienceProjection,
  chooseOperatorProjection,
  type AudienceProjectionVersion,
  type OperatorProjectionVersion,
} from "./projectionOrder";

const audienceState = (
  overrides: Partial<AudienceProjectionVersion> = {},
): AudienceProjectionVersion => ({
  projected_at: "2026-08-08T01:00:00Z",
  event: {
    event_id: "60000000-0000-4000-8000-000000000001",
    event_version: 2,
  },
  current_round: {
    round_id: "60000000-0000-4000-8000-000000000002",
    version: 3,
    state_version: 12,
  },
  latest_result: null,
  ...overrides,
});

describe("projection order", () => {
  it("表示番号が同じでも低いround versionのprojectionは受理しない", () => {
    const base = audienceState();
    const current = {
      ...base,
      current_round: { ...base.current_round!, version: 74, round_number: 4 },
    };
    const stale = {
      ...current,
      projected_at: "2026-08-08T01:00:01Z",
      current_round: { ...current.current_round, version: 73, state_version: 99 },
    };
    const next = {
      ...current,
      current_round: { ...current.current_round, version: 75, state_version: 1 },
    };

    expect(chooseAudienceProjection(current, stale)).toBe(current);
    expect(chooseAudienceProjection(current, next)).toBe(next);
  });

  it("遅れて届いた低versionで表示を巻き戻さない", () => {
    const current = audienceState();
    const stale = audienceState({
      projected_at: "2026-08-08T01:00:01Z",
      event: { ...current.event, event_version: 1 },
    });

    expect(chooseAudienceProjection(current, stale)).toBe(current);
  });

  it("新Roundはevent version上昇時だけ受理する", () => {
    const current = audienceState();
    const sameEventVersion = audienceState({
      current_round: {
        round_id: "60000000-0000-4000-8000-000000000099",
        version: 4,
        state_version: 16,
      },
    });
    const nextEventVersion = audienceState({
      event: { ...current.event, event_version: 3 },
      current_round: sameEventVersion.current_round,
    });

    expect(chooseAudienceProjection(current, sameEventVersion)).toBe(current);
    expect(chooseAudienceProjection(current, nextEventVersion)).toBe(
      nextEventVersion,
    );
  });

  it("同じRoundの自動公開でcurrentからlatestへ移っても更新を受理する", () => {
    const current = audienceState();
    if (current.current_round === null) {
      throw new Error("test fixture requires a current round");
    }
    const published = audienceState({
      projected_at: "2026-08-08T01:00:01Z",
      current_round: null,
      latest_result: {
        ...current.current_round,
        state_version: 13,
      },
    });

    expect(chooseAudienceProjection(current, published)).toBe(published);
  });

  it("event切替後に戻った旧event responseを拒否する", () => {
    const current: OperatorProjectionVersion = {
      event_id: "60000000-0000-4000-8000-000000000010",
      event_version: 1,
      current_round: null,
      projected_at: "2026-08-08T01:00:01Z",
    };
    const oldResponse: OperatorProjectionVersion = {
      ...current,
      event_id: "60000000-0000-4000-8000-000000000001",
      event_version: 99,
    };

    expect(chooseOperatorProjection(current, oldResponse)).toBe(current);
  });
});
