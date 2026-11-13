import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium, type Page } from "playwright";
import { expect, it } from "vitest";
import { configSchema } from "../src/config.js";
import { Evidence } from "../src/evidence.js";
import { createManualParticipant, type ManualParticipant } from "../src/manualParticipant.js";
import { manualCommandSchema } from "../src/manualProtocol.js";

const enabled = process.env.RUN_BROWSER_MANUAL_SMOKE === "1";

async function question(page: Page, round: number): Promise<void> {
  await page.evaluate(round => {
    const shell = document.querySelector(".participant-live-shell")!;
    shell.className = "participant-live-shell participant-live-shell-answering";
    shell.innerHTML = `<section class="participant-question"><strong class="participant-round-badge" data-round-number="${round}">${round === 1 ? `練習問題 ${round}` : `第${round - 1}問`}</strong></section>`;
    const root = shell.firstElementChild!;
    for (let option = 0; option < 4; option++) {
      const button = document.createElement("button");
      button.className = "answer-button"; button.textContent = `option ${option}`;
      button.onclick = () => {
        sessionStorage.setItem(`clicked-${round}`, String(option));
        const sending = {kind: "answer", phase: "sending", round_number: round,
          monotonic_ms: performance.now(), wall_time_ms: Date.now()};
        window.dispatchEvent(new CustomEvent("quiz:observation", {detail: sending}));
        const accepted = document.createElement("span"); accepted.className = "answer-state-accepted";
        accepted.textContent = "accepted"; root.append(accepted);
        window.dispatchEvent(new CustomEvent("quiz:observation", {detail: {
          kind: "answer_dom", status: "accepted", round_number: round, visible: true,
          monotonic_ms: performance.now(), wall_time_ms: Date.now(),
        }}));
      };
      root.append(button);
    }
    window.dispatchEvent(new CustomEvent("quiz:observation", {detail: {kind: "projection_received",
      round_number: round, state: "Open", has_question: true, monotonic_ms: performance.now(), wall_time_ms: Date.now()}}));
    (window as unknown as {quizRunnerProgress: {round: number; state: string}}).quizRunnerProgress = {round, state: "Open"};
    window.dispatchEvent(new CustomEvent("quiz:observation", {detail: {kind: "participant_display",
      round_number: round, phase: "answering", synchronized: true, has_question: true,
      answer_deadline_monotonic_ms: performance.now() + 10000,
      monotonic_ms: performance.now(), wall_time_ms: Date.now()}}));
  }, round);
}

it.runIf(enabled).each([6, 9])("completes %i actual rounds with the current result markup", async expectedRounds => {
  const directory = await mkdtemp(join(tmpdir(), "quiz-variable-rounds-"));
  const config = configSchema.parse({runId: "variable-rounds", sourceCommit: "0".repeat(40),
    baseUrl: "https://127.0.0.1", count: 1, ordinalStart: 1, startAt: new Date().toISOString(),
    durationSeconds: 180, joinSpreadMs: 0, operatorStartDelaySeconds: 10, preScheduleMarginMs: 1000,
    resultHoldMs: 3000, answerDelayMs: 0, scenario: "normal", faultAfterSeconds: 5,
    faultDurationMs: 100, outputDirectory: directory, channel: "chromium", headless: true});
  const evidence = new Evidence(config, "variable-rounds");
  const browser = await chromium.launch({headless: true});
  let participant: ManualParticipant | undefined;
  try {
    participant = await createManualParticipant(browser, 1, config, evidence,
      {expectedRounds, operationTimeoutMs: 3000, deadlineAtMs: Date.now() + 60000});
    await participant.actor.context.addCookies([{name: "synthetic_context_cookie", value: "fixture-variable-rounds",
      domain: "127.0.0.1", path: "/", secure: true, httpOnly: true, sameSite: "Strict"}]);
    const page = participant.actor.page;
    await page.route("https://127.0.0.1/", route => route.fulfill({contentType: "text/html; charset=utf-8",
      body: '<button onclick="this.remove();document.body.innerHTML=\'<main class=participant-live-shell>fixture</main>\'">参加する</button>'}));
    await participant.prepare();
    const base = {runId: config.runId, issuedAtMs: Date.now(), target: {ordinalStart: 1, count: 1}};
    await participant.dispatch({...base, kind: "join", commandId: "join", spreadMs: 0});
    await expect.poll(() => participant!.status().joined).toBe(true);
    for (let round = 1; round <= expectedRounds; round++) {
      await participant.dispatch({...base, kind: "answer", commandId: `round-${round}`, round,
        mode: "when-open", delayMs: 0, spreadMs: 0});
      await question(page, round);
      await expect.poll(() => participant!.status().accepted.includes(round)).toBe(true);
      expect(participant.status().complete).toBe(false);
      await page.evaluate(round => {
        document.querySelector(".participant-live-shell")!.innerHTML =
          `<section class="participant-result-stage"><header><span class="participant-result-round" data-round-number="${round}">${round === 1 ? `第${round}問 · 練習` : `第${round - 1}問`}</span></header><div role="tabpanel"><section class="participant-score-result outcome-correct">得点</section></div></section>`;
        window.dispatchEvent(new CustomEvent("quiz:observation", {detail: {kind: "participant_display",
          round_number: round, phase: "result", synchronized: true, has_question: true, result_dom_present: true,
          monotonic_ms: performance.now(), wall_time_ms: Date.now()}}));
      }, round);
      await expect.poll(() => participant!.status().results.includes(round)).toBe(true);
    }
    expect(participant.status()).toMatchObject({complete: true, failed: false, currentRound: expectedRounds});
    expect(evidence.count("invalid_ui_observation")).toBe(0);
    expect(evidence.count("result_dom")).toBe(expectedRounds);
  } finally {
    await participant?.stop();
    await browser.close();
    await evidence.close();
    await rm(directory, {recursive: true});
  }
}, 30000);

// 専用HTMLとroute.fulfillだけで実DOM clickと独立Contextを確認し、アプリやDBへ接続しない。
it.runIf(enabled)("prepares separately, controls isolated real pages and keeps unanswered results distinct", async () => {
  const directory = await mkdtemp(join(tmpdir(), "quiz-manual-smoke-"));
  const config = configSchema.parse({runId: "manual-smoke", sourceCommit: "0".repeat(40),
    baseUrl: "https://127.0.0.1", count: 2, ordinalStart: 1, startAt: new Date().toISOString(),
    durationSeconds: 180, joinSpreadMs: 0, operatorStartDelaySeconds: 10, preScheduleMarginMs: 1000,
    resultHoldMs: 3000, answerDelayMs: 0, scenario: "normal", faultAfterSeconds: 5,
    faultDurationMs: 100, outputDirectory: directory, channel: "chromium", headless: true});
  const evidence = new Evidence(config, "manual-smoke");
  const browser = await chromium.launch({headless: true});
  const participants: ManualParticipant[] = [];
  let sequence = 0;
  const dispatch = async (kind: string, fields: object = {}): Promise<void> => {
    const command = manualCommandSchema.parse({runId: config.runId, commandId: `cmd-${sequence++}`,
      issuedAtMs: Date.now(), target: {ordinalStart: 1, count: 2}, kind, ...fields});
    await Promise.all(participants.map(participant => participant.dispatch(command)));
  };
  try {
    for (let serial = 1; serial <= 2; serial++) {
      const participant = await createManualParticipant(browser, serial, config, evidence,
        {operationTimeoutMs: 3000, deadlineAtMs: Date.now() + 30000});
      participants.push(participant);
      await participant.actor.context.addCookies([{name: "synthetic_context_cookie", value: `fixture-${serial}`,
        domain: "127.0.0.1", path: "/", secure: true, httpOnly: true, sameSite: "Strict"}]);
      let requestFont!: () => void;
      let finishFont!: () => void;
      const fontRequested = new Promise<void>(resolve => { requestFont = resolve; });
      const fontReleased = new Promise<void>(resolve => { finishFont = resolve; });
      await participant.actor.page.route("https://127.0.0.1/font.woff2", async route => {
        requestFont();
        await fontReleased;
        await route.fulfill({contentType: "font/woff2", body: await readFile(new URL(
          "../../../frontend/src/assets/fonts/NotoSansJP-Variable.woff2", import.meta.url))});
      });
      await participant.actor.page.route("https://127.0.0.1/", route => route.fulfill({status: 200,
        contentType: "text/html; charset=utf-8", body: '<!doctype html><style>@font-face{font-family:fixture;src:url(/font.woff2)}button{font-family:fixture}</style><button onclick="this.remove();document.body.innerHTML=\'<main class=participant-live-shell>fixture</main>\'">参加する</button>'}));
      const preparing = participant.prepare();
      await fontRequested;
      await participant.actor.page.getByRole("button", {name: "参加する", exact: true}).waitFor();
      expect(participant.status()).toMatchObject({ready: false, joined: false});
      finishFont();
      await preparing;
      expect(await participant.actor.page.evaluate(() => document.fonts.status)).toBe("loaded");
    }
    expect(participants.map(participant => participant.status().ready)).toEqual([true, true]);
    expect(participants.map(participant => participant.status().joined)).toEqual([false, false]);
    await dispatch("join", {spreadMs: 50});
    await expect.poll(() => participants.every(participant => participant.status().joined)).toBe(true);
    await dispatch("pause");
    await dispatch("answer", {round: 1, mode: "when-open", delayMs: 100, spreadMs: 50});
    await Promise.all(participants.map(participant => question(participant.actor.page, 1)));
    expect(participants.every(participant => participant.status().clicked.length === 0)).toBe(true);
    await dispatch("resume");
    await expect.poll(() => participants.every(participant => participant.status().accepted.includes(1))).toBe(true);
    expect(await Promise.all(participants.map(participant => participant.actor.page.evaluate(() => sessionStorage.getItem("clicked-1")))))
      .toEqual(["0", "1"]);
    await participants[0]!.actor.page.evaluate(() => sessionStorage.setItem("isolated", "yes"));
    expect(await participants[1]!.actor.page.evaluate(() => sessionStorage.getItem("isolated"))).toBeNull();
    const visibilityTarget = participants[0]!;
    for (const hidden of ["display", "opacity", "inert"]) {
      const prior = evidence.count("result_dom_not_visible");
      await visibilityTarget.actor.page.evaluate(hidden => {
        const shell = document.querySelector(".participant-live-shell")!;
        shell.innerHTML = '<section class="participant-result-stage"><b class="participant-result-round" data-round-number="1">練習問題 1</b><span class="participant-score-result">fixture score</span></section>';
        const root = shell.firstElementChild as HTMLElement;
        if (hidden === "display") root.style.display = "none";
        if (hidden === "opacity") shell.setAttribute("style", "opacity:0");
        if (hidden === "inert") root.setAttribute("inert", "");
        window.dispatchEvent(new CustomEvent("quiz:observation", {detail: {kind: "participant_display", round_number: 1,
          phase: "result", synchronized: true, has_question: true, result_dom_present: true,
          monotonic_ms: performance.now(), wall_time_ms: Date.now()}}));
      }, hidden);
      await expect.poll(() => evidence.count("result_dom_not_visible")).toBe(prior + 1);
      expect(visibilityTarget.status().results).toEqual([]);
      await visibilityTarget.actor.page.locator(".participant-live-shell").evaluate(element => element.removeAttribute("style"));
    }
    for (const participant of participants) {
      await participant.actor.page.evaluate(serial => {
        const shell = document.querySelector(".participant-live-shell")!;
        shell.innerHTML = '<section class="participant-result-stage"><b class="participant-result-round" data-round-number="1">練習問題 1</b><span class="participant-score-result"></span></section>';
        if (serial === 2) shell.querySelector(".participant-score-result")!.classList.add("outcome-unanswered");
        window.dispatchEvent(new CustomEvent("quiz:observation", {detail: {kind: "participant_display", round_number: 1,
          phase: "result", synchronized: true, has_question: true, result_dom_present: true,
          monotonic_ms: performance.now(), wall_time_ms: Date.now()}}));
      }, participant.actor.serial);
    }
    await expect.poll(() => participants.every(participant => participant.status().results.includes(1))).toBe(true);
    expect(participants[0]!.status().answeredResults).toEqual([1]);
    expect(participants[1]!.status().unansweredResults).toEqual([1]);
    expect(participants.every(participant => !participant.status().complete)).toBe(true);
    await dispatch("answer", {round: 2, mode: "when-open", delayMs: 0, spreadMs: 0});
    await dispatch("stop");
    await Promise.all(participants.map(participant => question(participant.actor.page, 2)));
    expect(participants.every(participant => !participant.status().clicked.includes(2))).toBe(true);
    await evidence.flush();
    const output = await readFile(join(evidence.directory, "events.jsonl"), "utf8");
    expect(evidence.count("manual_answer_clicked")).toBe(2);
    expect(evidence.count("answer_send_to_accepted_dom")).toBe(2);
    expect(output).not.toContain("browser_probe");
    expect(output).not.toContain("fixture-");
  } finally {
    await Promise.all(participants.map(participant => participant.stop()));
    await browser.close();
    await evidence.close();
    await rm(directory, {recursive: true});
  }
}, 30000);
