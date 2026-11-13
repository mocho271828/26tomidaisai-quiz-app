import { z } from "zod";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiCancellation } from "./cancellation";

import {
  ApiContractError,
  ApiResponseError,
  requestJson,
} from "./client";

const responseSchema = z.strictObject({ value: z.string() });

describe("HTTP API client", () => {
  afterEach(() => vi.unstubAllGlobals());

  it.each(["superseded", "hidden", "unmount", "deadline"] as const)("%sを要求連番と固定取消理由で照合する", async (reason) => {
    const observations: unknown[] = [];
    const listener = (event: Event): void => { observations.push((event as CustomEvent<unknown>).detail); };
    window.addEventListener("quiz:observation", listener);
    const controller = new AbortController();
    const fetchMock = vi.fn((_url: string, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init.signal?.addEventListener("abort", () => reject(init.signal?.reason), { once: true });
    }));
    vi.stubGlobal("fetch", fetchMock);
    try {
      const request = requestJson("/participant/result", responseSchema,
        { signal: controller.signal, observationOperation: "participant_result" });
      const rejected = expect(request).rejects.toBeInstanceOf(DOMException);
      controller.abort(new ApiCancellation(reason));
      await rejected;
      const init = fetchMock.mock.calls[0]?.[1];
      const sequence = Number(new Headers(init?.headers).get("x-quiz-observation-sequence"));
      expect(sequence).toBeGreaterThan(0);
      expect(observations[0]).toMatchObject({ phase: "started", request_sequence: sequence });
      expect(observations[1]).toMatchObject({ phase: "settled", request_sequence: sequence,
        outcome: reason === "deadline" ? "timeout" : "aborted", cancellation_reason: reason });
    } finally { window.removeEventListener("quiz:observation", listener); }
  });

  it("成功応答をZod strict境界で検証する", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ value: "ok" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(requestJson("/test", responseSchema)).resolves.toEqual({
      value: "ok",
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/test",
      expect.objectContaining({ credentials: "same-origin" }),
    );
  });

  it("malformed 200を成功として扱わない", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ value: "ok", leaked: "secret" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await expect(requestJson("/test", responseSchema)).rejects.toBeInstanceOf(
      ApiContractError,
    );
  });

  it("観測設定をfetchへ漏らさず、Zod検証を通過した応答だけをvalidatedとして記録する", async () => {
    const observations: unknown[] = [];
    const listener = (event: Event): void => { observations.push((event as CustomEvent<unknown>).detail); };
    window.addEventListener("quiz:observation", listener);
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ value: "ok", leaked: "private" }), {
      status: 200, headers: { "Content-Type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    try {
      await expect(requestJson("/test", responseSchema, { observationOperation: "participant_state" }))
        .rejects.toBeInstanceOf(ApiContractError);

      expect(fetchMock.mock.calls[0]?.[1]).not.toHaveProperty("observationOperation");
      expect(observations).toHaveLength(2);
      expect(observations[1]).toMatchObject({ phase: "settled", outcome: "contract_error", status: 200 });
      expect(JSON.stringify(observations)).not.toContain("private");
    } finally {
      window.removeEventListener("quiz:observation", listener);
    }
  });

  it("本人GETの期限切れを取消や通信失敗と分けて観測する", async () => {
    const observations: unknown[] = [];
    const listener = (event: Event): void => { observations.push((event as CustomEvent<unknown>).detail); };
    window.addEventListener("quiz:observation", listener);
    const controller = new AbortController();
    controller.abort(new DOMException("private-timeout-detail", "TimeoutError"));
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(controller.signal.reason));
    try {
      await expect(requestJson("/participant/result", responseSchema,
        { signal: controller.signal, observationOperation: "participant_result" })).rejects.toMatchObject({ name: "TimeoutError" });
      expect(observations[1]).toMatchObject({ operation: "participant_result", phase: "settled", outcome: "timeout" });
      expect(JSON.stringify(observations)).not.toContain("private-");
    } finally {
      window.removeEventListener("quiz:observation", listener);
    }
  });

  it("headers受信後のbody中断をJSON契約違反へ変換しない", async () => {
    const response = new Response(null, { status: 200 });
    const abort = new DOMException("aborted", "AbortError");
    vi.spyOn(response, "json").mockRejectedValue(abort);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response));

    await expect(requestJson("/test", responseSchema, { observationOperation: "answer_post" }))
      .rejects.toBe(abort);
  });

  it("型付きAPI errorをstatus/code付きで返す", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ code: "state_conflict", message: "更新してください" }), {
          status: 409,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    const promise = requestJson("/test", responseSchema);
    await expect(promise).rejects.toBeInstanceOf(ApiResponseError);
    await expect(promise).rejects.toMatchObject({
      status: 409,
      code: "state_conflict",
    });
  });

  it.each(["state_conflict", "private-participant-answer"])(
    "API拒否 %sの観測は安全なcodeと相関に限定し、message・本文を記録しない", async (code) => {
      const observations: unknown[] = [];
      const listener = (event: Event): void => { observations.push((event as CustomEvent<unknown>).detail); };
      window.addEventListener("quiz:observation", listener);
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
        code, message: "private-question-and-answer",
      }), { status: 409, headers: {
        "x-quiz-request-id": "a".repeat(32), "x-quiz-answer-trace": "b".repeat(64),
      } })));
      try {
        await expect(requestJson("/private-participant", responseSchema, { observationOperation: "answer_post" }))
          .rejects.toMatchObject({ status: 409, code });

        expect(observations[1]).toMatchObject({
          outcome: "http_error", api_code: code === "state_conflict" ? code : "other",
          server_request_id: "a".repeat(32), answer_trace: "b".repeat(64),
        });
        expect(JSON.stringify(observations)).not.toContain("private-");
      } finally {
        window.removeEventListener("quiz:observation", listener);
      }
    },
  );

  it("拒否JSONの中断では未取得codeを記録しない", async () => {
    const observations: unknown[] = [];
    const listener = (event: Event): void => { observations.push((event as CustomEvent<unknown>).detail); };
    window.addEventListener("quiz:observation", listener);
    const response = new Response(null, { status: 409, headers: { "x-quiz-request-id": "a".repeat(32) } });
    vi.spyOn(response, "json").mockRejectedValue(new DOMException("private-abort", "AbortError"));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response));
    try {
      await expect(requestJson("/private-participant", responseSchema, { observationOperation: "answer_post" }))
        .rejects.toBeInstanceOf(DOMException);

      expect(observations[1]).toMatchObject({ outcome: "aborted", server_request_id: "a".repeat(32) });
      expect(observations[1]).not.toHaveProperty("api_code");
      expect(observations[1]).not.toHaveProperty("answer_trace");
      expect(JSON.stringify(observations)).not.toContain("private-");
    } finally {
      window.removeEventListener("quiz:observation", listener);
    }
  });
});
