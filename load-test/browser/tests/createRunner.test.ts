import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { printRunnerCompletion } from "../src/createRunner.js";

let directory: string;
let previousExitCode: typeof process.exitCode;
const report = {
  format: "browser-control-report-v1", stopReason: "worker_failed", normalTermination: false,
  plannedVus: 300, prepared: 30, registered: 0, failedParticipants: 10, timeouts: 10,
  measurementComplete: false, expectedRounds: 6, allRoundsBrowserComplete: false,
};

beforeEach(async () => {
  directory = await mkdtemp(join(tmpdir(), "browser-runner-completion-"));
  previousExitCode = process.exitCode;
  process.exitCode = undefined;
  vi.spyOn(process.stdout, "write").mockImplementation(() => true);
  vi.spyOn(process.stderr, "write").mockImplementation(() => true);
});

afterEach(async () => {
  process.exitCode = previousExitCode;
  vi.restoreAllMocks();
  await rm(directory, {recursive: true, force: true});
});

it("reports setup failure and its fixed denominator instead of claiming completion", async () => {
  await writeFile(join(directory, "report.json"), JSON.stringify(report));
  await printRunnerCompletion(directory);
  expect(process.stderr.write).toHaveBeenCalledWith(
    `Runner 異常終了: worker_failed\n準備 30/300 / 登録 0/300 / 失敗 10 / timeout 10\n` +
    `計測記録: 不完全 / 予定全6問: 未完了\nreport.json / rounds.csv: ${directory}\n`);
  expect(process.stdout.write).not.toHaveBeenCalled();
  expect(process.exitCode).toBe(1);
});

it.each([false, true])("distinguishes a normal stop from six-round completion (%s)", async complete => {
  await writeFile(join(directory, "report.json"), JSON.stringify({...report, stopReason: "requested_stop",
    normalTermination: true, prepared: 300, registered: 300, failedParticipants: 0, timeouts: 0,
    measurementComplete: true, allRoundsBrowserComplete: complete}));
  await printRunnerCompletion(directory);
  expect(process.stdout.write).toHaveBeenCalledWith(expect.stringContaining("Runner 停止: requested_stop"));
  expect(process.stdout.write).toHaveBeenCalledWith(expect.stringContaining(`予定全6問: ${complete ? "完了" : "未完了"}`));
  expect(process.stderr.write).not.toHaveBeenCalled();
  expect(process.exitCode).toBeUndefined();
});

it.each([7, 10])("prints the planned %i rounds without claiming a six-round run", async expectedRounds => {
  await writeFile(join(directory, "report.json"), JSON.stringify({...report, expectedRounds,
    stopReason: "requested_stop", normalTermination: true, measurementComplete: true, allRoundsBrowserComplete: true}));
  await printRunnerCompletion(directory);
  expect(process.stdout.write).toHaveBeenCalledWith(expect.stringContaining(`予定全${expectedRounds}問: 完了`));
});

it("preserves a supervisor failure even when the report records a requested stop", async () => {
  await writeFile(join(directory, "report.json"), JSON.stringify({...report,
    stopReason: "requested_stop", normalTermination: true}));
  process.exitCode = 1;
  await printRunnerCompletion(directory);
  expect(process.stderr.write).toHaveBeenCalledWith(expect.stringContaining("Runner 異常終了: requested_stop"));
  expect(process.exitCode).toBe(1);
});

it("does not print an unrecognized stop reason or unrelated report fields", async () => {
  await writeFile(join(directory, "report.json"), JSON.stringify({...report,
    stopReason: "private-value\nunsafe-output", detail: "private-value"}));
  await printRunnerCompletion(directory);
  expect(process.stderr.write).toHaveBeenCalledWith(expect.stringContaining("Runner 異常終了: unknown"));
  expect(process.stderr.write).not.toHaveBeenCalledWith(expect.stringContaining("private-value"));
});

it.each(["missing", "invalid_json", "invalid_summary"])("reports %s evidence without exposing input", async mode => {
  if (mode !== "missing") await writeFile(join(directory, "report.json"), mode === "invalid_json" ? "private-value" :
    JSON.stringify({...report, prepared: "private-value"}));
  await printRunnerCompletion(directory);
  expect(process.stderr.write).toHaveBeenCalledWith(
    `Runner 異常終了: browser_runner_report_unavailable; 終了結果を確認できません。\n証跡: ${directory}\n`);
  expect(process.stdout.write).not.toHaveBeenCalled();
  expect(process.exitCode).toBe(1);
});
