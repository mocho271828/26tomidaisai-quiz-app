import { describe, expect, it } from "vitest";
import { ApiCancellation, cancellationReason, forwardedCancellation } from "./cancellation";

describe("safe API cancellation", () => {
  it("任意の秘密値を理由分類や子signalへ転記しない", () => {
    const controller = new AbortController();
    controller.abort(new DOMException("private-cookie-value", "AbortError"));
    expect(cancellationReason(controller.signal)).toBeUndefined();
    expect(forwardedCancellation(controller.signal).message).not.toContain("private");
  });

  it("最初の期限切れを後続の正常取消で上書きしない", () => {
    const controller = new AbortController();
    controller.abort(new ApiCancellation("deadline"));
    controller.abort(new ApiCancellation("superseded"));
    expect(cancellationReason(controller.signal)).toBe("deadline");
    expect(forwardedCancellation(controller.signal).name).toBe("TimeoutError");
  });
});
