import { createHash } from "node:crypto";

import { describe, expect, it } from "vitest";

import { uuid5Ascii, type Sha1Binary } from "../src/generator/uuid5-core.js";

const RUN_ID = "11111111-1111-4111-8111-111111111111";
const QUESTION_ID = "91b30112-660f-5b70-8457-d126a16b2810";

const nodeSha1: Sha1Binary = (value) => {
  const digest = createHash("sha1").update(new Uint8Array(value)).digest();
  return digest.buffer.slice(
    digest.byteOffset,
    digest.byteOffset + digest.byteLength,
  ) as ArrayBuffer;
};

describe("k6-safe UUIDv5", () => {
  it("Node側plannerと同じRFC 4122 UUIDv5を生成する", () => {
    expect(uuid5Ascii(RUN_ID, `${QUESTION_ID}:1`, nodeSha1)).toBe(
      "6830027b-dcdb-55d3-ba33-be1450cb2855",
    );
    expect(uuid5Ascii(RUN_ID, `${QUESTION_ID}:600`, nodeSha1)).toBe(
      "4e8695c4-e021-5788-8162-02c8a889c536",
    );
  });

  it("namespace、ASCII name、SHA-1長の境界違反を固定codeで拒否する", () => {
    expect(() => uuid5Ascii("not-a-uuid", `${QUESTION_ID}:1`, nodeSha1)).toThrow(
      "generator_uuid5_input_invalid",
    );
    expect(() => uuid5Ascii(RUN_ID, "問題:1", nodeSha1)).toThrow(
      "generator_uuid5_input_invalid",
    );
    expect(() =>
      uuid5Ascii(RUN_ID, `${QUESTION_ID}:1`, () => new ArrayBuffer(19)),
    ).toThrow("generator_uuid5_digest_invalid");
  });
});
