import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useOperatorPanelLayout } from "./useOperatorPanelLayout";

const observers: TestResizeObserver[] = [];
const frames = new Map<number, FrameRequestCallback>();
let nextFrameId = 0;

class TestResizeObserver implements ResizeObserver {
  observe = vi.fn<(target: Element) => void>();
  unobserve = vi.fn<(target: Element) => void>();
  disconnect = vi.fn<() => void>();

  constructor(private readonly callback: ResizeObserverCallback) {
    observers.push(this);
  }

  notify(): void {
    this.callback([], this);
  }
}

function PanelGrid({ isEnabled = true }: { isEnabled?: boolean }) {
  const ref = useOperatorPanelLayout(isEnabled);
  return (
    <div
      data-testid="grid"
      ref={ref}
      style={{ gridTemplateColumns: "400px 400px", columnGap: "16px" }}
    >
      <section data-height="100.2" data-testid="first">進行操作</section>
      <section data-height="201" data-testid="second">公開設定</section>
    </div>
  );
}

function flushFrame(): void {
  act(() => {
    const scheduledFrames = Array.from(frames.values());
    frames.clear();
    scheduledFrames.forEach((callback) => callback(0));
  });
}

describe("useOperatorPanelLayout", () => {
  beforeEach(() => {
    observers.length = 0;
    frames.clear();
    nextFrameId = 0;
    vi.stubGlobal("ResizeObserver", TestResizeObserver);
    vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback): number => {
      const id = ++nextFrameId;
      frames.set(id, callback);
      return id;
    }));
    vi.stubGlobal("cancelAnimationFrame", vi.fn((id: number): void => {
      frames.delete(id);
    }));
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return new DOMRect(0, 0, 400, Number(this.dataset.height ?? 0));
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("ResizeObserverがなければ通常のCSS gridを維持する", () => {
    vi.stubGlobal("ResizeObserver", undefined);
    render(<PanelGrid />);

    expect(screen.getByTestId("grid")).not.toHaveClass("operator-panels-packed");
    expect(screen.getByTestId("first").style.gridRowEnd).toBe("");
    expect(screen.getByTestId("second").style.gridRowEnd).toBe("");
    expect(requestAnimationFrame).not.toHaveBeenCalled();
  });

  it("無効な間は観測せず、有効化後に自然高と余白で配置しDOM順を保持する", () => {
    const { rerender } = render(<PanelGrid isEnabled={false} />);
    expect(observers).toHaveLength(0);
    expect(screen.getByTestId("grid")).not.toHaveClass("operator-panels-packed");

    rerender(<PanelGrid />);
    const grid = screen.getByTestId("grid");
    const first = screen.getByTestId("first");
    const second = screen.getByTestId("second");
    expect(grid).toHaveClass("operator-panels-packed");
    expect(first.style.gridRowEnd).toBe("span 117");
    expect(second.style.gridRowEnd).toBe("span 217");
    expect(Array.from(grid.children)).toEqual([first, second]);
    expect(first.style.height).toBe("");
    expect(first.style.width).toBe("");
    expect(observers).toHaveLength(1);
    expect(observers[0]!.observe.mock.calls.map(([target]) => target)).toEqual([grid, first, second]);
  });

  it("1列への変更でpackingを解除し、2列へ戻すと再計測する", () => {
    render(<PanelGrid />);
    const grid = screen.getByTestId("grid");
    const first = screen.getByTestId("first");
    grid.style.gridTemplateColumns = "500px";
    observers[0]!.notify();
    flushFrame();

    expect(grid).not.toHaveClass("operator-panels-packed");
    expect(first.style.gridRowEnd).toBe("");
    expect(screen.getByTestId("second").style.gridRowEnd).toBe("");

    grid.style.gridTemplateColumns = "300px 300px";
    grid.style.columnGap = "12px";
    first.dataset.height = "230.5";
    observers[0]!.notify();
    flushFrame();
    expect(grid).toHaveClass("operator-panels-packed");
    expect(first.style.gridRowEnd).toBe("span 243");
  });

  it("内容の高さ変更を1 frameへ集約して最新の自然高を反映する", () => {
    render(<PanelGrid />);
    const first = screen.getByTestId("first");
    first.dataset.height = "150";
    observers[0]!.notify();
    first.dataset.height = "280.1";
    observers[0]!.notify();

    expect(frames.size).toBe(1);
    expect(first.style.gridRowEnd).toBe("span 117");
    flushFrame();
    expect(first.style.gridRowEnd).toBe("span 297");
    expect(screen.getByTestId("second").style.gridRowEnd).toBe("span 217");
  });

  it("無効化時は観測と未実行frameを取り消し通常配置へ戻す", () => {
    const { rerender } = render(<PanelGrid />);
    observers[0]!.notify();
    expect(frames.size).toBe(1);
    rerender(<PanelGrid isEnabled={false} />);

    expect(observers[0]!.disconnect).toHaveBeenCalledOnce();
    expect(frames.size).toBe(0);
    expect(screen.getByTestId("grid")).not.toHaveClass("operator-panels-packed");
    expect(screen.getByTestId("first").style.gridRowEnd).toBe("");
    expect(screen.getByTestId("second").style.gridRowEnd).toBe("");
  });

  it("unmount後に観測や予約frameを残さない", () => {
    const { unmount } = render(<PanelGrid />);
    const grid = screen.getByTestId("grid");
    const first = screen.getByTestId("first");
    observers[0]!.notify();
    unmount();

    expect(observers[0]!.disconnect).toHaveBeenCalledOnce();
    expect(frames.size).toBe(0);
    expect(grid).not.toHaveClass("operator-panels-packed");
    expect(first.style.gridRowEnd).toBe("");
  });
});
