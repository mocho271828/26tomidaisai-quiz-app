import { createHmac } from "node:crypto";
import { constants } from "node:fs";
import { open } from "node:fs/promises";
import { z } from "zod";
import type { Page } from "playwright";
import type { BrowserRunConfig } from "./config.js";
import type { Evidence } from "./evidence.js";

const labelSchema = z.string().regex(/^[A-Z]{3}$/u);
const keySchema = z.string().regex(/^[a-f0-9]{64}\n?$/u);
const keyDomain = "quiz.load-test.identity.v1";

export function participantIdentity(key: Buffer, runId: string, label: unknown): {
  participantKey: string; identityKeyFingerprint: string;
} {
  const parsed = labelSchema.safeParse(label);
  if (!parsed.success || key.length !== 32) throw new Error("participant_identity_invalid");
  const prefix = `${keyDomain}\0${runId}`;
  return {
    participantKey: createHmac("sha256", key).update(`${prefix}\0${parsed.data}`).digest("hex"),
    identityKeyFingerprint: createHmac("sha256", key).update(prefix).digest("hex"),
  };
}

/** 所有者限定の通常ファイルを検証する。返した鍵は呼出側で使用後にゼロ化する。 */
async function readIdentityKey(path: string): Promise<Buffer> {
  const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const metadata = await file.stat();
    if (!metadata.isFile() || (metadata.mode & 0o777) !== 0o600 || metadata.nlink !== 1 ||
      metadata.uid !== process.getuid?.() || metadata.size > 65) {
      throw new Error("participant_identity_key_permissions_invalid");
    }
    const parsed = keySchema.safeParse(await file.readFile("utf8"));
    if (!parsed.success) throw new Error("participant_identity_key_invalid");
    return Buffer.from(parsed.data.trim(), "hex");
  } finally { await file.close(); }
}

export async function recordParticipantIdentity(
  page: Page, serial: number, config: BrowserRunConfig, evidence: Evidence,
): Promise<void> {
  if (config.identityKeyFile === undefined) {
    evidence.event({event: "participant_identity_unavailable", serial});
    return;
  }
  // IDは画面から一度読み、run専用鍵で仮名化する。生値や認証情報は保存しない。
  const key = await readIdentityKey(config.identityKeyFile);
  try {
    const label = await page.locator(".participant-header-id strong").textContent();
    evidence.event({event: "participant_identity", serial, ...participantIdentity(key, config.runId, label?.trim())});
  } finally { key.fill(0); }
}
