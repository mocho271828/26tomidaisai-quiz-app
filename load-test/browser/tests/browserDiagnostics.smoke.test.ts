import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium } from "playwright";
import { expect, it } from "vitest";
import { z } from "zod";
import { configSchema } from "../src/config.js";
import { Evidence } from "../src/evidence.js";
import { browserProbeSchema, flushBrowserDiagnostics } from "../src/browserDiagnostics.js";
import { createActor } from "../src/participant.js";

// 固定HTMLで計測器のbrowser内動作だけを確認する。TLS・API・Reactの実走合格には数えない。
it.runIf(process.env.RUN_BROWSER_DIAGNOSTICS_SMOKE === "1")("records browser DOM and transport without page contents", async () => {
  const directory = await mkdtemp(join(tmpdir(), "quiz-browser-probe-"));
  const config = configSchema.parse({
    runId: "diagnostics-smoke", sourceCommit: "0".repeat(40), baseUrl: "https://127.0.0.1", count: 1,
    ordinalStart: 1, startAt: new Date().toISOString(), durationSeconds: 180, joinSpreadMs: 0,
    operatorStartDelaySeconds: 10, preScheduleMarginMs: 1000, resultHoldMs: 3000, answerDelayMs: 0,
    scenario: "normal", faultAfterSeconds: 5, faultDurationMs: 100, outputDirectory: directory,
    channel: "chromium", headless: true,
  });
  const evidence = new Evidence(config, "diagnostics");
  const browser = await chromium.launch({headless: true});
  try {
    const actor = await createActor(browser, 1, config, evidence);
    await actor.page.route("https://127.0.0.1/", route => route.fulfill({status: 200,
      contentType: "text/html", body: "<!doctype html><title>fixture</title><main></main>"}));
    await actor.page.route("**/participant/session", route => route.fulfill({status: 200,
      headers: {"server-timing": "api_total;dur=10, participant_metadata;dur=3, dispatch_wait;dur=0.5"},
      contentType: "application/json", body: "{}"}));
    await actor.page.goto(config.baseUrl);
    await actor.page.evaluate(async () => {
      await fetch("/api/v1/participant/session");
      const stage = document.createElement("section");
      stage.className = "participant-question";
      const badge = document.createElement("strong");
      badge.className = "participant-round-badge"; badge.textContent = "第1問";
      badge.dataset.roundNumber = "3";
      const receipt = document.createElement("span");
      receipt.className = "answer-state-accepted"; receipt.textContent = "受付済み";
      stage.append(badge, receipt); document.body.append(stage);
      await new Promise(resolve => setTimeout(resolve, 100));
      badge.dataset.roundNumber = "4";
      await new Promise(resolve => setTimeout(resolve, 100));
      const result = document.createElement("section");
      result.className = "participant-result-stage";
      result.innerHTML = '<span class="participant-result-round" data-round-number="10">第8問</span><section class="participant-score-result participant-result-pending">結果待ち</section>';
      document.body.append(result);
      await new Promise(resolve => setTimeout(resolve, 100));
      result.querySelector(".participant-result-round")!.setAttribute("data-round-number", "7");
      result.querySelector(".participant-score-result")!.classList.remove("participant-result-pending");
      await new Promise(resolve => setTimeout(resolve, 100));
    });
    await flushBrowserDiagnostics(actor.page);
    await evidence.flush();
    const rows = (await readFile(join(evidence.directory, "events.jsonl"), "utf8")).trim().split("\n")
      .map(row => z.object({event: z.string(), browserProbe: browserProbeSchema.optional(),
        callbackLagMs: z.number().optional(), callbackEpochDeltaUncalibratedMs: z.number().optional(),
        callbackEpochDeltaReason: z.string().optional()}).parse(JSON.parse(row) as unknown));
    expect(evidence.count("page_error")).toBe(0);
    expect(evidence.count("invalid_browser_probe")).toBe(0);
    expect(rows.filter(row => row.browserProbe?.kind === "dom" && row.browserProbe.phase === "accepted" && row.browserProbe.visible)
      .map(row => row.browserProbe?.kind === "dom" ? row.browserProbe.round_number : null)).toEqual([3, 4]);
    expect(rows.filter(row => row.browserProbe?.kind === "dom" && row.browserProbe.phase === "result")
      .map(row => row.browserProbe?.kind === "dom" ? row.browserProbe.round_number : null)).toEqual([7]);
    expect(rows.some(row => row.browserProbe?.kind === "resource" && row.browserProbe.route === "state" && row.browserProbe.server_timing.api_total === 10)).toBe(true);
    expect(rows.some(row => row.browserProbe?.kind === "resource" && row.browserProbe.server_timing.participant_metadata === 3 && row.browserProbe.server_timing.dispatch_wait === 0.5)).toBe(true);
    expect(rows.some(row => row.event === "http_response_headers" && row.callbackEpochDeltaReason === "node_browser_epoch_not_calibrated")).toBe(true);
    expect(rows.some(row => row.event === "http" && row.callbackEpochDeltaReason === "playwright_finish_relative_origin_mismatch")).toBe(true);
    expect(rows.every(row => row.callbackLagMs === undefined)).toBe(true);
  } finally { await browser.close(); await evidence.close(); await rm(directory, {recursive: true}); }
}, 30000);
