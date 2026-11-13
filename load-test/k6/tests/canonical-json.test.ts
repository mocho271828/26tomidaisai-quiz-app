import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";
import { z } from "zod";

import {
  CanonicalJsonError,
  canonicalJson,
  canonicalJsonBytes,
  canonicalSha256,
} from "../src/canonical-json.js";

const fixtureSchema = z.strictObject({
  value: z.unknown(),
  canonical_ascii: z.string(),
  sha256: z.string().regex(/^[0-9a-f]{64}$/),
});

describe("canonical JSON", () => {
  it("Python ensure_ascii bytes and SHA-256に一致する", () => {
    const fixture = fixtureSchema.parse(
      JSON.parse(
        readFileSync(new URL("../../fixtures/canonical-python-golden.json", import.meta.url), "utf8"),
      ),
    );

    expect(canonicalJson(fixture.value)).toBe(fixture.canonical_ascii);
    expect(Buffer.from(canonicalJsonBytes(fixture.value))).toEqual(
      Buffer.from(fixture.canonical_ascii, "utf8"),
    );
    expect(canonicalSha256(fixture.value)).toBe(fixture.sha256);
  });

  it("Python code-point orderでobject keyを並べる", () => {
    expect(canonicalJson({ "\u{10000}": 1, "\ue000": 2 })).toBe(
      "{\"\\ue000\":2,\"\\ud800\\udc00\":1}",
    );
  });

  it.each([1.5, Number.NaN, Number.POSITIVE_INFINITY, Number.MAX_SAFE_INTEGER + 1])(
    "小数・非有限・unsafe integerを拒否する: %s",
    (value) => {
      expect(() => canonicalJson({ value })).toThrow(CanonicalJsonError);
    },
  );

  it("undefined、class instance、循環参照を拒否する", () => {
    const cyclic: { self?: unknown } = {};
    cyclic.self = cyclic;
    expect(() => canonicalJson({ value: undefined })).toThrow(CanonicalJsonError);
    expect(() => canonicalJson(new Date())).toThrow(CanonicalJsonError);
    expect(() => canonicalJson(cyclic)).toThrow(CanonicalJsonError);
  });

  it("sparse arrayとsymbol propertyをJSON値として受理しない", () => {
    const sparse = Array<number>(1);
    const symbolValue = { value: 1, [Symbol("hidden")]: 2 };
    expect(() => canonicalJson(sparse)).toThrow(CanonicalJsonError);
    expect(() => canonicalJson(symbolValue)).toThrow(CanonicalJsonError);
  });
});
