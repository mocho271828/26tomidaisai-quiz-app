import { useLayoutEffect, useRef } from "react";

export function useOperatorPanelLayout(isEnabled: boolean) {
  const gridRef = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const grid = gridRef.current;
    if (!isEnabled || grid === null || typeof ResizeObserver === "undefined") return;
    const panels = Array.from(grid.children).filter(
      (element): element is HTMLElement => element instanceof HTMLElement,
    );
    let frame: number | undefined;
    const arrange = (): void => {
      const style = getComputedStyle(grid);
      const hasTwoColumns = style.gridTemplateColumns.split(" ").length === 2;
      grid.classList.toggle("operator-panels-packed", hasTwoColumns);
      const gap = Number.parseFloat(style.columnGap);
      for (const panel of panels) {
        // 自然高だけを測り、横幅を変更しないことで再計算の循環を防ぐ。
        panel.style.gridRowEnd = hasTwoColumns
          ? `span ${Math.ceil(panel.getBoundingClientRect().height + gap)}`
          : "";
      }
    };
    arrange();
    const observer = new ResizeObserver(() => {
      if (frame !== undefined) cancelAnimationFrame(frame);
      frame = requestAnimationFrame(arrange);
    });
    observer.observe(grid);
    panels.forEach((panel) => observer.observe(panel));
    return () => {
      observer.disconnect();
      if (frame !== undefined) cancelAnimationFrame(frame);
      grid.classList.remove("operator-panels-packed");
      panels.forEach((panel) => { panel.style.gridRowEnd = ""; });
    };
  }, [isEnabled]);

  return gridRef;
}
