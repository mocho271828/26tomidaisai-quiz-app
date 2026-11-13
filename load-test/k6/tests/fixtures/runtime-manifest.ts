import { createHash } from "node:crypto";

import { canonicalJson, canonicalSha256 } from "../../src/canonical-json.js";
import { LoadTestManifestSchema } from "../../src/contracts.js";
import { projectK6RuntimeManifest } from "../../src/generator/runtime-manifest.js";
import { validManifestFixture } from "./manifest.js";

export function validRuntimeManifestFixture() {
  const sourceManifest = LoadTestManifestSchema.parse(validManifestFixture());
  const envelope = projectK6RuntimeManifest(sourceManifest, {
    manifestSha256: canonicalSha256(sourceManifest),
    scenarioSha256: canonicalSha256(sourceManifest.scenario),
  });
  const raw = canonicalJson(envelope);
  return {
    sourceManifest,
    envelope,
    raw,
    environment: {
      LOAD_TEST_RUNTIME_MANIFEST_JSON: raw,
      LOAD_TEST_RUNTIME_MANIFEST_SHA256: sha256Text(raw),
    },
  } as const;
}

export function sha256Text(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}
