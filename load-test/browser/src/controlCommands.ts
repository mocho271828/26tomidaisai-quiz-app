import { randomUUID } from "node:crypto";
import { z } from "zod";
import type { ControlManifest } from "./controlPlan.js";
import { manualCommandSchema, type ManualCommand } from "./manualProtocol.js";

export const cliVusSchema = z.coerce.number().int().min(50).max(650);
export function durationMs(text: string): number {
  const match = /^(\d+(?:\.\d+)?)(ms|s)$/.exec(text);
  if (!match) throw new Error("duration_requires_ms_or_s");
  return z.number().int().min(0).max(120000).parse(Number(match[1]) * (match[2] === "s" ? 1000 : 1));
}
export function parseControlCommand(line: string, manifest: ControlManifest, now = Date.now()): ManualCommand | "status" {
  const [kind, ...tokens] = line.trim().split(/\s+/u);
  if (kind === "status" && tokens.length === 0) return "status";
  const args = new Map<string, string>();
  while (tokens.length) {
    const flag = tokens.shift()!;
    if (!["--spread", "--round", "--delay", "--now", "--when-open"].includes(flag) || args.has(flag))
      throw new Error("invalid_command_option");
    const value = flag === "--now" || flag === "--when-open" ? "true" : tokens.shift();
    if (value === undefined) throw new Error("missing_command_value");
    args.set(flag, value);
  }
  const base = {runId: manifest.runId, commandId: randomUUID(), issuedAtMs: now,
    target: {ordinalStart: Math.min(...manifest.workers.map(worker => worker.ordinalStart)), count: manifest.totalVus}};
  if (kind === "join" && [...args.keys()].every(flag => flag === "--spread"))
    return manualCommandSchema.parse({...base, kind, spreadMs: durationMs(args.get("--spread") ?? "0ms")});
  if (kind === "answer" && args.has("--round") && args.has("--now") !== args.has("--when-open")) {
    if (Number(args.get("--round")) > manifest.expectedRounds) throw new Error("round_outside_plan");
    return manualCommandSchema.parse({...base, kind, round: Number(args.get("--round")),
      mode: args.has("--now") ? "now" : "when-open", delayMs: durationMs(args.get("--delay") ?? "0ms"),
      spreadMs: durationMs(args.get("--spread") ?? "0ms")});
  }
  if (["pause", "resume", "stop"].includes(kind ?? "") && args.size === 0)
    return manualCommandSchema.parse({...base, kind});
  throw new Error("invalid_control_command");
}
