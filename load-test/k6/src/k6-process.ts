import { spawn } from "node:child_process";
import type { ChildProcess, SpawnOptions } from "node:child_process";

export const K6_COMMAND = "k6";
export const K6_FIXED_RUN_ARGUMENTS = [
  "run",
  "--quiet",
  "--summary-mode=full",
  "--no-color",
  "--no-usage-report",
  "--log-output=stdout",
  "--log-format=raw",
] as const;

export interface K6SpawnOptions {
  readonly cwd: string;
  readonly env: NodeJS.ProcessEnv;
  readonly shell: false;
  readonly stdio: readonly ["ignore", "pipe", "ignore"];
  readonly windowsHide: true;
}

export function spawnK6Process(
  command: string,
  arguments_: readonly string[],
  options: K6SpawnOptions,
): ChildProcess {
  const spawnOptions: SpawnOptions = {
    cwd: options.cwd,
    env: options.env,
    shell: false,
    stdio: ["ignore", "pipe", "ignore"],
    windowsHide: true,
  };
  return spawn(command, [...arguments_], spawnOptions);
}
