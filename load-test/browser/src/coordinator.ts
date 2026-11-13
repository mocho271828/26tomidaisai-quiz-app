import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { z } from "zod";
import type { RunConfig } from "./config.js";
import { cancellableDelay } from "./lifecycle.js";

const LEASE_TIMEOUT_MS = 10_000;
const HEARTBEAT_MS = 1_000;
const leaseSchema = z.object({
  version: z.literal(1), runId: z.string(), sourceCommit: z.string(), startAt: z.string(),
  ordinalStart: z.number().int().positive(), count: z.number().int().positive(),
  phase: z.enum(["setup", "measurement", "completed", "stopped"]),
  ready: z.boolean(), sequence: z.number().int().nonnegative(),
}).strict();
type Lease = z.infer<typeof leaseSchema>;
export type CohortStatus = "waiting" | "ready" | "unavailable";

function settings(config: RunConfig) {
  return config.coordination ?? {directory: resolve(config.outputDirectory, ".coordination"),
    cohorts: [{ordinalStart: config.ordinalStart, count: config.count}]};
}

function directory(config: RunConfig): string {
  return resolve(settings(config).directory, config.runId);
}

export function inspectLease(input: unknown, config: RunConfig,
  cohort: {ordinalStart: number; count: number}): CohortStatus {
  const parsed = leaseSchema.safeParse(input);
  if (!parsed.success) return "unavailable";
  const lease = parsed.data;
  if (lease.runId !== config.runId || lease.sourceCommit !== config.sourceCommit ||
      lease.startAt !== config.startAt || lease.ordinalStart !== cohort.ordinalStart ||
      lease.count !== cohort.count || lease.phase === "stopped")
    return "unavailable";
  return (lease.phase === "measurement" || lease.phase === "completed") && lease.ready ? "ready" : "waiting";
}

export class LeaseMonitor {
  private readonly seen = new Map<number, {sequence: number; changedAt: number; progressing: boolean}>();

  observe(input: unknown, config: RunConfig, cohort: {ordinalStart: number; count: number},
    now = performance.now()): CohortStatus {
    const status = inspectLease(input, config, cohort);
    if (status === "unavailable") return status;
    const lease = leaseSchema.parse(input);
    const prior = this.seen.get(cohort.ordinalStart);
    if (prior === undefined) {
      this.seen.set(cohort.ordinalStart, {sequence: lease.sequence, changedAt: now, progressing: false});
      return "waiting";
    }
    if (lease.sequence < prior.sequence) return "unavailable";
    if (lease.sequence > prior.sequence) {
      prior.sequence = lease.sequence;
      prior.changedAt = now;
      prior.progressing = true;
    }
    // 他PCの壁時計を引かず、更新が進むことを生成機側の単調時計で確認する。
    if (lease.phase !== "completed" && now - prior.changedAt > LEASE_TIMEOUT_MS) return "unavailable";
    return prior.progressing ? status : "waiting";
  }
}

const monitors = new WeakMap<RunConfig, LeaseMonitor>();

export class ParticipantCoordinator {
  private timer: ReturnType<typeof setTimeout> | undefined;
  private pending: Promise<void> = Promise.resolve();
  private stopped = false;
  private started = false;
  private phase: Lease["phase"] = "setup";
  private ready = false;
  private sequence = 0;
  private readonly path: string;

  constructor(private readonly config: RunConfig, private readonly onFailure: () => void) {
    this.path = resolve(directory(config), `participants-${config.ordinalStart}.json`);
  }

  async start(): Promise<void> {
    if (!settings(this.config).cohorts.some(cohort => cohort.ordinalStart === this.config.ordinalStart &&
        cohort.count === this.config.count)) throw new Error("coordinator_cohort_missing");
    await mkdir(directory(this.config), {recursive: true, mode: 0o700});
    // 同一runの旧leaseを再利用すると、別試験の準備完了を誤認するため拒否する。
    await writeFile(this.path, JSON.stringify(this.snapshot()), {flag: "wx", mode: 0o600});
    this.started = true;
    this.schedule();
  }

  measurementReady(ready: boolean): void { this.phase = "measurement"; this.ready = ready; }

  private snapshot(): Lease {
    return {version: 1, runId: this.config.runId, sourceCommit: this.config.sourceCommit,
      startAt: this.config.startAt, ordinalStart: this.config.ordinalStart, count: this.config.count,
      phase: this.phase, ready: this.ready, sequence: this.sequence};
  }

  private async publish(): Promise<void> {
    this.sequence += 1;
    const temporary = `${this.path}.${process.pid}.tmp`;
    await writeFile(temporary, JSON.stringify(this.snapshot()), {mode: 0o600});
    await rename(temporary, this.path);
  }

  private schedule(): void {
    this.timer = setTimeout(() => {
      this.pending = this.publish().catch(() => this.onFailure()).finally(() => {
        if (!this.stopped) this.schedule();
      });
    }, HEARTBEAT_MS);
  }

  async stop(completed = false): Promise<void> {
    if (!this.started) return;
    this.stopped = true;
    clearTimeout(this.timer);
    await this.pending;
    this.phase = completed ? "completed" : "stopped";
    this.ready = completed;
    await this.publish();
  }
}

export async function cohortStatus(config: RunConfig, now = performance.now()): Promise<CohortStatus> {
  let monitor = monitors.get(config);
  if (monitor === undefined) { monitor = new LeaseMonitor(); monitors.set(config, monitor); }
  const observer = monitor;
  const statuses = await Promise.all(settings(config).cohorts.map(async cohort => {
    try {
      const input: unknown = JSON.parse(await readFile(resolve(directory(config),
        `participants-${cohort.ordinalStart}.json`), "utf8"));
      return observer.observe(input, config, cohort, now);
    } catch (error: unknown) {
      // 起動前の未作成だけを待機とし、壊れたJSONや読取り失敗をreadyにしない。
      return error instanceof Error && "code" in error && error.code === "ENOENT" ? "waiting" : "unavailable";
    }
  }));
  if (statuses.includes("unavailable")) return "unavailable";
  return statuses.every(status => status === "ready") ? "ready" : "waiting";
}

export async function awaitCohortReady(config: RunConfig, signal: AbortSignal): Promise<void> {
  const deadline = Date.parse(config.startAt) + config.durationSeconds * 1000;
  while (Date.now() < deadline) {
    signal.throwIfAborted();
    const status = await cohortStatus(config);
    if (status === "unavailable") throw new Error("participant_cohort_unavailable");
    if (status === "ready") return;
    await cancellableDelay(250, signal);
  }
  throw new Error("participant_cohort_deadline");
}

export async function requireCohortAlive(config: RunConfig): Promise<void> {
  // 初回成立後は個別WSの復帰を待たず、runner全体の中止とlease失効を監視する。
  if (await cohortStatus(config) !== "ready") throw new Error("participant_cohort_unavailable");
}
