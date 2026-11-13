import { execFile, type ChildProcess } from "node:child_process";
import { promisify } from "node:util";
const execute = promisify(execFile);

/** workerが応答しないときもChromium子孫を回収し、強制終了は計測不完全として残す。 */
export async function terminateWorkerTree(child: ChildProcess): Promise<void> {
  if (child.pid === undefined || child.exitCode !== null || child.signalCode !== null) return;
  const {stdout} = await execute("ps", ["-A", "-o", "pid=,ppid="], {timeout: 6000, maxBuffer: 1024 * 1024});
  const rows = stdout.trim().split("\n").map(line => line.trim().split(/\s+/u).map(Number));
  const descendants = new Set([child.pid]);
  for (let prior = 0; prior !== descendants.size;) {
    prior = descendants.size;
    for (const [pid, parent] of rows) if (pid !== undefined && parent !== undefined && descendants.has(parent)) descendants.add(pid);
  }
  for (const pid of [...descendants].reverse()) {
    try { process.kill(pid, "SIGKILL"); }
    catch (error: unknown) {
      if (!(error instanceof Error && "code" in error && error.code === "ESRCH")) throw new Error("worker_tree_cleanup_failed");
    }
  }
}
