import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getParticipantResult, getParticipantSession, joinParticipant } from "./audienceClient";
import { ApiCancellation, cancellationReason } from "./cancellation";

const apiClient = vi.hoisted(() => ({
  requestJson: vi.fn(),
}));

vi.mock("./client", () => apiClient);

describe("audience API client", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiClient.requestJson.mockResolvedValue(undefined);
  });

  afterEach(() => { vi.useRealTimers(); });

  it.each([getParticipantSession, getParticipantResult])("参加者GETを5秒で打ち切り、fetchをabortする", async (get) => {
    vi.useFakeTimers();
    apiClient.requestJson.mockReturnValue(new Promise(() => undefined));
    const request = get();
    const rejected = expect(request).rejects.toMatchObject({ name: "TimeoutError" });
    const signal = apiClient.requestJson.mock.calls[0]?.[2].signal as AbortSignal;
    await vi.advanceTimersByTimeAsync(5_000);
    await rejected;
    expect(signal.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("本人結果GETの親取消を即時反映しtimeoutを回収する", async () => {
    vi.useFakeTimers();
    apiClient.requestJson.mockReturnValue(new Promise(() => undefined));
    const controller = new AbortController();
    const request = getParticipantResult(controller.signal);
    const rejected = expect(request).rejects.toMatchObject({ name: "AbortError" });
    controller.abort();
    await rejected;
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each(["superseded", "hidden", "unmount", "deadline"] as const)("親の%sを有界GETへ引き継ぐ", async (reason) => {
    vi.useFakeTimers();
    apiClient.requestJson.mockReturnValue(new Promise(() => undefined));
    const controller = new AbortController();
    const request = getParticipantResult(controller.signal);
    const rejected = expect(request).rejects.toMatchObject({ name: reason === "deadline" ? "TimeoutError" : "AbortError" });
    const childSignal = apiClient.requestJson.mock.calls[0]?.[2].signal as AbortSignal;
    controller.abort(new ApiCancellation(reason));
    await rejected;
    expect(cancellationReason(childSignal)).toBe(reason);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("参加を再送識別用tokenだけで固定endpointへ送る", async () => {
    const clientJoinToken = "A".repeat(43);

    await joinParticipant(clientJoinToken);

    expect(apiClient.requestJson).toHaveBeenCalledWith(
      "/participant/join",
      expect.anything(),
      {
        body: { client_join_token: clientJoinToken },
        method: "POST",
        observationOperation: "participant_join",
      },
    );
  });
});
