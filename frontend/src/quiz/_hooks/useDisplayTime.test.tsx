import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useServerClock } from "./useServerClock";
import { useDisplayTime, useTimeBoundary, type DisplayClock } from "./useDisplayTime";

const api = vi.hoisted(() => ({ getServerTime: vi.fn() }));
vi.mock("../../api/audienceClient", () => api);

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-25T00:00:00Z"));
  api.getServerTime.mockResolvedValue({ server_time: new Date().toISOString(), shared_sample_age_ms: 0 });
});
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); vi.clearAllMocks(); });

function Display({ clock, until = Number.POSITIVE_INFINITY, enabled = true }:
  { clock: DisplayClock; until?: number; enabled?: boolean }) {
  return <output>{Math.round(useDisplayTime(clock, until, enabled))}</output>;
}

it("時計の安全監視を続けながら表示tickでページ所有者を再描画しない", async () => {
  const ownerRender = vi.fn();
  function Owner() {
    ownerRender();
    const clock = useServerClock();
    return <Display clock={clock} enabled={clock.isSynchronized} />;
  }
  render(<Owner />);
  await act(async () => vi.advanceTimersByTimeAsync(0));
  ownerRender.mockClear();
  const initial = Number(screen.getByRole("status").textContent);
  await act(async () => vi.advanceTimersByTimeAsync(1000));
  expect(Number(screen.getByRole("status").textContent)).toBe(initial + 1000);
  expect(ownerRender).not.toHaveBeenCalled();
  expect(api.getServerTime).toHaveBeenCalledTimes(3);
  vi.setSystemTime(new Date("2036-01-01T00:00:00Z"));
  await act(async () => vi.advanceTimersByTimeAsync(100));
  expect(ownerRender).toHaveBeenCalled();
  expect(api.getServerTime).toHaveBeenCalledTimes(6);
});

it("開始・締切だけで親を更新し、終了したタイマーのtickを回収する", async () => {
  const parentRender = vi.fn();
  const readNow = () => performance.now();
  function Owner() {
    parentRender();
    const now = readNow();
    useTimeBoundary({now, readNow}, [1000, 2000]);
    return <><span data-testid="phase">{now < 1000 ? "scheduled" : now < 2000 ? "answering" : "collecting"}</span>
      <Display clock={{now, readNow}} until={2000} /></>;
  }
  render(<Owner />);
  await act(async () => vi.advanceTimersByTimeAsync(999));
  expect(parentRender).toHaveBeenCalledTimes(1);
  expect(screen.getByTestId("phase")).toHaveTextContent("scheduled");
  await act(async () => vi.advanceTimersByTimeAsync(1));
  expect(parentRender).toHaveBeenCalledTimes(2);
  expect(screen.getByTestId("phase")).toHaveTextContent("answering");
  await act(async () => vi.advanceTimersByTimeAsync(1000));
  expect(parentRender).toHaveBeenCalledTimes(3);
  expect(screen.getByTestId("phase")).toHaveTextContent("collecting");
  expect(vi.getTimerCount()).toBe(0);
  await act(async () => vi.advanceTimersByTimeAsync(1000));
  expect(screen.getByRole("status")).toHaveTextContent("2000");
});

it("回答選択後は更新を止め、別の同期anchorでは古い表示標本を使わない", async () => {
  const readNow = () => performance.now();
  const view = render(<Display clock={{now: 0, readNow}} />);
  await act(async () => vi.advanceTimersByTimeAsync(500));
  view.rerender(<Display clock={{now: 267, readNow}} enabled={false} />);
  await act(async () => vi.advanceTimersByTimeAsync(1000));
  expect(screen.getByRole("status")).toHaveTextContent("267");
  expect(vi.getTimerCount()).toBe(0);
  view.rerender(<Display clock={{now: 50, readNow: () => 50}} />);
  expect(screen.getByRole("status")).toHaveTextContent("50");
});

it("小数msの境界では早いcallbackでも通知を失わず開始前には進めない", async () => {
  const original = window.setTimeout;
  window.setTimeout = new Proxy(original, {
    apply(target, receiver, args: unknown[]) {
      window.setTimeout = original;
      return Reflect.apply(target, receiver, [args[0], 0, ...args.slice(2)]);
    },
  });
  const readNow = () => performance.now();
  function Owner() {
    useTimeBoundary({now: readNow(), readNow}, [1000.5]);
    return <output>{readNow() >= 1000.5 ? "open" : "scheduled"}</output>;
  }
  try { render(<Owner />); }
  finally { window.setTimeout = original; }
  await act(async () => vi.advanceTimersByTimeAsync(0));
  expect(screen.getByRole("status")).toHaveTextContent("scheduled");
  await act(async () => vi.advanceTimersByTimeAsync(1000));
  expect(screen.getByRole("status")).toHaveTextContent("scheduled");
  await act(async () => vi.advanceTimersByTimeAsync(1));
  expect(screen.getByRole("status")).toHaveTextContent("open");
});
