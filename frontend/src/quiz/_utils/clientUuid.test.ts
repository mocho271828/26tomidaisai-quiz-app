import { describe, expect, it, vi } from "vitest";
import { z } from "zod";

import { createClientUuid } from "./clientUuid";

describe("createClientUuid", () => {
  it("secure contextではbrowser標準のrandomUUIDを使う", () => {
    const expected = "123e4567-e89b-42d3-a456-426614174000";
    const getRandomValues = vi.fn((bytes: Uint8Array) => bytes);
    const randomUUID = vi.fn(() => expected);

    expect(createClientUuid({ getRandomValues, randomUUID })).toBe(expected);
    expect(randomUUID).toHaveBeenCalledOnce();
    expect(getRandomValues).not.toHaveBeenCalled();
  });

  it("LAN HTTPでrandomUUIDがなくてもgetRandomValuesからUUID v4を生成する", () => {
    const getRandomValues = vi.fn((bytes: Uint8Array) => {
      bytes.fill(0xff);
      return bytes;
    });

    const uuid = createClientUuid({ getRandomValues });

    expect(uuid).toBe("ffffffff-ffff-4fff-bfff-ffffffffffff");
    expect(z.uuid().parse(uuid)).toBe(uuid);
    expect(getRandomValues).toHaveBeenCalledOnce();
    expect(getRandomValues.mock.calls[0]?.[0]).toHaveLength(16);
  });
});
