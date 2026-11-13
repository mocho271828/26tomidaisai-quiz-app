import { afterEach, describe, expect, it, vi } from "vitest";
import { getModeratorParticipantPreview } from "./moderatorClient";

const data = {
    award_ceremony: null,
  projected_at: "2026-10-22T03:00:00Z",
  event: { event_id: "78000000-0000-4000-8000-000000000001", event_title: "富大祭", event_state: "LobbyOpen",
    event_version: 1, start_delay_seconds: 3, timing_basis: "client_monotonic_v1", arrival_grace_ms: 2000,
    clock_tolerance_ms: 500, client_ahead_tolerance_ms: 2500 },
  current_round: null, latest_result: null, latest_ranking_snapshot_version: null,
  result_visibility: { distribution: false, personal_ranking: false, overall_ranking: false },
  personal_result_delivery: { status: "none", revision: null },
};
afterEach(() => vi.unstubAllGlobals());

describe("司会者の参加者プレビューAPI", () => {
  it("既存運営Cookie経路でGETし、取消signalと共通Zod契約を使う", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(data)));
    vi.stubGlobal("fetch", fetch);
    const controller = new AbortController();
    await expect(getModeratorParticipantPreview(controller.signal)).resolves.toEqual(data);
    expect(fetch).toHaveBeenCalledWith("/api/v1/operator/participant-preview", expect.objectContaining({
      credentials: "same-origin", signal: controller.signal,
    }));
  });
  it.each(["participant", "answer", "token"])("個人/認証情報%sが混入した応答を拒否する", async (field) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ ...data, [field]: {} }))));
    await expect(getModeratorParticipantPreview(new AbortController().signal)).rejects.toThrow();
  });
});
