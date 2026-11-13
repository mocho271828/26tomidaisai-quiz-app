import { mkdtemp, mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { chromium } from "playwright";
import type { Browser } from "playwright";
import { expect, it } from "vitest";
import { z } from "zod";

import { configSchema } from "../src/config.js";
import { Evidence, startResourceSampling } from "../src/evidence.js";
import { createActor, verifyIsolation, type Actor } from "../src/participant.js";

// 明示実行のみに限定する。これは独立BrowserContextの検証であり、TLS・React・DB試験ではない。
it.runIf(process.env.RUN_BROWSER_CONTEXT_SMOKE === "1")(
  "維持中の各BrowserContextでCookie/localStorage/sessionStorageが独立する",
  async () => {
    const count = z.coerce.number().int().min(1).max(100).parse(process.env.BROWSER_CONTEXT_SMOKE_COUNT ?? "100");
    const executablePath = z.string().startsWith("/").optional().parse(process.env.BROWSER_CONTEXT_SMOKE_EXECUTABLE);
    const parent = process.env.BROWSER_CONTEXT_SMOKE_OUTPUT
      ? resolve(process.env.BROWSER_CONTEXT_SMOKE_OUTPUT)
      : await mkdtemp(join(tmpdir(), "quiz-context-smoke-"));
    await mkdir(parent, { recursive: true, mode: 0o700 });
    const config = configSchema.parse({
      runId: `context-isolation-${Date.now()}`, sourceCommit: "0000000000000000000000000000000000000000",
      baseUrl: "https://127.0.0.1", count, ordinalStart: 1, startAt: new Date().toISOString(),
      durationSeconds: 180, joinSpreadMs: 0, operatorStartDelaySeconds: 10,
      preScheduleMarginMs: 1000, resultHoldMs: 3000, answerDelayMs: 0,
      scenario: "normal", faultAfterSeconds: 5, faultDurationMs: 100, outputDirectory: parent,
      channel: "chromium", headless: true,
    });
    const evidence = new Evidence(config, "context-smoke");
    const stopSampling = startResourceSampling(evidence);
    const actors: Actor[] = [];
    let browser: Browser | undefined;
    let failureStage = "launch";
    const started = performance.now();
    try {
      browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
      failureStage = "create_contexts";
      for (let serial = 1; serial <= count; serial += 1) {
        const actor = await createActor(browser, serial, config, evidence);
        actors.push(actor);
        // ネットワークを使わない固定fixtureでstorage originを与える。証明書検証を回避したTLS成功とは扱わない。
        await actor.page.route("https://127.0.0.1/", route => route.fulfill({
          status: 200, contentType: "text/html", body: "<!doctype html><title>Context isolation fixture</title><main>fixture</main>",
        }));
        await actor.page.goto(config.baseUrl);
        const initial = await actor.page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length }));
        expect(initial).toEqual({ local: 0, session: 0 });
        expect(await actor.context.cookies()).toEqual([]);
        await actor.context.addCookies([{ name: "synthetic_context_cookie", value: String(serial),
          domain: "127.0.0.1", path: "/", secure: true, httpOnly: true, sameSite: "Strict" }]);
        await actor.page.evaluate(value => {
          localStorage.setItem("synthetic_context_key", String(value));
          sessionStorage.setItem("synthetic_context_key", String(value));
        }, serial);
      }
      failureStage = "verify_isolation";
      const peakOpenContexts = browser.contexts().length;
      const peakOpenPages = actors.filter(actor => !actor.page.isClosed()).length;
      expect(peakOpenContexts).toBe(count);
      expect(peakOpenPages).toBe(count);
      const isolatedCookies = await verifyIsolation(actors, evidence);
      expect(isolatedCookies).toBe(true);
      let matchingStorage = 0;
      for (const actor of actors) {
        const isolated = await actor.page.evaluate(value =>
          localStorage.getItem("synthetic_context_key") === String(value) &&
          sessionStorage.getItem("synthetic_context_key") === String(value), actor.serial);
        if (isolated) matchingStorage += 1;
      }
      expect(matchingStorage).toBe(count);
      await stopSampling();
      evidence.save("summary.json", {
        status: "passed", requestedContexts: count, maintainedContexts: peakOpenContexts,
        maintainedPages: peakOpenPages, isolatedCookies, matchingStorage,
        elapsedMs: performance.now() - started, playwrightVersion: "1.63.0",
        browserVersion: browser.version(), usesAlternateExecutable: executablePath !== undefined,
        evidenceWriteMs: evidence.overhead(), source: "working tree; not a committed application image",
        scope: "actual concurrent BrowserContexts and isolated storage only; intercepted fixed HTML; no TLS handshake, React, registration, WS, PostgreSQL, Redis or quiz progression",
      });
    } catch {
      // Playwrightのraw例外はpathや環境を含むので、共有可能な固定段階だけ記録する。
      await stopSampling();
      await writeFile(join(evidence.directory, "failure.json"), JSON.stringify({
        status: "failed", failureStage, requestedContexts: count, createdContexts: actors.length,
        usesAlternateExecutable: executablePath !== undefined,
        elapsedMs: performance.now() - started,
        scope: "BrowserContext independence smoke only; not application/TLS acceptance",
      }) + "\n", { mode: 0o600, flag: "wx" });
      throw new Error(`browser_context_smoke_failed:${failureStage}`);
    } finally {
      await browser?.close();
      await stopSampling();
      await evidence.close();
    }
  },
  180000,
);
