import { spawn, execFile } from "node:child_process";
import { once } from "node:events";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";
import { expect, it } from "vitest";

const execute = promisify(execFile);
const enabled = process.env.RUN_BROWSER_MANUAL_SMOKE === "1";
type ProcessEntry = {pid: number; parent: number; group: number; state: string};

async function processes(): Promise<ProcessEntry[]> {
  const {stdout} = await execute("ps", ["-axo", "pid=,ppid=,pgid=,stat="]);
  return stdout.trim().split("\n").map(line => {
    const [pid, parent, group, state] = line.trim().split(/\s+/u);
    if (!pid || !parent || !group || !state) throw new Error("invalid_process_snapshot");
    return {pid: Number(pid), parent: Number(parent), group: Number(group), state};
  });
}

function descendants(entries: ProcessEntry[], parent: number): ProcessEntry[] {
  const found = new Set([parent]);
  const output: ProcessEntry[] = [];
  for (let pass = 0; pass < entries.length; pass++) {
    const children = entries.filter(entry => found.has(entry.parent) && !found.has(entry.pid));
    if (children.length === 0) break;
    for (const child of children) { found.add(child.pid); output.push(child); }
  }
  return output;
}

function killOwnedGroup(group: number): void {
  try { process.kill(-group, "SIGKILL"); }
  catch (error: unknown) { if (!(error instanceof Error && "code" in error && error.code === "ESRCH")) throw error; }
}

// このテストが起動したworkerとブラウザ子孫だけを強制終了し、実アプリへ接続しない。
it.runIf(enabled)("reclaims Chromium descendants after an isolated worker is killed", async () => {
  const worker = spawn(process.execPath, ["--input-type=module", "-e", `
    import {chromium} from "playwright";
    const browser = await chromium.launch({headless: true});
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.setContent("<!doctype html><p>isolated crash fixture</p>");
    process.stdout.write("ready\\n");
    setInterval(() => {}, 1000);
  `], {cwd: fileURLToPath(new URL("../", import.meta.url)), stdio: ["ignore", "pipe", "pipe"]});
  const exited = once(worker, "exit");
  let children: ProcessEntry[] = [];
  try {
    const ready = once(worker.stdout, "data");
    const first = await Promise.race([ready.then(() => "ready"), exited.then(() => "exited")]);
    expect(first).toBe("ready");
    if (worker.pid === undefined) throw new Error("worker_pid_missing");
    children = descendants(await processes(), worker.pid);
    expect(children.length).toBeGreaterThan(0);
    worker.kill("SIGKILL");
    await exited;
    const owned = new Set(children.map(child => child.pid));
    await expect.poll(async () => (await processes()).filter(entry => owned.has(entry.pid) && !entry.state.includes("Z")),
      {timeout: 8000, interval: 100}).toEqual([]);
  } finally {
    worker.kill("SIGKILL");
    const active = await processes();
    const groups = new Set(children.filter(entry => entry.pid === entry.group).map(entry => entry.group));
    for (const group of groups) {
      if (!active.some(entry => entry.group === group && !entry.state.includes("Z"))) continue;
      killOwnedGroup(group);
    }
    await exited;
  }
}, 15000);
