import { useLayoutEffect, useState, type RefObject } from "react";

/** Measure the workspace, not the viewport (the platform sidebar also takes space). */
export function useDrawerMode(ref: RefObject<HTMLElement | null>, {
  closest, inlineMinWidth = 1280, fallback = "overlay"
}: { closest?: string; inlineMinWidth?: number; fallback?: "inline" | "overlay" } = {}) {
  const [mode, setMode] = useState(fallback);
  useLayoutEffect(() => {
    const element = closest ? ref.current?.closest<HTMLElement>(closest) : ref.current;
    if (!element) return;
    const update = (width: number) => {
      // Hidden kept-alive modules have no geometry; keep their last layout mode.
      if (width > 0) setMode(width >= inlineMinWidth ? "inline" : "overlay");
    };
    const measure = () => update(element.getBoundingClientRect().width);
    measure();
    window.addEventListener("resize", measure);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(entries => update(entries[0]?.contentRect.width ?? element.getBoundingClientRect().width));
    observer?.observe(element);
    return () => { observer?.disconnect(); window.removeEventListener("resize", measure); };
  }, [ref, closest, inlineMinWidth]);
  return mode;
}
