import { networkInterfaces } from "node:os";
import { z } from "zod";

import { readSecureExternalArtifact } from "./artifacts.js";
import type { LoadedManifest } from "./manifest.js";

const sha256 = z.string().regex(/^[0-9a-f]{64}$/u);
const privateIpv4 = z.ipv4().refine((value) => {
  const [first, second = -1] = value.split(".").map(Number);
  return first === 10 || (first === 172 && second >= 16 && second <= 31) ||
    (first === 192 && second === 168);
});

export const AwsPreparationEvidenceSchema = z.strictObject({
  schema_version: z.literal("aws-load-test-preparation-v1"),
  run_id: z.uuid(),
  account_id: z.string().regex(/^[0-9]{12}$/u),
  region: z.literal("ap-northeast-1"),
  source_commit: z.string().regex(/^[0-9a-f]{40}$/u),
  manifest_sha256: sha256,
  app_instance_id: z.string().regex(/^i-[0-9a-f]{8,17}$/u),
  runner_instance_id: z.string().regex(/^i-[0-9a-f]{8,17}$/u),
  app_private_ip: privateIpv4,
  runner_private_ip: privateIpv4,
  public_origin: z.string(),
  ca_sha256: sha256,
  prepared_at: z.iso.datetime(),
  expires_at: z.iso.datetime(),
}).superRefine((value, context) => {
  if (value.app_private_ip === value.runner_private_ip ||
      value.app_instance_id === value.runner_instance_id ||
      value.public_origin !== `https://${value.app_private_ip}` ||
      Date.parse(value.prepared_at) >= Date.parse(value.expires_at)) {
    context.addIssue({ code: "custom", message: "preparation topology invalid" });
  }
});

export class PreparationRefusal extends Error {
  readonly code = "generator_preparation_invalid";
  constructor() {
    super("generator_preparation_invalid");
    this.name = "PreparationRefusal";
  }
}

/** AWS側がsealした準備証拠と実runner・CAを一致させ、未準備の起動を拒否する。 */
export function loadPreparedApplicationCa(
  options: { readonly preparationEvidencePath: string; readonly applicationCaFile: string },
  loaded: LoadedManifest,
  now = Date.now(),
  localAddresses: readonly string[] = Object.values(networkInterfaces()).flatMap(
    (entries) => (entries ?? []).filter((entry) => !entry.internal).map((entry) => entry.address),
  ),
): Uint8Array {
  try {
    const evidenceFile = readSecureExternalArtifact(options.preparationEvidencePath, { maximumBytes: 16_384 });
    const evidence = AwsPreparationEvidenceSchema.parse(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(evidenceFile.body)));
    const ca = readSecureExternalArtifact(options.applicationCaFile, { maximumBytes: 65_536 });
    if (evidence.run_id !== loaded.manifest.run_id ||
        evidence.source_commit !== loaded.manifest.artifacts.source_commit ||
        evidence.manifest_sha256 !== loaded.hashes.manifestSha256 ||
        evidence.public_origin !== loaded.manifest.public_origin ||
        evidence.ca_sha256 !== ca.artifact.sha256 ||
        !localAddresses.includes(evidence.runner_private_ip) ||
        !Number.isSafeInteger(now) || now < Date.parse(evidence.prepared_at) ||
        now >= Date.parse(evidence.expires_at) ||
        Date.parse(loaded.manifest.scheduled_start_at) + 4_830_000 > Date.parse(evidence.expires_at) ||
        !new TextDecoder().decode(ca.body).startsWith("-----BEGIN CERTIFICATE-----")) {
      throw new PreparationRefusal();
    }
    return ca.body;
  } catch {
    throw new PreparationRefusal();
  }
}
