import { chromium, type Browser, type Page } from "playwright";
import { expect, it } from "vitest";
import { PlaywrightManualDriver } from "../src/manualBrowser.js";
import type { ManualAction } from "../src/manualProtocol.js";

const enabled = process.env.RUN_BROWSER_MANUAL_SMOKE === "1";
type AnswerCommand = Extract<ManualAction, {kind: "answer"}>;
function command(round: number, overrides: Partial<AnswerCommand> = {}): AnswerCommand {
  return {runId: "answer-smoke", commandId: `answer-${round}`, kind: "answer", round, mode: "when-open",
    delayMs: 20, spreadMs: 0, issuedAtMs: Date.now(), target: {ordinalStart: 1, count: 2}, ...overrides};
}
async function fixture(browser: Browser): Promise<{page: Page; driver: PlaywrightManualDriver}> {
  const page = await browser.newPage();
  const driver = new PlaywrightManualDriver(page, 1000);
  await driver.initialize();
  await page.route("https://127.0.0.1/", route => route.fulfill({contentType: "text/html",
    body: "<main class='participant-live-shell participant-live-shell-answering'></main>"}));
  await page.goto("https://127.0.0.1/");
  return {page, driver};
}
async function display(page: Page, round: number, options: {
  deadlineMs?: number | null; phase?: string; hidden?: string; synchronized?: boolean; stallMs?: number;
} = {}): Promise<void> {
  await page.evaluate(({round, options}) => {
    const shell = document.querySelector("main")!;
    shell.innerHTML = `<section class="participant-question"><b class="participant-round-badge" data-round-number="${round}">${round === 1 ? `練習問題 ${round}` : `第${round - 1}問`}</b></section>`;
    const question = shell.firstElementChild as HTMLElement;
    if (options.hidden === "inert") question.setAttribute("inert", "");
    if (options.hidden === "opacity") question.style.opacity = "0";
    if (options.hidden === "visibility") question.style.visibility = "hidden";
    for (let index = 0; index < 4; index++) {
      const button = document.createElement("button");
      button.className = "answer-button"; button.textContent = String(index);
      button.disabled = options.hidden === "disabled";
      button.onclick = event => {
        sessionStorage.setItem(`click-${round}`, JSON.stringify({option: index, trusted: event.isTrusted, time: performance.now()}));
        // React同様、click処理内で同期的に観測が返っても再入clickしない。
        window.dispatchEvent(new CustomEvent("quiz:observation", {detail: {kind: "answer", phase: "sending", round_number: round}}));
      };
      question.append(button);
    }
    window.dispatchEvent(new CustomEvent("quiz:observation", {detail: {kind: "projection_received", round_number: round, state: "Open"}}));
    sessionStorage.setItem(`display-${round}`, String(performance.now()));
    window.dispatchEvent(new CustomEvent("quiz:observation", {detail: {kind: "participant_display", round_number: round,
      phase: options.phase ?? "answering", synchronized: options.synchronized ?? true, has_question: true,
      ...(options.deadlineMs === null ? {} : {answer_deadline_monotonic_ms: performance.now() + (options.deadlineMs ?? 3000)})}}));
    if (options.stallMs !== undefined) {
      const resumeAt = performance.now() + options.stallMs;
      while (performance.now() < resumeAt) { /* 締切後もenabled DOMを残す描画停止のfixture */ }
    }
  }, {round, options});
}

it.runIf(enabled)("answers locally while Node observation delivery is withheld and spreads isolated contexts", async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const pages = await Promise.all([fixture(browser), fixture(browser)]);
    let received = 0;
    let release!: () => void;
    const held = new Promise<void>(resolve => { release = resolve; });
    for (const {page} of pages) {
      await page.exposeBinding("delayedObservation", async () => { await held; received++; });
      await page.evaluate(() => window.addEventListener("quiz:observation", () => {
        void (window as unknown as {delayedObservation: () => Promise<void>}).delayedObservation();
      }));
    }
    const answers = pages.map(({driver}, index) => driver.answer(command(1, {delayMs: 50, spreadMs: 100}), index + 1, Date.now() + 5000));
    await Promise.all(pages.map(({page}) => display(page, 1)));
    const results = await Promise.all(answers);
    expect(received).toBe(0);
    for (const result of results) {
      expect(result.clicked).toBe(true);
      expect(result.browserMonotonicMs! - result.browserPlannedMonotonicMs!).toBeGreaterThanOrEqual(-1);
      expect(result.browserMonotonicMs! - result.browserPlannedMonotonicMs!).toBeLessThan(500);
    }
    const displayed = await Promise.all(pages.map(({page}) => page.evaluate(() => Number(sessionStorage.getItem("display-1")))));
    for (let index = 0; index < results.length; index++) {
      const delay = results[index]!.browserPlannedMonotonicMs! - displayed[index]!;
      expect(delay).toBeGreaterThanOrEqual(50 + index * 100);
      expect(delay).toBeLessThan(70 + index * 100);
    }
    const clicks = await Promise.all(pages.map(({page}) => page.evaluate(() => JSON.parse(sessionStorage.getItem("click-1")!) as {option: number; trusted: boolean})));
    expect(clicks).toMatchObject([{option: 0, trusted: false}, {option: 1, trusted: false}]);
    expect(await pages[0]!.driver.answer(command(1, {commandId: "duplicate"}), 1, Date.now() + 1000))
      .toMatchObject({clicked: false, reason: "duplicate_operation"});
    release();
  } finally { await browser.close(); }
}, 15000);

it.runIf(enabled)("does not recreate future-round timers for answer observations and still expires them", async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const {page, driver} = await fixture(browser);
    const answer = driver.answer(command(8), 1, Date.now() + 500);
    await page.evaluate(() => undefined);
    const created = await page.evaluate(() => {
      const original = window.setTimeout;
      let count = 0;
      window.setTimeout = new Proxy(original, {
        apply(target, receiver, args: Parameters<typeof window.setTimeout>) {
          count++;
          return Reflect.apply(target, receiver, args);
        },
      });
      try {
        for (let index = 0; index < 100; index++) window.dispatchEvent(new CustomEvent("quiz:observation",
          {detail: {kind: "answer", phase: "sending", round_number: 1}}));
        window.dispatchEvent(new CustomEvent("quiz:observation",
          {detail: {kind: "projection_received", round_number: 1, state: "Open"}}));
        return count;
      } finally { window.setTimeout = original; }
    });
    expect(created).toBe(0);
    expect(await answer).toMatchObject({clicked: false, reason: "run_deadline"});
  } finally { await browser.close(); }
}, 15000);

it.runIf(enabled)("keeps the delay origin through pause, expires reservations, and makes stop sticky", async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const {page, driver} = await fixture(browser);
    await driver.pause(true);
    const first = driver.answer(command(1, {delayMs: 10}), 1, Date.now() + 5000);
    await display(page, 1);
    await page.waitForTimeout(80);
    expect(await page.evaluate(() => sessionStorage.getItem("click-1"))).toBeNull();
    await driver.pause(false);
    const clicked = await first;
    expect(clicked.clicked).toBe(true);
    expect(clicked.browserMonotonicMs! - clicked.browserPlannedMonotonicMs!).toBeGreaterThan(40);
    await driver.pause(true);
    const expired = driver.answer(command(2), 1, Date.now() + 5000);
    await display(page, 2, {deadlineMs: 60});
    expect(await expired).toMatchObject({clicked: false, reason: "answer_deadline_expired"});
    await driver.pause(false);
    expect(await page.evaluate(() => sessionStorage.getItem("click-2"))).toBeNull();
    await display(page, 3);
    const immediate = await driver.answer(command(3, {mode: "now", issuedAtMs: Date.now() - 1000, delayMs: 500}), 1, Date.now() + 5000);
    expect(immediate.clicked).toBe(true);
    expect(immediate.browserPlannedMonotonicMs).toBeUndefined();
    const stopped = driver.answer(command(4), 1, Date.now() + 5000);
    await driver.pause(true, true);
    expect(await stopped).toMatchObject({clicked: false, reason: "run_stopped"});
    await driver.pause(false);
    await display(page, 4);
    expect(await page.evaluate(() => sessionStorage.getItem("click-4"))).toBeNull();
  } finally { await browser.close(); }
}, 10000);

it.runIf(enabled)("does not reserve now before opening, carry old rounds forward, or wait beyond the run", async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const {page, driver} = await fixture(browser);
    expect(await driver.answer(command(1, {mode: "now"}), 1, Date.now() + 5000))
      .toMatchObject({clicked: false, reason: "round_not_answerable"});
    const old = driver.answer(command(1, {commandId: "reserved-1"}), 1, Date.now() + 5000);
    await display(page, 2);
    expect(await old).toMatchObject({clicked: false, reason: "round_expired"});
    const closing = driver.answer(command(3), 1, Date.now() + 5000);
    await display(page, 3, {phase: "collecting"});
    expect(await closing).toMatchObject({clicked: false, reason: "round_expired"});
    expect(await driver.answer(command(4), 1, Date.now() + 50)).toMatchObject({clicked: false, reason: "run_deadline"});
  } finally { await browser.close(); }
}, 10000);

it.runIf(enabled)("blocks unavailable deadlines and invisible, inert, or disabled answer DOM", async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const options of [{deadlineMs: null}, {deadlineMs: -1}, {deadlineMs: 10}, {deadlineMs: 30, stallMs: 80}, {hidden: "inert"}, {hidden: "opacity"},
      {hidden: "visibility"}, {hidden: "disabled"}]) {
      const {page, driver} = await fixture(browser);
      const answer = driver.answer(command(1), 1, Date.now() + 5000);
      await display(page, 1, options);
      expect((await answer).clicked).toBe(false);
      expect(await page.evaluate(() => sessionStorage.getItem("click-1"))).toBeNull();
      await page.context().close();
    }
  } finally { await browser.close(); }
}, 15000);

it.runIf(enabled)("waits without an AWS run timer but keeps answer deadlines and explicit stop effective", async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const {page, driver} = await fixture(browser);
    const first = driver.answer(command(1), 1, null);
    await page.waitForTimeout(80);
    await display(page, 1);
    expect((await first).clicked).toBe(true);
    await driver.pause(true);
    const expired = driver.answer(command(2), 1, null);
    await display(page, 2, {deadlineMs: 60});
    expect(await expired).toMatchObject({clicked: false, reason: "answer_deadline_expired"});
    await driver.pause(false);
    const stopped = driver.answer(command(3), 1, null);
    await driver.pause(true, true);
    expect(await stopped).toMatchObject({clicked: false, reason: "run_stopped"});
    await driver.pause(false);
    await display(page, 3);
    expect(await page.evaluate(() => sessionStorage.getItem("click-3"))).toBeNull();
  } finally { await browser.close(); }
}, 10000);

it.runIf(enabled)("matches actual answer and result rounds when official numbering restarts at one", async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const {page, driver} = await fixture(browser);
    for (let round = 1; round <= 2; round++) {
      await display(page, round);
      expect((await driver.answer(command(round, {mode: "now"}), 1, Date.now() + 5000)).clicked).toBe(true);
    }
    expect(await page.locator(".participant-round-badge").textContent()).toBe("第1問");
    await page.evaluate(() => {
      document.querySelector("main")!.innerHTML = '<section class="participant-result-stage"><b class="participant-result-round" data-round-number="2">第1問</b><span class="participant-score-result">fixture score</span></section>';
    });
    expect(await driver.observeResult(2, 1000)).toEqual({unanswered: false});
    expect(await driver.observeResult(1, 1000)).toBeNull();
    await page.locator(".participant-score-result").evaluate(element => element.classList.add("participant-result-pending"));
    expect(await driver.observeResult(2, 1000)).toBeNull();
    await page.locator(".participant-score-result").evaluate(element => element.classList.remove("participant-result-pending"));
    expect(await driver.observeResult(2, 1000)).toEqual({unanswered: false});
    await page.locator(".participant-result-round").evaluate(element => element.removeAttribute("data-round-number"));
    expect(await driver.observeResult(1, 1000)).toBeNull();
  } finally { await browser.close(); }
}, 10000);

it.runIf(enabled)("rejects answer DOM with missing or mismatched actual round metadata", async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const badgeRound of [null, "2"]) {
      const {page, driver} = await fixture(browser);
      await display(page, 3);
      await page.locator(".participant-round-badge").evaluate((element, badgeRound) => {
        element.textContent = "第3問";
        if (badgeRound === null) element.removeAttribute("data-round-number");
        else element.setAttribute("data-round-number", badgeRound);
      }, badgeRound);
      expect(await driver.answer(command(3, {mode: "now"}), 1, Date.now() + 5000))
        .toMatchObject({clicked: false, reason: "answer_dom_unavailable"});
      expect(await page.evaluate(() => sessionStorage.getItem("click-3"))).toBeNull();
      await page.context().close();
    }
  } finally { await browser.close(); }
}, 10000);
