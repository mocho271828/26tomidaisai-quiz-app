import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium, type Page } from "playwright";
import { expect, it } from "vitest";
import { configSchema } from "../src/config.js";
import { Evidence } from "../src/evidence.js";
import { RunLifecycle } from "../src/lifecycle.js";
import { createActor, runActor } from "../src/participant.js";

const enabled = process.env.RUN_BROWSER_PROGRESS_SMOKE === "1";

async function fixture() {
  const directory = await mkdtemp(join(tmpdir(), "quiz-progress-smoke-"));
  const config = configSchema.parse({runId: "progress-smoke", sourceCommit: "0".repeat(40),
    baseUrl: "https://127.0.0.1", count: 1, ordinalStart: 1, startAt: new Date().toISOString(),
    durationSeconds: 180, joinSpreadMs: 0, operatorStartDelaySeconds: 10, preScheduleMarginMs: 1000,
    resultHoldMs: 3000, answerDelayMs: 0, scenario: "normal", faultAfterSeconds: 5,
    faultDurationMs: 100, outputDirectory: directory, channel: "chromium", headless: true});
  const evidence = new Evidence(config, "progress-smoke");
  const lifecycle = new RunLifecycle(evidence, "participants");
  const browser = await chromium.launch({headless: true});
  const actor = await createActor(browser, 1, config, evidence, lifecycle);
  await actor.context.addCookies([{name: "synthetic_context_cookie", value: "fixture", domain: "127.0.0.1",
    path: "/", secure: true, httpOnly: true, sameSite: "Strict"}]);
  await actor.page.route("https://127.0.0.1/", route => route.fulfill({status: 200, contentType: "text/html; charset=utf-8",
    body: '<!doctype html><button onclick="this.remove();document.body.innerHTML=\'<main class=participant-live-shell>fixture</main>\'">参加する</button>'}));
  lifecycle.startMeasurement();
  const running = runActor(actor, config, evidence, lifecycle.signal);
  try { await expect.poll(() => actor.joined, {timeout: 5000}).toBe(true); }
  catch (error: unknown) {
    lifecycle.finish("run_failed");
    await browser.close();
    await running;
    await evidence.close();
    await rm(directory, {recursive: true});
    throw error;
  }
  return {actor, config, evidence, lifecycle, running, directory, browser};
}

async function showQuestion(page: Page, round: number): Promise<void> {
  await page.evaluate(round => {
    const root = document.querySelector(".participant-live-shell")!;
    root.replaceChildren();
    const question = document.createElement("section");
    question.className = "participant-question";
    const badge = document.createElement("strong");
    badge.className = "participant-round-badge";
    badge.textContent = round === 1 ? `練習問題 ${round}` : `第${round - 1}問`;
    badge.dataset.roundNumber = String(round);
    const button = document.createElement("button");
    button.className = "answer-button";
    button.textContent = "fixture answer";
    button.addEventListener("click", () => {
      const accepted = document.createElement("span");
      accepted.className = "answer-state-accepted";
      accepted.textContent = "accepted";
      question.append(accepted);
    });
    question.append(badge, button);
    root.append(question);
    (window as unknown as {quizRunnerProgress: {round: number; state: string}}).quizRunnerProgress = {round, state: "Open"};
  }, round);
}

async function showResult(page: Page, round: number): Promise<void> {
  await page.evaluate(round => {
    const result = document.createElement("section");
    result.className = "participant-result-stage";
    const badge = document.createElement("strong");
    badge.className = "participant-result-round";
    badge.textContent = round === 1 ? `練習問題 ${round}` : `第${round - 1}問`;
    badge.dataset.roundNumber = String(round);
    const score = document.createElement("span");
    score.className = "participant-score-result";
    score.textContent = "fixture result";
    result.append(badge, score);
    document.querySelector(".participant-live-shell")!.append(result);
  }, round);
}

// 固定HTMLだけでrunnerの待機と取消を検証する。TLS/React/DB/100人試験の代用にはしない。
it.runIf(enabled)("continues six actual rounds across practice numbering when an earlier result is missing", async () => {
  const {actor, evidence, lifecycle, running, directory, browser} = await fixture();
  try {
    for (let round = 1; round <= 6; round++) {
      await showQuestion(actor.page, round);
      await expect.poll(() => actor.rounds.includes(round), {timeout: 5000}).toBe(true);
      if (round > 1) {
        await showResult(actor.page, round);
        await expect.poll(() => actor.results.includes(round), {timeout: 5000}).toBe(true);
      }
    }
    await running;
    expect(actor.rounds).toEqual([1, 2, 3, 4, 5, 6]);
    expect(actor.results).toEqual([2, 3, 4, 5, 6]);
    expect(actor.failures).toEqual([{round: 1, stage: "result_dom"}]);
    expect(actor.complete).toBe(false);
    expect(evidence.count("actor_failed")).toBe(1);
    await evidence.flush();
    expect(await readFile(join(evidence.directory, "events.jsonl"), "utf8")).toContain('"reason":"round_superseded"');
  } finally {
    lifecycle.finish("completed");
    await browser.close();
    await running;
    await evidence.close();
    await rm(directory, {recursive: true});
  }
}, 30000);

it.runIf(enabled)("cancels the active wait without synthesizing failures for unattempted rounds", async () => {
  const {actor, evidence, lifecycle, running, directory, browser} = await fixture();
  try {
    await expect.poll(() => evidence.count("round_expected"), {timeout: 5000}).toBe(1);
    lifecycle.finish("run_failed");
    await actor.context.close();
    await running;
    expect(evidence.count("actor_failed")).toBe(0);
    expect(evidence.count("actor_cancelled")).toBe(1);
    expect(evidence.count("round_expected")).toBe(1);
    expect(actor.complete).toBe(false);
  } finally {
    lifecycle.finish("completed");
    await browser.close();
    await running;
    await evidence.close();
    await rm(directory, {recursive: true});
  }
}, 30000);
