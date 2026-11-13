import { fork, type ChildProcess } from "node:child_process";
import { EventEmitter } from "node:events";
import { writeFile } from "node:fs/promises";
import { createInterface, type Interface } from "node:readline";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { parentMessageSchema, workerInputSchema, emptyCounts, type WorkerCounts } from "../src/controlIpc.js";
import { controlSettingsSchema, type ControlSettings } from "../src/controlPlan.js";
import type { ManualCommand } from "../src/manualProtocol.js";
import { superviseControl } from "../src/controlSupervisor.js";
import { terminateWorkerTree } from "../src/controlTermination.js";
import { generateControlReport } from "../src/controlReport.js";

const recording = vi.hoisted(() => ({complete: true}));

vi.mock("node:child_process", () => ({fork: vi.fn()}));
vi.mock("node:fs/promises", () => ({mkdir: vi.fn().mockResolvedValue(undefined), writeFile: vi.fn().mockResolvedValue(undefined)}));
vi.mock("node:readline", () => ({createInterface: vi.fn()}));
vi.mock("../src/controlReport.js", () => ({generateControlReport: vi.fn().mockResolvedValue(undefined)}));
vi.mock("../src/controlTermination.js", () => ({terminateWorkerTree: vi.fn().mockResolvedValue(undefined)}));
vi.mock("../src/evidence.js", () => ({
  Evidence: class {
    event(): void {}
    async close(): Promise<void> {}
    health(): {complete: boolean} { return recording; }
  },
  startResourceSampling: () => async (): Promise<void> => {},
  startWorkerSampling: () => (): void => {},
}));

class Input extends EventEmitter {
  close(): void { this.emit("close"); }
}

class FakeWorker extends EventEmitter {
  connected = true;
  pid = 100000;
  exitCode: number | null = null;
  signalCode: string | null = null;
  readonly commands: ManualCommand[] = [];
  automaticStop = true;
  failSending = false;
  throwSending = false;
  kill = vi.fn(() => true);
  unref = vi.fn();
  disconnect(): void { this.connected = false; this.emit("disconnect"); }
  constructor(readonly workerId: string) { super(); }
  send(raw: unknown, callback: (error: Error | null) => void): boolean {
    if (this.throwSending) throw new Error("safe-test-failure");
    const {command} = parentMessageSchema.parse(raw);
    this.commands.push(command);
    callback(this.failSending ? new Error("safe-test-failure") : null);
    if (command.kind === "stop" && this.automaticStop) queueMicrotask(() => {
      this.emit("message", {type: "finished", workerId: this.workerId, clean: true});
      this.exit(0);
    });
    return true;
  }
  status(counts: WorkerCounts): void { this.emit("message", {type: "status", workerId: this.workerId, counts}); }
  exit(code: number): void { this.exitCode = code; this.connected = false; this.emit("exit", code); }
}

const input = new Input();
let workers: FakeWorker[] = [];
let stdout: MockInstance<typeof process.stdout.write>;
let stderr: MockInstance<typeof process.stderr.write>;
let previousExitCode: typeof process.exitCode;

function settings(): ControlSettings {
  return controlSettingsSchema.parse({
    workerSize: 50, setupTimeoutSeconds: 30,
    run: {runId: "supervision-test", sourceCommit: "0".repeat(40), baseUrl: "https://127.0.0.1",
      count: 1, ordinalStart: 1, startAt: new Date().toISOString(), durationSeconds: 180,
      joinSpreadMs: 0, operatorStartDelaySeconds: 10, preScheduleMarginMs: 1000,
      resultHoldMs: 3000, answerDelayMs: 0, scenario: "normal", faultAfterSeconds: 5,
      faultDurationMs: 100, outputDirectory: "/virtual-test", channel: "chromium", headless: true},
  });
}

async function start(total = 100, configuration = settings()): Promise<{completion: Promise<string>}> {
  const completion = superviseControl(configuration, total);
  await vi.waitFor(() => expect(workers).toHaveLength(Math.ceil(total / 50)));
  return {completion};
}
function ready(worker: FakeWorker, count = 50): void { worker.status({...emptyCounts(), ready: count}); }
function savedTermination(): unknown {
  const call = vi.mocked(writeFile).mock.calls.find(([path]) => String(path).endsWith("termination.json"));
  if (!call || typeof call[1] !== "string") throw new Error("termination_missing");
  return JSON.parse(call[1]) as unknown;
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers();
  workers = [];
  recording.complete = true;
  input.removeAllListeners();
  previousExitCode = process.exitCode;
  process.exitCode = undefined;
  stdout = vi.spyOn(process.stdout, "write").mockReturnValue(true);
  stderr = vi.spyOn(process.stderr, "write").mockReturnValue(true);
  vi.mocked(createInterface).mockReturnValue(input as unknown as Interface);
  vi.mocked(fork).mockImplementation((_modulePath, args) => {
    const workerId = Array.isArray(args) ? args[0]?.match(/(worker-\d+)\.json$/u)?.[1] : undefined;
    if (!workerId) throw new Error("fixture_worker_path_missing");
    const worker = new FakeWorker(workerId);
    workers.push(worker);
    return worker as unknown as ChildProcess;
  });
});
afterEach(() => {
  process.exitCode = previousExitCode;
  stdout.mockRestore();
  stderr.mockRestore();
  vi.useRealTimers();
});

describe("isolated same-host supervision", () => {
  it.each(["navigation", "join_button", "fonts"])("prints a validated %s failure and stops all workers", async stage => {
    const {completion} = await start();
    workers[1]!.emit("message", {type: "failed", workerId: "worker-2", preparationFailure: {serial: 59, stage}});
    await completion;
    expect(stderr).toHaveBeenCalledWith(`準備失敗: worker-2 / VU 59 / ${stage}\n`);
    expect(savedTermination()).toMatchObject({reason: "worker_failed", expectedWorkers: 2});
    expect(workers.every(worker => worker.commands.at(-1)?.kind === "stop")).toBe(true);
    expect(process.exitCode).toBe(1);
  });

  it.each([{serial: 59, stage: "private-error"}, {serial: 50, stage: "fonts"},
    {serial: 59, stage: "fonts", reason: "private-error"}])("rejects unsafe or out-of-worker failure details", async preparationFailure => {
    const {completion} = await start();
    workers[1]!.emit("message", {type: "failed", workerId: "worker-2", preparationFailure});
    await completion;
    expect(savedTermination()).toMatchObject({reason: "worker_invalid_message"});
    expect(stderr).not.toHaveBeenCalled();
    expect(process.exitCode).toBe(1);
  });

  it("keeps generic worker failures valid without inventing preparation details", async () => {
    const {completion} = await start();
    workers[1]!.emit("message", {type: "failed", workerId: "worker-2"});
    await completion;
    expect(savedTermination()).toMatchObject({reason: "worker_failed"});
    expect(stderr).not.toHaveBeenCalled();
  });

  it("waits for every planned worker before distributing a command to the full fixed target", async () => {
    const {completion} = await start();
    input.emit("line", "join");
    ready(workers[0]!);
    input.emit("line", "join");
    expect(workers.every(worker => worker.commands.length === 0)).toBe(true);
    ready(workers[1]!);
    input.emit("line", "join --spread 30s");
    const commands = workers.map(worker => worker.commands[0]);
    expect(commands[0]).toMatchObject({runId: "supervision-test", kind: "join", spreadMs: 30000,
      target: {ordinalStart: 1, count: 100}});
    expect(commands[0]).toEqual(commands[1]);
    input.emit("line", "stop");
    await completion;
    expect(savedTermination()).toMatchObject({reason: "requested_stop", expectedWorkers: 2,
      workers: [{workerId: "worker-1", exitCode: 0, clean: true}, {workerId: "worker-2", exitCode: 0, clean: true}]});
    expect(generateControlReport).toHaveBeenCalledOnce();
  });

  it("stops remaining workers after abnormal exit and preserves the expected inventory", async () => {
    const {completion} = await start();
    workers[0]!.exit(7);
    await completion;
    expect(workers[1]!.commands.at(-1)?.kind).toBe("stop");
    expect(savedTermination()).toMatchObject({reason: "worker_unexpected_exit", expectedWorkers: 2,
      workers: [{workerId: "worker-1", exitCode: 7, clean: false}, {workerId: "worker-2", exitCode: 0}]});
    expect(process.exitCode).toBe(1);
  });

  it("keeps missing status distinct from zero", async () => {
    const {completion} = await start();
    ready(workers[0]!);
    input.emit("line", "status");
    expect(stdout.mock.calls.some(([body]) => typeof body === "string" && body.includes('"counts":null'))).toBe(true);
    input.emit("line", "stop");
    await completion;
    expect(savedTermination()).toMatchObject({reason: "requested_stop", expectedWorkers: 2});
  });

  it("allows 60 seconds without a heartbeat and stops only after that limit", async () => {
    const {completion} = await start();
    workers.forEach(worker => ready(worker));
    await vi.advanceTimersByTimeAsync(60000);
    expect(workers.every(worker => worker.commands.length === 0)).toBe(true);
    expect(generateControlReport).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1000);
    await completion;
    expect(savedTermination()).toMatchObject({reason: "worker_heartbeat_timeout", expectedWorkers: 2});
    expect(process.exitCode).toBe(1);
  });

  it("rejects a status bound to an unexpected worker", async () => {
    const {completion} = await start(50);
    workers[0]!.emit("message", {type: "status", workerId: "other-worker", counts: emptyCounts()});
    await completion;
    expect(savedTermination()).toMatchObject({reason: "worker_invalid_message"});
  });

  it("fails delivery errors and gathers each child before returning", async () => {
    const {completion} = await start();
    workers.forEach(worker => ready(worker));
    workers[1]!.failSending = true;
    input.emit("line", "pause");
    await completion;
    expect(savedTermination()).toMatchObject({reason: "worker_ipc_failed"});
    expect(workers.every(worker => worker.exitCode === 0)).toBe(true);
  });

  it("forces a nonresponsive worker after the bounded graceful stop interval", async () => {
    const {completion} = await start(50);
    workers[0]!.automaticStop = false;
    vi.mocked(terminateWorkerTree).mockImplementationOnce(async () => { workers[0]!.exit(137); });
    input.emit("line", "stop");
    await vi.advanceTimersByTimeAsync(15000);
    expect(terminateWorkerTree).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(29999);
    expect(terminateWorkerTree).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    await completion;
    expect(terminateWorkerTree).toHaveBeenCalledOnce();
    expect(savedTermination()).toMatchObject({workers: [{exitCode: 137, clean: false}]});
    expect(process.exitCode).toBe(1);
  });

  it("accepts a clean worker exit beyond the old 15 second grace without killing it", async () => {
    const {completion} = await start(50);
    workers[0]!.automaticStop = false;
    input.emit("line", "stop");
    await vi.advanceTimersByTimeAsync(20000);
    expect(terminateWorkerTree).not.toHaveBeenCalled();
    workers[0]!.emit("message", {type: "finished", workerId: "worker-1", clean: true});
    workers[0]!.exit(0);
    await completion;
    expect(terminateWorkerTree).not.toHaveBeenCalled();
    expect(savedTermination()).toMatchObject({reason: "requested_stop", workers: [{exitCode: 0, clean: true}]});
    expect(process.exitCode).toBeUndefined();
  });
});

it("does not hang if forced cleanup fails and no exit event arrives", async () => {
  const {completion} = await start(50);
  workers[0]!.automaticStop = false;
  vi.mocked(terminateWorkerTree).mockRejectedValueOnce(new Error("safe-test-cleanup-failure"));
  input.emit("line", "stop");
  await vi.advanceTimersByTimeAsync(45000);
  expect(workers[0]!.kill).toHaveBeenCalledWith("SIGKILL");
  await vi.advanceTimersByTimeAsync(3000);
  expect(workers[0]!.unref).not.toHaveBeenCalled();
  expect(workers[0]!.connected).toBe(true);
  await vi.advanceTimersByTimeAsync(5999);
  expect(workers[0]!.unref).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(1);
  await completion;
  expect(workers[0]!.kill).toHaveBeenCalledWith("SIGKILL");
  expect(workers[0]!.unref).toHaveBeenCalledOnce();
  expect(workers[0]!.connected).toBe(false);
  expect(savedTermination()).toMatchObject({reason: "worker_cleanup_failed", workers: [{exitCode: null, clean: false}]});
  expect(process.exitCode).toBe(1);
});

it("gathers workers after a synchronous IPC send failure", async () => {
  const {completion} = await start(50);
  ready(workers[0]!);
  workers[0]!.throwSending = true;
  input.emit("line", "join");
  workers[0]!.throwSending = false;
  await completion;
  expect(savedTermination()).toMatchObject({reason: "worker_ipc_failed", workers: [{exitCode: 0}]});
});

it.each(["failed", "paused", "clicked"] as const)("rejects an impossible %s count", async field => {
  const {completion} = await start(50);
  const counts = emptyCounts();
  if (field === "clicked") counts.rounds[0]!.clicked = 51;
  else counts[field] = 51;
  workers[0]!.status(counts);
  await completion;
  expect(savedTermination()).toMatchObject({reason: "worker_invalid_counts"});
});

it("rejects duplicate rounds instead of interpreting them as complete status", async () => {
  const {completion} = await start(50);
  const counts = emptyCounts();
  counts.rounds[1]!.round = 1;
  workers[0]!.status(counts);
  await completion;
  expect(savedTermination()).toMatchObject({reason: "worker_invalid_message"});
});

it("rejects a contiguous worker snapshot with fewer rounds than the immutable plan", async () => {
  const {completion} = await start(50);
  workers[0]!.status(emptyCounts(5));
  await completion;
  expect(savedTermination()).toMatchObject({reason: "worker_invalid_counts"});
});

it("bounds preparation independently of liveness and waits for no human command", async () => {
  const {completion} = await start(50);
  const heartbeat = setInterval(() => workers[0]!.status(emptyCounts()), 1000);
  await vi.advanceTimersByTimeAsync(30000);
  clearInterval(heartbeat);
  await completion;
  expect(savedTermination()).toMatchObject({reason: "setup_timeout"});
  expect(workers[0]!.commands.map(command => command.kind)).toEqual(["stop"]);
});

it("has a finite manual run deadline even while workers stay healthy", async () => {
  const {completion} = await start(50);
  ready(workers[0]!);
  const heartbeat = setInterval(() => ready(workers[0]!), 1000);
  await vi.advanceTimersByTimeAsync(180000);
  clearInterval(heartbeat);
  await completion;
  expect(savedTermination()).toMatchObject({reason: "run_deadline"});
  expect(process.exitCode).toBe(1);
});

const awsSettings = (recordResults = true): ControlSettings => controlSettingsSchema.parse({...settings(),
  run: {...settings().run, baseUrl: "https://d123abc.cloudfront.net", durationSeconds: null,
    aws: {recordResults, sourceDirty: false}}});

it.each([true, false])("keeps a healthy independent AWS run alive until stop with recordResults=%s", async recordResults => {
  const {completion} = await start(50, awsSettings(recordResults));
  const worker = workers[0]!;
  expect(worker.workerId).toBe("worker-1");
  ready(worker);
  const heartbeat = setInterval(() => ready(worker), 1000);
  await vi.advanceTimersByTimeAsync(3600001);
  clearInterval(heartbeat);
  expect(worker.commands).toHaveLength(0);
  input.emit("line", "join");
  expect(worker.commands[0]).toMatchObject({kind: "join", target: {ordinalStart: 1, count: 50}});
  input.emit("line", "stop");
  await completion;
  expect(savedTermination()).toMatchObject({reason: "requested_stop", expectedWorkers: 1,
    workers: [{workerId: "worker-1", exitCode: 0}]});
  const saved = vi.mocked(writeFile).mock.calls.find(([path]) => String(path).endsWith("worker-1.json"));
  expect(typeof saved?.[1]).toBe("string");
  expect(workerInputSchema.parse(JSON.parse(String(saved?.[1])) as unknown))
    .toMatchObject({deadlineAtMs: null, settings: {run: {aws: {recordResults}}}});
  expect(generateControlReport).toHaveBeenCalledOnce();
  expect(stdout.mock.calls.some(([line]) => String(line).includes("stopまで継続"))).toBe(true);
  expect(process.exitCode).toBeUndefined();
});

it.each(["setup_timeout", "worker_heartbeat_timeout"])("retains AWS %s protection without a run deadline", async reason => {
  const {completion} = await start(50, awsSettings());
  const worker = workers[0]!;
  if (reason === "worker_heartbeat_timeout") ready(worker);
  const heartbeat = reason === "setup_timeout" ? setInterval(() => worker.status(emptyCounts()), 1000) : undefined;
  await vi.advanceTimersByTimeAsync(reason === "setup_timeout" ? 30000 : 61000);
  clearInterval(heartbeat);
  await completion;
  expect(savedTermination()).toMatchObject({reason});
  expect(worker.commands.at(-1)?.kind).toBe("stop");
  expect(process.exitCode).toBe(1);
});

it("marks supervisor recording overflow as failed even after orderly worker exit", async () => {
  const {completion} = await start();
  workers.forEach(worker => ready(worker));
  recording.complete = false;
  input.emit("line", "stop");
  await completion;
  expect(savedTermination()).toMatchObject({reason: "recording_failed"});
  expect(process.exitCode).toBe(1);
});

it("does not display stale worker counts as current connections", async () => {
  const {completion} = await start();
  workers.forEach(worker => ready(worker));
  await vi.advanceTimersByTimeAsync(3000);
  input.emit("line", "status");
  const line = stdout.mock.calls.find(([value]) => typeof value === "string" && value.includes('"plannedVus"'))?.[0];
  expect(typeof line).toBe("string");
  const snapshot: unknown = JSON.parse(String(line));
  expect(snapshot).toMatchObject({workers: [{stale: true, counts: null}, {stale: true, counts: null}]});
  input.emit("line", "stop");
  await completion;
});

it("always permits status and stop after the action command capacity is reached", async () => {
  const {completion} = await start();
  workers.forEach(worker => ready(worker));
  for (let index = 0; index < 4096; index++) input.emit("line", "pause");
  input.emit("line", "resume");
  expect(workers[0]!.commands).toHaveLength(4096);
  input.emit("line", "status");
  input.emit("line", "stop");
  await completion;
  expect(savedTermination()).toMatchObject({reason: "requested_stop"});
  expect(workers[0]!.commands.at(-1)).toMatchObject({kind: "stop"});
});
