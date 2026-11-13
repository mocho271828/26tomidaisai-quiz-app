import { readFile, lstat } from "node:fs/promises";
import { z } from "zod";
import type { Browser } from "playwright";
import type { RunConfig } from "./config.js";
import { Evidence } from "./evidence.js";
import { observeOperatorMutations } from "./operatorDiagnostics.js";
import { awaitCohortReady, requireCohortAlive } from "./coordinator.js";
import { cancellableDelay, type RunLifecycle } from "./lifecycle.js";

const credentialSchema = z.object({username: z.string().min(1), password: z.string().min(1)}).strict();

export async function runOperator(browser: Browser, config: RunConfig, evidence: Evidence,
  credentialPath: string, lifecycle: RunLifecycle): Promise<void> {
  const host = new URL(config.baseUrl).hostname;
  if (host !== "localhost" && host !== "127.0.0.1") throw new Error("operator_requires_loopback");
  const stat = await lstat(credentialPath);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1 ||
      (stat.mode & 0o777) !== 0o600 || stat.uid !== process.getuid?.())
    throw new Error("operator_credentials_require_owned_0600_file");
  const input: unknown = JSON.parse(await readFile(credentialPath, "utf8"));
  const credentials = credentialSchema.parse(input);
  const context = await browser.newContext();
  let stopObserving: (() => Promise<void>) | undefined;
  const watcher = new AbortController();
  let monitoring: Promise<void> = Promise.resolve();
  const closeOnAbort = (): void => { void context.close().catch(() => undefined); };
  lifecycle.signal.addEventListener("abort", closeOnAbort, {once: true});
  try {
    lifecycle.signal.throwIfAborted();
    const page = await context.newPage();
    stopObserving = observeOperatorMutations(page, evidence);
    page.setDefaultTimeout(30000);
    await page.goto(`${config.baseUrl}/operator`, {waitUntil: "domcontentloaded"});
    await page.getByLabel("ユーザー名", {exact: true}).fill(credentials.username);
    await page.getByLabel("パスワード", {exact: true}).fill(credentials.password);
    await page.getByRole("button", {name: "ログイン", exact: true}).click();
    await page.getByRole("region", {name: "現在の問題と進行", exact: true}).waitFor();
    await cancellableDelay(Date.parse(config.startAt) + config.operatorStartDelaySeconds * 1000 - Date.now(), lifecycle.signal);
    if (config.coordination !== undefined) await awaitCohortReady(config, lifecycle.signal);
    lifecycle.startMeasurement();
    monitoring = (async () => {
      try {
        while (!watcher.signal.aborted && !lifecycle.signal.aborted) {
          if (config.coordination !== undefined) await requireCohortAlive(config);
          await cancellableDelay(250, watcher.signal);
        }
      } catch {
        if (!watcher.signal.aborted && !lifecycle.signal.aborted) {
          evidence.event({event: "operator_cohort_unavailable", phase: "measurement"});
          lifecycle.finish("participant_unavailable");
        }
      }
    })();
    for (let round = 1; round <= 6; round++) {
      lifecycle.signal.throwIfAborted();
      if (config.coordination !== undefined) await requireCohortAlive(config);
      await page.getByRole("button", {name: "問題を確定して配信", exact: true}).click();
      evidence.event({event: "operator_prepared", round});
      await cancellableDelay(config.preScheduleMarginMs, lifecycle.signal);
      if (config.coordination !== undefined) await requireCohortAlive(config);
      await page.getByRole("button", {name: "3秒後に開始予約", exact: true}).click();
      evidence.event({event: "operator_scheduled", round});
      await page.getByRole("button", {name: "正解・結果を公開", exact: true}).click({timeout: 45000});
      evidence.event({event: "operator_published", round});
      await page.getByRole("heading", {name: "公開済み", exact: true}).waitFor({timeout: 45000});
      evidence.event({event: "operator_reveal", round});
      // 次問へ進む前にも結果を表示する余裕を取る。
      await cancellableDelay(config.resultHoldMs, lifecycle.signal);
      if (round < 6) {
        if (config.coordination !== undefined) await requireCohortAlive(config);
        await page.getByRole("button", {name: /^backupを検証して登録問題 \d+へ進む$/}).click();
        await page.getByRole("button", {name: "問題を確定して配信", exact: true}).waitFor();
      }
    }
    // 最終結果の証跡を取れるようEvent完了は人が行う。
    evidence.event({event: "operator_six_rounds_complete"});
  } finally {
    watcher.abort();
    await monitoring;
    lifecycle.signal.removeEventListener("abort", closeOnAbort);
    await stopObserving?.();
    await context.close();
  }
}
