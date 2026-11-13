import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium } from "playwright";
import { expect, it } from "vitest";
import { configSchema } from "../src/config.js";
import { Evidence } from "../src/evidence.js";
import { createManualParticipant, type ManualParticipant } from "../src/manualParticipant.js";
import { participantIdentity } from "../src/participantIdentity.js";

const enabled = process.env.RUN_BROWSER_MANUAL_SMOKE === "1";

it.runIf(enabled)("records the header identity after joining without waiting for a question", async () => {
  const directory = await mkdtemp(join(tmpdir(), "quiz-identity-smoke-"));
  const key = Buffer.alloc(32, 7);
  const config = configSchema.parse({runId: "identity-smoke", sourceCommit: "0".repeat(40),
    baseUrl: "https://127.0.0.1", count: 1, ordinalStart: 1, startAt: new Date().toISOString(),
    durationSeconds: 180, joinSpreadMs: 0, operatorStartDelaySeconds: 10, preScheduleMarginMs: 1000,
    resultHoldMs: 3000, answerDelayMs: 0, scenario: "normal", faultAfterSeconds: 5,
    faultDurationMs: 100, outputDirectory: directory, channel: "chromium", headless: true,
    identityKeyFile: join(directory, "identity.key")});
  await writeFile(config.identityKeyFile!, key.toString("hex") + "\n", {mode: 0o600});
  const evidence = new Evidence(config, "identity");
  const browser = await chromium.launch({headless: true});
  let participant: ManualParticipant | undefined;
  try {
    participant = await createManualParticipant(browser, 1, config, evidence,
      {operationTimeoutMs: 3000, deadlineAtMs: Date.now() + 30000});
    const page = participant.actor.page;
    // 旧selectorなら有限時間で再現し、稼働中のAWSには接続しない。
    page.setDefaultTimeout(1000);
    await participant.actor.context.addCookies([{name: "synthetic_context_cookie", value: "fixture-cookie",
      domain: "127.0.0.1", path: "/", secure: true, httpOnly: true, sameSite: "Strict"}]);
    await page.route("https://127.0.0.1/", route => route.fulfill({contentType: "text/html; charset=utf-8",
      body: '<button id="join">参加する</button><script>document.getElementById("join").onclick=()=>{document.body.innerHTML=`<main class="participant-live-shell"><header class="participant-header"><div class="participant-header-id"><span>あなたのID</span><strong>ABC</strong></div></header><section><p>開始をお待ちください</p><div class="participant-ticket"><span>あなたの参加者ID</span><strong>ABC</strong></div></section></main>`}</script>'}));
    await participant.prepare();
    await participant.dispatch({runId: config.runId, commandId: "join", kind: "join",
      issuedAtMs: Date.now(), target: {ordinalStart: 1, count: 1}, spreadMs: 0});
    await expect.poll(() => participant!.status().joined).toBe(true);
    await expect.poll(() => participant!.status().pending).toBe(0);
    expect(participant.status()).toMatchObject({joined: true, failed: false, timeouts: 0});
    expect(await page.locator(".participant-question").count()).toBe(0);
    expect(evidence.count("participant_identity")).toBe(1);
    expect(evidence.count("manual_operation_failed")).toBe(0);
    await evidence.flush();
    const output = await readFile(join(evidence.directory, "events.jsonl"), "utf8");
    expect(output).toContain(participantIdentity(key, config.runId, "ABC").participantKey);
    expect(output).not.toContain("ABC");
    expect(output).not.toContain(key.toString("hex"));
    expect(output).not.toContain("fixture-cookie");
  } finally {
    await participant?.stop();
    await browser.close();
    await evidence.close();
    key.fill(0);
    await rm(directory, {recursive: true});
  }
}, 15000);
