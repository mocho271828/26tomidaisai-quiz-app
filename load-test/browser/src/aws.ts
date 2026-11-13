import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { z } from "zod";
import { createAwsRunnerPreset, parseAwsEnvironment, removeAwsRun, type AwsSource } from "./awsPreset.js";
import { configSchema } from "./config.js";
import { printRunnerCompletion } from "./createRunner.js";
import { superviseControl } from "./controlSupervisor.js";

const execute = promisify(execFile);

export async function readAwsSource(cwd = process.cwd()): Promise<AwsSource> {
  const options = {cwd, timeout: 30000, maxBuffer: 1024 * 1024};
  const root = z.string().min(1).parse((await execute("git", ["rev-parse", "--show-toplevel"], options)).stdout.trim());
  const gitOptions = {...options, cwd: root};
  const state = await execute("git", ["status", "--porcelain=v1", "--untracked-files=normal"], gitOptions);
  const head = configSchema.shape.sourceCommit.parse((await execute("git", ["rev-parse", "HEAD"], gitOptions)).stdout.trim());
  return {root, head, dirty: state.stdout.trim().length > 0};
}

export function validateAwsRuntime(environment: NodeJS.ProcessEnv, platform = process.platform): void {
  if (platform !== "darwin" && platform !== "linux") throw new Error("aws_browser_platform_unsupported");
  if ((environment.NODE_TLS_REJECT_UNAUTHORIZED !== undefined && environment.NODE_TLS_REJECT_UNAUTHORIZED !== "1") ||
      (environment.NODE_OPTIONS?.trim() ?? "") !== "") throw new Error("aws_browser_tls_environment_rejected");
}

export type AwsDependencies = {
  source: typeof readAwsSource;
  supervise: typeof superviseControl;
  completion: typeof printRunnerCompletion;
};

export async function runAwsRunner(environment: NodeJS.ProcessEnv,
  dependencies: AwsDependencies = {source: readAwsSource, supervise: superviseControl, completion: printRunnerCompletion}): Promise<void> {
  validateAwsRuntime(environment);
  const options = parseAwsEnvironment(environment);
  const source = await dependencies.source();
  const settings = await createAwsRunnerPreset(source, options);
  process.stdout.write(`AWS Browser Runner: ${options.vus} VU / 予定${options.expectedRounds}問 / stopまで継続 / 結果保存 ${options.recordResults ? "あり" : "なし"}\n` +
    `対象: ${settings.run.baseUrl}\n試験: ${settings.run.runId}\n` +
    "このPCのVUだけを操作します。join / status / answer / pause / resume / stopを使用できます。\n");
  try {
    const directory = await dependencies.supervise(settings, options.vus);
    await dependencies.completion(directory, options.recordResults);
  } finally {
    // falseも計測と異常検知は同じ経路を通し、今回排他作成したrunだけを削除する。
    if (!options.recordResults) {
      await removeAwsRun(settings.run.outputDirectory);
      process.stdout.write("RECORD=false: 今回の一時記録と匿名照合鍵を削除しました。\n");
    }
  }
}

export function awsFailureCode(error: unknown): string {
  const codes = new Set(["aws_browser_directory_permissions_invalid",
    "aws_browser_platform_unsupported", "aws_browser_tls_environment_rejected", "aws_browser_uses_manual_stop", "aws_browser_cleanup_failed"]);
  if (error instanceof Error && codes.has(error.message)) return error.message;
  if (error instanceof z.ZodError) return "aws_browser_settings_invalid";
  return "aws_browser_configuration_or_runtime_failed";
}

if (process.argv[1]?.endsWith("aws.js")) void runAwsRunner(process.env).catch((error: unknown) => {
  process.stderr.write(`${awsFailureCode(error)}; 手順書の設定・Git版・保存先・権限を確認してください。秘密値・例外本文は表示しません。\n`);
  process.exitCode = 1;
});
