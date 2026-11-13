import { describe, expect, it } from "vitest";

import { canonicalJson, canonicalSha256 } from "../src/canonical-json.js";
import { LoadTestManifestSchema } from "../src/contracts.js";
import {
  loadK6RuntimeManifest,
  projectK6RuntimeManifest,
  requireBoundedRuntimeManifestText,
  RuntimeManifestRefusal,
} from "../src/generator/runtime-manifest.js";
import { validManifestFixture } from "./fixtures/manifest.js";
import {
  sha256Text,
  validRuntimeManifestFixture,
} from "./fixtures/runtime-manifest.js";

function loadFixture(
  overrides: Readonly<Record<string, string | undefined>> = {},
  rawOverride?: string,
) {
  const fixture = validRuntimeManifestFixture();
  return loadK6RuntimeManifest(
    rawOverride ?? fixture.raw,
    { ...fixture.environment, ...overrides },
    sha256Text,
  );
}

function loadMatchingEnvelope(value: unknown) {
  const raw = canonicalJson(value);
  return loadK6RuntimeManifest(
    raw,
    { LOAD_TEST_RUNTIME_MANIFEST_SHA256: sha256Text(raw) },
    sha256Text,
  );
}

describe("k6 runtime manifest", () => {

  it.each([
    [{ LOAD_TEST_RUNTIME_MANIFEST_SHA256: undefined }, "missing"],
    [{ LOAD_TEST_RUNTIME_MANIFEST_SHA256: "A".repeat(64) }, "uppercase"],
    [{ LOAD_TEST_RUNTIME_MANIFEST_SHA256: "a".repeat(63) }, "short"],
    [{ LOAD_TEST_RUNTIME_MANIFEST_SHA256: `${"b".repeat(64)}x` }, "suffix"],
  ] as const)("不正なgenerator環境を固定codeで拒否する: %s", (overrides, _label) => {
    void _label;
    expect(() => loadFixture(overrides)).toThrowError(
      new RuntimeManifestRefusal("generator_environment_invalid"),
    );
  });

  it("manifest本文が無い環境を通信前に拒否する", () => {
    const fixture = validRuntimeManifestFixture();
    expect(() =>
      loadK6RuntimeManifest(undefined, fixture.environment, sha256Text),
    ).toThrowError(new RuntimeManifestRefusal("generator_environment_invalid"));
  });

  it("壊れたJSONを内容非依存の固定codeで拒否する", () => {
    const fixture = validRuntimeManifestFixture();
    expect(() =>
      loadK6RuntimeManifest(
        '{"client_join_token":"do-not-leak"',
        fixture.environment,
        sha256Text,
      ),
    ).toThrowError(new RuntimeManifestRefusal("generator_manifest_invalid"));
  });

  it.each([
    [
      (value: Record<string, unknown>) => {
        value["extra"] = true;
      },
      "top-level extra",
    ],
    [
      (value: Record<string, unknown>) => {
        const projection = value["projection"] as Record<string, unknown>;
        const event = projection["event"] as Record<string, unknown>;
        event["extra"] = true;
      },
      "nested extra",
    ],
    [
      (value: Record<string, unknown>) => {
        const projection = value["projection"] as Record<string, unknown>;
        delete projection["schema_revision"];
      },
      "missing field",
    ],
    [
      (value: Record<string, unknown>) => {
        const projection = value["projection"] as Record<string, unknown>;
        projection["run_id"] = 1;
      },
      "type drift",
    ],
  ] as const)("strict envelope境界を拒否する: %s", (mutate, _label) => {
    void _label;
    const value = structuredClone(validRuntimeManifestFixture().envelope) as unknown as Record<
      string,
      unknown
    >;
    mutate(value);
    expect(() => loadMatchingEnvelope(value)).toThrowError(
      new RuntimeManifestRefusal("generator_manifest_invalid"),
    );
  });

  it("非canonical JSONは同じ意味とhashでも拒否する", () => {
    const fixture = validRuntimeManifestFixture();
    const nonCanonical = JSON.stringify(fixture.envelope, null, 2);
    expect(() => loadFixture({}, nonCanonical)).toThrowError(
      new RuntimeManifestRefusal("generator_manifest_binding_mismatch"),
    );
  });

  it("projection差し替えをenvelope hash不一致で拒否する", () => {
    const fixture = validRuntimeManifestFixture();
    const replacement = structuredClone(fixture.envelope);
    replacement.projection.public_origin = "https://replacement.aws.internal";

    expect(() =>
      loadK6RuntimeManifest(
        canonicalJson(replacement),
        fixture.environment,
        sha256Text,
      ),
    ).toThrowError(
      new RuntimeManifestRefusal("generator_manifest_binding_mismatch"),
    );
  });

  it("旧v1 runtime envelopeを拒否する", () => {
    const envelope = structuredClone(
      validRuntimeManifestFixture().envelope,
    ) as unknown as Record<string, unknown>;
    envelope["format"] = "load-test-k6-runtime-envelope-v1";

    expect(() => loadMatchingEnvelope(envelope)).toThrowError(
      new RuntimeManifestRefusal("generator_manifest_invalid"),
    );
  });

  it("full manifest/scenario hashのmix-and-matchもenvelope hashで拒否する", () => {
    const fixture = validRuntimeManifestFixture();
    const replacement = structuredClone(fixture.envelope);
    replacement.manifest_sha256 = "c".repeat(64);
    replacement.scenario_sha256 = "d".repeat(64);

    expect(() =>
      loadK6RuntimeManifest(
        canonicalJson(replacement),
        fixture.environment,
        sha256Text,
      ),
    ).toThrowError(
      new RuntimeManifestRefusal("generator_manifest_binding_mismatch"),
    );
  });

  it("Python whitespace正規化後のhost projectionだけをchildへ渡す", () => {
    const source = validManifestFixture();
    source.event.title += "\u0085";
    const normalized = LoadTestManifestSchema.parse(source);
    const envelope = projectK6RuntimeManifest(normalized, {
      manifestSha256: canonicalSha256(normalized),
      scenarioSha256: canonicalSha256(normalized.scenario),
    });
    const raw = canonicalJson(envelope);

    expect(normalized.event.title.endsWith("\u0085")).toBe(false);
    expect(
      loadK6RuntimeManifest(
        raw,
        { LOAD_TEST_RUNTIME_MANIFEST_SHA256: sha256Text(raw) },
        sha256Text,
      ).manifest.run_id,
    ).toBe(normalized.run_id);
  });

  it.each([
    "https://quiz.aws.internal.",
    "https://a-.com",
    "https://a_b.com",
    "https://quiz.aws.internal:0",
  ])("hostが受理したOriginをk6側で再解釈して拒否しない: %s", (origin) => {
    const source = validManifestFixture();
    source.public_origin = origin;
    const normalized = LoadTestManifestSchema.parse(source);
    const envelope = projectK6RuntimeManifest(normalized, {
      manifestSha256: canonicalSha256(normalized),
      scenarioSha256: canonicalSha256(normalized.scenario),
    });

    expect(() => loadMatchingEnvelope(envelope)).not.toThrow();
  });

  it.each([
    "http://quiz.aws.internal",
    "https://user@quiz.aws.internal",
    "https://quiz.aws.internal/path",
    "https://quiz.aws.internal?query=1",
  ])("k6で安全に固定pathを連結できないOrigin形状を拒否する: %s", (origin) => {
    const envelope = structuredClone(validRuntimeManifestFixture().envelope);
    envelope.projection.public_origin = origin;
    expect(() => loadMatchingEnvelope(envelope)).toThrowError(
      new RuntimeManifestRefusal("generator_manifest_invalid"),
    );
  });

  it("単一env値をUTF-8 byteで64 KiB未満に固定する", () => {
    expect(requireBoundedRuntimeManifestText("x".repeat(65_535))).toHaveLength(
      65_535,
    );
    expect(() =>
      requireBoundedRuntimeManifestText("x".repeat(65_536)),
    ).toThrowError(new RuntimeManifestRefusal("generator_manifest_invalid"));
    expect(requireBoundedRuntimeManifestText("😀".repeat(16_383))).toHaveLength(
      32_766,
    );
    expect(() =>
      requireBoundedRuntimeManifestText("😀".repeat(16_384)),
    ).toThrowError(new RuntimeManifestRefusal("generator_manifest_invalid"));
    expect(() =>
      loadK6RuntimeManifest(
        "x".repeat(65_536),
        { LOAD_TEST_RUNTIME_MANIFEST_SHA256: "0".repeat(64) },
        sha256Text,
      ),
    ).toThrowError(new RuntimeManifestRefusal("generator_manifest_invalid"));
  });
});
