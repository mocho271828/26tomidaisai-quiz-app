import { createHash } from "node:crypto";
import { chmodSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import { canonicalSha256 } from "../src/canonical-json.js";
import { loadPreparedApplicationCa, PreparationRefusal } from "../src/preparation.js";
import { loadedManifest } from "./generator-event-fixtures.js";

const roots: string[] = [];
const caBody = "-----BEGIN CERTIFICATE-----\nfixture-ca\n-----END CERTIFICATE-----\n";

function fixture() {
  const directory = realpathSync(mkdtempSync(join(tmpdir(), "k6-preparation-")));
  roots.push(directory);
  chmodSync(directory, 0o700);
  const loaded = loadedManifest();
  loaded.manifest.public_origin = "https://10.0.1.20";
  const binding = { ...loaded, hashes: { ...loaded.hashes, manifestSha256: canonicalSha256(loaded.manifest) } };
  const evidence = {
    schema_version: "aws-load-test-preparation-v1", run_id: loaded.manifest.run_id,
    account_id: "123456789012", region: "ap-northeast-1",
    source_commit: loaded.manifest.artifacts.source_commit,
    manifest_sha256: binding.hashes.manifestSha256,
    app_instance_id: "i-11111111", runner_instance_id: "i-22222222",
    app_private_ip: "10.0.1.20", runner_private_ip: "10.0.1.21",
    public_origin: loaded.manifest.public_origin,
    ca_sha256: createHash("sha256").update(caBody).digest("hex"),
    prepared_at: "2026-09-03T00:00:00Z", expires_at: "2026-09-03T03:00:00Z",
  };
  const options = { preparationEvidencePath: join(directory, "preparation.json"), applicationCaFile: join(directory, "ca.pem") };
  const write = () => {
    writeFileSync(options.preparationEvidencePath, JSON.stringify(evidence), { mode: 0o600 });
    writeFileSync(options.applicationCaFile, caBody, { mode: 0o600 });
  };
  write();
  return { options, binding, evidence, write, read: () => loadPreparedApplicationCa(options, binding, Date.parse("2026-09-03T00:30:00Z"), ["10.0.1.21"]) };
}

afterEach(() => { roots.splice(0).forEach((path) => rmSync(path, { recursive: true, force: true })); });

describe("AWS preparation evidence", () => {
  it("同一run/source/manifestと実runner IPへ束縛されたCAだけを返す", () => {
    expect(new TextDecoder().decode(fixture().read())).toBe(caBody);
  });

  it.each([
    ["run_id", "33333333-3333-4333-8333-333333333333"],
    ["source_commit", "b".repeat(40)], ["manifest_sha256", "c".repeat(64)],
    ["ca_sha256", "d".repeat(64)], ["runner_private_ip", "10.0.1.22"],
    ["app_private_ip", "203.0.113.2"], ["public_origin", "https://other.example"],
    ["runner_instance_id", "i-11111111"], ["prepared_at", "2026-09-03T00:45:00Z"],
    ["expires_at", "2026-09-03T02:20:00Z"], ["prepared_at", "2026-09-03T09:00:00+09:00"],
  ])("準備証跡の不一致・期限不足を拒否する: %s", (key, value) => {
    const test = fixture();
    Object.assign(test.evidence, { [key]: value });
    test.write();
    expect(test.read).toThrow(PreparationRefusal);
  });

  it("準備証跡欠落、過大permission、CA改変を固定codeで拒否する", () => {
    const missing = fixture();
    rmSync(missing.options.preparationEvidencePath);
    expect(missing.read).toThrow("generator_preparation_invalid");
    const unsafe = fixture();
    chmodSync(unsafe.options.applicationCaFile, 0o644);
    expect(unsafe.read).toThrow("generator_preparation_invalid");
    const changed = fixture();
    writeFileSync(changed.options.applicationCaFile, "secret-invalid-certificate");
    expect(changed.read).toThrow("generator_preparation_invalid");
  });
});
