import { EventEmitter } from "node:events";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { checkLanBrowser, checkLanReadiness } from "../src/lanChecks.js";

const transport = vi.hoisted(() => ({get: vi.fn(), launch: vi.fn()}));
vi.mock("node:https", () => ({get: transport.get}));
vi.mock("playwright", () => ({chromium: {launch: transport.launch}}));

beforeEach(() => vi.resetAllMocks());
afterEach(() => vi.useRealTimers());

it("uses exact read-only readiness URL, supplied CA and TLS verification without redirects", async () => {
  const request = Object.assign(new EventEmitter(), {destroy: vi.fn()});
  const response = Object.assign(new EventEmitter(), {statusCode: 200, destroy: vi.fn(),
    resume: () => queueMicrotask(() => response.emit("end"))});
  transport.get.mockImplementation((_url: unknown, _options: unknown, callback: (value: unknown) => void) => {
    queueMicrotask(() => callback(response)); return request;
  });
  await checkLanReadiness("fixture-ca");
  expect(transport.get).toHaveBeenCalledWith("https://10.0.1.2:8443/api/health/ready",
    {ca: "fixture-ca", rejectUnauthorized: true, agent: false}, expect.any(Function));
  response.statusCode = 302;
  await expect(checkLanReadiness("fixture-ca")).rejects.toThrow("lan_readiness_unavailable");
  expect(response.destroy).toHaveBeenCalledOnce();
});

it("limits the complete readiness exchange to thirty seconds and destroys a hanging request", async () => {
  vi.useFakeTimers();
  const request = Object.assign(new EventEmitter(), {destroy: vi.fn()});
  transport.get.mockReturnValue(request);
  const expected = expect(checkLanReadiness("fixture-ca")).rejects.toThrow("lan_readiness_timeout");
  await vi.advanceTimersByTimeAsync(29999);
  expect(request.destroy).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(1);
  await expected;
  expect(request.destroy).toHaveBeenCalledOnce();
});

it("does not expose low-level TLS error text", async () => {
  const request = Object.assign(new EventEmitter(), {destroy: vi.fn()});
  transport.get.mockImplementation(() => {
    queueMicrotask(() => request.emit("error", new Error("sensitive network exception")));
    return request;
  });
  await expect(checkLanReadiness("fixture-ca")).rejects.toThrow("lan_readiness_tls_or_network_failed");
});

it("uses runner Chromium with normal certificate checks and only waits for the join button", async () => {
  vi.useFakeTimers();
  const waitFor = vi.fn().mockResolvedValue(undefined);
  const click = vi.fn();
  const goto = vi.fn().mockImplementation(async () => { await vi.advanceTimersByTimeAsync(12000); });
  const getByRole = vi.fn().mockReturnValue({waitFor, click});
  const newContext = vi.fn().mockResolvedValue({newPage: async () => ({goto, getByRole})});
  const close = vi.fn().mockResolvedValue(undefined);
  transport.launch.mockResolvedValue({newContext, close});
  await checkLanBrowser();
  expect(transport.launch).toHaveBeenCalledWith({headless: true, timeout: 30000,
    args: ["--disable-background-timer-throttling", "--disable-renderer-backgrounding"]});
  expect(newContext).toHaveBeenCalledWith({viewport: {width: 390, height: 844}, locale: "ja-JP"});
  expect(goto).toHaveBeenCalledWith("https://10.0.1.2:8443", {waitUntil: "domcontentloaded", timeout: 30000});
  expect(getByRole).toHaveBeenCalledWith("button", {name: "参加する", exact: true});
  expect(waitFor).toHaveBeenCalledWith({state: "visible", timeout: 18000});
  expect(click).not.toHaveBeenCalled();
  expect(close).toHaveBeenCalledOnce();
  goto.mockRejectedValue(new Error("private browser error"));
  await expect(checkLanBrowser()).rejects.toThrow("lan_browser_tls_or_page_failed");
  expect(close).toHaveBeenCalledTimes(2);
});
