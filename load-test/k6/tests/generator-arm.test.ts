import { createHash } from "node:crypto";
import {
  chmodSync,
  lstatSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import {
  GeneratorArmRefusal,
  sealGeneratorArmMarker,
} from "../src/generator-arm.js";
import type { GeneratorArmBinding } from "../src/generator-arm.js";

const RUN_ID = "11111111-1111-4111-8111-111111111111";
const roots: string[] = [];

function runtimeBase(): string {
  const directory = realpathSync(mkdtempSync(join(tmpdir(), "k6-arm-")));
  chmodSync(directory, 0o700);
  roots.push(directory);
  return directory;
}

function binding(bundle: Uint8Array): GeneratorArmBinding {
  const sha256 = createHash("sha256").update(bundle).digest("hex");
  return {
    format: "load-test-generator-arm-v1",
    run_id: RUN_ID,
    manifest_sha256: "a".repeat(64),
    generator_image: "ghcr.io/grafana/k6@sha256:" + "b".repeat(64),
    generator_bundle_sha256: sha256,
    generator_bundle_size_bytes: bundle.byteLength,
    scheduled_start_at: "2030-01-01T00:00:00Z",
    container_name: `gs-loadtest-k6-${RUN_ID}`,
  };
}

afterEach(() => {
  for (const root of roots.splice(0)) {
    rmSync(root, { force: true, recursive: true });
  }
});

describe("generator arm marker", () => {
  it("seals one immutable bundle snapshot and one durable marker", () => {
    const base = runtimeBase();
    const bundle = Buffer.from("export default function () {}\n");
    const result = sealGeneratorArmMarker(binding(bundle), bundle, {
      runtimeBaseDirectory: base,
      effectiveUserId: process.geteuid!(),
      effectiveGroupId: process.getegid!(),
      platform: process.platform,
    });

    expect(result.bundle.name).toBe(`${RUN_ID}.generator.js`);
    expect(result.bundle.sha256).toBe(binding(bundle).generator_bundle_sha256);
    expect(result.marker.name).toBe(`${RUN_ID}.arm.json`);
    expect(lstatSync(result.bundlePath).mode & 0o7777).toBe(0o400);
    expect(
      lstatSync(join(base, "gs-loadtest-k6", result.marker.name)).mode & 0o7777,
    ).toBe(0o600);
    expect(readFileSync(result.bundlePath)).toEqual(bundle);
    expect(
      readFileSync(join(base, "gs-loadtest-k6", result.marker.name), "utf8"),
    ).toContain(`"run_id":"${RUN_ID}"`);
  });

  it("refuses a second arm for the same run even with another output", () => {
    const base = runtimeBase();
    const bundle = Buffer.from("bundle\n");
    const options = {
      runtimeBaseDirectory: base,
      effectiveUserId: process.geteuid!(),
      effectiveGroupId: process.getegid!(),
      platform: process.platform,
    } as const;
    sealGeneratorArmMarker(binding(bundle), bundle, options);

    expect(() =>
      sealGeneratorArmMarker(binding(bundle), bundle, options),
    ).toThrowError(
      expect.objectContaining<Partial<GeneratorArmRefusal>>({
        code: "generator_already_armed",
      }),
    );
  });

  it("seal済みCAがあっても別runをarmでき、CAのmode driftは拒否する", () => {
    const base = runtimeBase();
    const bundle = Buffer.from("bundle\n");
    const options = { runtimeBaseDirectory: base, effectiveUserId: process.geteuid!(), effectiveGroupId: process.getegid!(), platform: process.platform };
    sealGeneratorArmMarker(binding(bundle), bundle, options);
    const caPath = join(base, "gs-loadtest-k6", `${RUN_ID}.ca.pem`);
    writeFileSync(caPath, "fixture-cert", { mode: 0o600 });
    const next = { ...binding(bundle), run_id: "33333333-3333-4333-8333-333333333333" };
    expect(() => sealGeneratorArmMarker(next, bundle, options)).not.toThrow();
    chmodSync(caPath, 0o644);
    expect(() => sealGeneratorArmMarker({ ...next, run_id: "44444444-4444-4444-8444-444444444444" }, bundle, options)).toThrow("generator_arm_directory_invalid");
  });

  it("rejects unexpected inventory instead of deleting it", () => {
    const base = runtimeBase();
    const directory = join(base, "gs-loadtest-k6");
    const bundle = Buffer.from("bundle\n");
    sealGeneratorArmMarker(binding(bundle), bundle, {
      runtimeBaseDirectory: base,
      effectiveUserId: process.geteuid!(),
      effectiveGroupId: process.getegid!(),
      platform: process.platform,
    });
    writeFileSync(join(directory, "unexpected"), "keep\n", { mode: 0o600 });

    expect(() =>
      sealGeneratorArmMarker(
        { ...binding(bundle), run_id: "22222222-2222-4222-8222-222222222222" },
        bundle,
        {
          runtimeBaseDirectory: base,
          effectiveUserId: process.geteuid!(),
          effectiveGroupId: process.getegid!(),
          platform: process.platform,
        },
      ),
    ).toThrowError(
      expect.objectContaining<Partial<GeneratorArmRefusal>>({
        code: "generator_arm_directory_invalid",
      }),
    );
    expect(readFileSync(join(directory, "unexpected"), "utf8")).toBe("keep\n");
  });
});
