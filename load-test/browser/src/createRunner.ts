import { execFile } from "node:child_process";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { parseArgs, promisify } from "node:util";
import { z } from "zod";
import { createRunnerPreset, parseRunnerEnvironment } from "./runnerPreset.js";
import { superviseControl } from "./controlSupervisor.js";
import { expectedRoundsSchema } from "./controlRounds.js";

const execute = promisify(execFile);
const completionSchema = z.object({
  format: z.literal("browser-control-report-v1"),
  stopReason: z.enum(["requested_stop", "signal", "input_closed", "setup_timeout", "run_deadline",
    "worker_ipc_unavailable", "worker_ipc_failed", "worker_spawn_failed", "worker_unexpected_exit",
    "worker_invalid_message", "worker_failed", "worker_invalid_counts", "worker_heartbeat_timeout",
    "supervisor_failed", "worker_cleanup_failed", "recording_failed", "unknown"]).catch("unknown"),
  normalTermination: z.boolean(), plannedVus: z.number().int().positive(),
  prepared: z.number().int().nonnegative(), registered: z.number().int().nonnegative(),
  failedParticipants: z.number().int().nonnegative(), timeouts: z.number().int().nonnegative(),
  measurementComplete: z.boolean(), expectedRounds: expectedRoundsSchema, allRoundsBrowserComplete: z.boolean(),
});

export async function printRunnerCompletion(directory: string, retainResults = true): Promise<void> {
  let report: z.infer<typeof completionSchema>;
  try {
    const input: unknown = JSON.parse(await readFile(resolve(directory, "report.json"), "utf8"));
    report = completionSchema.parse(input);
  } catch {
    process.exitCode = 1;
    process.stderr.write(`Runner 異常終了: browser_runner_report_unavailable; 終了結果を確認できません。\n` +
      (retainResults ? `証跡: ${directory}\n` : "一時記録は保存しません。\n"));
    return;
  }
  const failed = !report.normalTermination || Number(process.exitCode ?? 0) !== 0;
  if (failed) process.exitCode = 1;
  const output = failed ? process.stderr : process.stdout;
  output.write(`Runner ${failed ? "異常終了" : "停止"}: ${report.stopReason}\n` +
    `準備 ${report.prepared}/${report.plannedVus} / 登録 ${report.registered}/${report.plannedVus} / ` +
    `失敗 ${report.failedParticipants} / timeout ${report.timeouts}\n` +
    `計測記録: ${report.measurementComplete ? "完全" : "不完全"} / 予定全${report.expectedRounds}問: ${report.allRoundsBrowserComplete ? "完了" : "未完了"}\n` +
    (retainResults ? `report.json / rounds.csv: ${directory}\n` : "一時集計のみ（停止後に削除）\n"));
}

async function main(): Promise<void> {
  const {values} = parseArgs({options: {start: {type: "boolean", default: false}}});
  const options = parseRunnerEnvironment(process.env);
  const gitOptions = {timeout: 30000, maxBuffer: 1024 * 1024};
  const root = (await execute("git", ["rev-parse", "--show-toplevel"], gitOptions)).stdout.trim();
  const state = await execute("git", ["status", "--porcelain=v1", "--untracked-files=normal"], gitOptions);
  if (state.stdout.trim() !== "") throw new Error("browser_source_dirty");
  const head = (await execute("git", ["rev-parse", "HEAD"], gitOptions)).stdout.trim();
  const settings = await createRunnerPreset(root, head, options);
  const config = resolve(settings.run.outputDirectory, "run.json");
  process.stdout.write(`新規Runner: ${options.vus} VU / ${options.durationSeconds}秒 / ${options.measurementLevel}\n設定: ${config}\n`);
  if (values.start) {
    const directory = await superviseControl(settings, options.vus);
    await printRunnerCompletion(directory);
  } else {
    const quotedConfig = "'" + config.replaceAll("'", "'\\''") + "'";
    process.stdout.write(`起動: pnpm --dir load-test/browser control --vus ${options.vus} --config ${quotedConfig}\n`);
  }
}

if (process.argv[1]?.endsWith("createRunner.js")) void main().catch((error: unknown) => {
  if (error instanceof z.ZodError) {
    process.stderr.write(`browser_runner_invalid_settings; 設定範囲を確認してください: ${error.issues.map(issue => issue.path.join(".")).join(", ")}\n`);
  } else if (error instanceof Error && error.message === "browser_source_dirty") {
    process.stderr.write("browser_source_dirty; アプリとRunnerの版を記録するため、変更をcommitしてから実行してください。\n");
  } else {
    process.stderr.write("browser_runner_failed; 設定・依存・保存先・非秘密の証跡を確認してください。\n");
  }
  process.exitCode = 1;
});
