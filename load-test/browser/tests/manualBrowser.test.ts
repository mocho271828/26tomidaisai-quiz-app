import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { Page } from "playwright";
import { PlaywrightManualDriver } from "../src/manualBrowser.js";
import type { ManualAction } from "../src/manualProtocol.js";

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());
const join: ManualAction = {kind: "join", commandId: "join-timeout", runId: "driver-timeout",
  issuedAtMs: 1, target: {ordinalStart: 1, count: 1}, spreadMs: 0};
const answer: Extract<ManualAction, {kind: "answer"}> = {...join, kind: "answer", round: 1,
  mode: "when-open", delayMs: 0};

it("bounds a hung arm evaluation and never starts its delayed click after timeout", async () => {
  let release!: () => void;
  const arm = new Promise<void>(resolve => { release = resolve; });
  const evaluate = vi.fn().mockReturnValue(arm);
  const click = vi.fn().mockResolvedValue(undefined);
  const close = vi.fn().mockResolvedValue(undefined);
  const getByRole = vi.fn().mockReturnValue({click});
  const page = {evaluate, close, getByRole, isClosed: () => false} as unknown as Page;
  const driver = new PlaywrightManualDriver(page, 25);
  const timeout = expect(driver.click(join, 1, 25)).rejects.toMatchObject({name: "TimeoutError"});
  await vi.advanceTimersByTimeAsync(25);
  await timeout;
  expect(close).toHaveBeenCalledWith({runBeforeUnload: false});
  release();
  await vi.advanceTimersByTimeAsync(0);
  expect(getByRole).not.toHaveBeenCalled();
  await expect(driver.click(join, 1, 25)).rejects.toMatchObject({name: "TimeoutError"});
});

it("bounds pause evaluation and makes stop sticky against a later resume", async () => {
  const close = vi.fn().mockResolvedValue(undefined);
  const evaluate = vi.fn().mockReturnValue(new Promise<void>(() => undefined));
  const driver = new PlaywrightManualDriver({evaluate, close} as unknown as Page, 20);
  const timeout = expect(driver.pause(true)).rejects.toMatchObject({name: "TimeoutError"});
  await vi.advanceTimersByTimeAsync(20);
  await timeout;
  await driver.pause(true, true);
  await driver.pause(false);
  expect(evaluate).toHaveBeenCalledTimes(1);
  expect(await driver.click(join, 1, 20)).toEqual({clicked: false});
});

it("bounds result DOM inspection even when its renderer does not respond", async () => {
  const close = vi.fn().mockResolvedValue(undefined);
  const evaluate = vi.fn().mockReturnValue(new Promise<void>(() => undefined));
  const driver = new PlaywrightManualDriver({evaluate, close} as unknown as Page, 20);
  const timeout = expect(driver.observeResult(1, 20)).rejects.toMatchObject({name: "TimeoutError"});
  await vi.advanceTimersByTimeAsync(20);
  await timeout;
  expect(close).toHaveBeenCalledOnce();
});

it("bounds reservation registration before beginning its separate human wait", async () => {
  let release!: () => void;
  const evaluate = vi.fn().mockReturnValue(new Promise<void>(resolve => { release = resolve; }));
  const close = vi.fn().mockResolvedValue(undefined);
  const driver = new PlaywrightManualDriver({evaluate, close} as unknown as Page, 20);
  const timeout = expect(driver.answer(answer, 1, Date.now() + 1000)).rejects.toMatchObject({name: "TimeoutError"});
  await vi.advanceTimersByTimeAsync(20);
  await timeout;
  release();
  await vi.advanceTimersByTimeAsync(0);
  expect(evaluate).toHaveBeenCalledOnce();
  expect(close).toHaveBeenCalledOnce();
});

it("allows human waiting beyond operation timeout but bounds a hung result after the run deadline", async () => {
  const evaluate = vi.fn().mockResolvedValueOnce(null).mockReturnValue(new Promise<void>(() => undefined));
  const close = vi.fn().mockResolvedValue(undefined);
  const driver = new PlaywrightManualDriver({evaluate, close} as unknown as Page, 20);
  const timeout = expect(driver.answer(answer, 1, Date.now() + 100)).rejects.toMatchObject({name: "TimeoutError"});
  await vi.advanceTimersByTimeAsync(25);
  expect(close).not.toHaveBeenCalled();
  expect(evaluate).toHaveBeenCalledTimes(2);
  await vi.advanceTimersByTimeAsync(95);
  await timeout;
  expect(close).toHaveBeenCalledOnce();
});

it("keeps an unbounded human wait cancellable even when the renderer never returns", async () => {
  const pending = new Promise<void>(() => undefined);
  const evaluate = vi.fn().mockResolvedValueOnce(null).mockReturnValueOnce(pending).mockResolvedValue(undefined);
  const close = vi.fn().mockResolvedValue(undefined);
  const driver = new PlaywrightManualDriver({evaluate, close} as unknown as Page, 20);
  const waiting = driver.answer(answer, 1, null);
  await vi.advanceTimersByTimeAsync(7200000);
  expect(evaluate).toHaveBeenCalledTimes(2);
  expect(close).not.toHaveBeenCalled();
  await driver.pause(true, true);
  expect(await waiting).toEqual({clicked: false, reason: "run_stopped"});
  await driver.pause(false);
  expect(await driver.answer(answer, 1, null)).toEqual({clicked: false, reason: "run_stopped"});
  expect(evaluate).toHaveBeenCalledTimes(3);
});

it("bounds an unresponsive AWS stop and still releases its pending answer wait", async () => {
  const evaluate = vi.fn().mockResolvedValueOnce(null).mockReturnValue(new Promise<void>(() => undefined));
  const close = vi.fn().mockResolvedValue(undefined);
  const driver = new PlaywrightManualDriver({evaluate, close} as unknown as Page, 20);
  const waiting = driver.answer(answer, 1, null);
  await vi.advanceTimersByTimeAsync(100);
  const stopping = expect(driver.pause(true, true)).rejects.toMatchObject({name: "TimeoutError"});
  expect(await waiting).toEqual({clicked: false, reason: "run_stopped"});
  await vi.advanceTimersByTimeAsync(20);
  await stopping;
  expect(close).toHaveBeenCalledExactlyOnceWith({runBeforeUnload: false});
});

it("still bounds AWS reservation registration instead of interpreting a hung renderer as human waiting", async () => {
  const evaluate = vi.fn().mockReturnValue(new Promise<void>(() => undefined));
  const close = vi.fn().mockResolvedValue(undefined);
  const driver = new PlaywrightManualDriver({evaluate, close} as unknown as Page, 20);
  const waiting = expect(driver.answer(answer, 1, null)).rejects.toMatchObject({name: "TimeoutError"});
  await vi.advanceTimersByTimeAsync(20);
  await waiting;
  expect(evaluate).toHaveBeenCalledOnce();
  expect(close).toHaveBeenCalledOnce();
});
