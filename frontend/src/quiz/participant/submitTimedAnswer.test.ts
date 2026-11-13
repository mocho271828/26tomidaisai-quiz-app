import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiContractError, ApiResponseError, ApiTransportError } from "../../api/client";
import { submitAnswer } from "../../api/audienceClient";
import { submitTimedAnswer } from "./submitTimedAnswer";

vi.mock("../../api/audienceClient", () => ({ submitAnswer: vi.fn() }));
const pending = { round_id: "60000000-0000-4000-8000-000000000001", round_version: 3,
  option_id: "60000000-0000-4000-8000-000000000002", client_answer_id: "60000000-0000-4000-8000-000000000003", client_elapsed_ms: 2089 };
const receipt = { ...pending, answer_id: pending.client_answer_id, received_at: "2026-10-15T00:00:03Z",
  timing_basis: "client_monotonic_v1" as const, is_replay: true };

describe("回答の有限POST再送", () => {
  afterEach(() => { vi.useRealTimers(); vi.resetAllMocks(); });
  it("通信例外時はID・版・選択・整数msを変えず最大1回だけ再送する", async () => {
    vi.mocked(submitAnswer).mockRejectedValue(new ApiTransportError(new TypeError("offline")));
    await expect(submitTimedAnswer(pending, 3, () => true)).rejects.toBeInstanceOf(ApiTransportError);
    expect(submitAnswer).toHaveBeenCalledTimes(2);
    expect(vi.mocked(submitAnswer).mock.calls[0]?.[1]).toEqual(vi.mocked(submitAnswer).mock.calls[1]?.[1]);
    expect(vi.mocked(submitAnswer).mock.calls[0]?.[1].client_elapsed_ms).toBe(2089);
  });
  it("1秒で初回応答待ちを打切り、保存済みのexact replayを採用する", async () => {
    vi.useFakeTimers();
    vi.mocked(submitAnswer).mockReturnValueOnce(new Promise(() => undefined)).mockResolvedValueOnce(receipt);
    const response = submitTimedAnswer(pending, 3, () => true);
    await vi.advanceTimersByTimeAsync(999);
    expect(submitAnswer).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    await expect(response).resolves.toEqual(receipt);
    expect(vi.mocked(submitAnswer).mock.calls[0]?.[2]?.aborted).toBe(true);
  });
  it.each([
    new ApiResponseError(401, { code: "auth", message: "auth" }),
    new ApiResponseError(409, { code: "timing", message: "timing" }),
    new ApiResponseError(503, { code: "busy", message: "busy" }),
    new ApiContractError("invalid"),
  ])("業務・HTTP・契約エラーでは再送しない: %s", async (error) => {
    vi.mocked(submitAnswer).mockRejectedValue(error);
    await expect(submitTimedAnswer(pending, 3, () => true)).rejects.toBe(error);
    expect(submitAnswer).toHaveBeenCalledTimes(1);
  });
  it("期限・復帰確認・session世代の条件が失われれば再送せず保存照会へ委ねる", async () => {
    vi.mocked(submitAnswer).mockRejectedValue(new ApiTransportError("offline"));
    await expect(submitTimedAnswer(pending, 3, () => false)).rejects.toBeInstanceOf(ApiTransportError);
    expect(submitAnswer).toHaveBeenCalledTimes(1);
  });
});
