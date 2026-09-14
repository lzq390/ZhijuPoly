import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";

export function useDrawerResize({ width, minWidth, maxWidth, onWidthChange, enabled }: {
  width: number; minWidth: number; maxWidth: number; onWidthChange: (width: number) => void; enabled: boolean;
}) {
  const [resizing, setResizing] = useState(false);
  const options = useRef({ minWidth, maxWidth, onWidthChange });
  options.current = { minWidth, maxWidth, onWidthChange };
  const drag = useRef<{ x: number; width: number; lastX: number; pointerId: number } | null>(null);
  const frame = useRef<number | null>(null);
  useEffect(() => {
    if (!enabled) {
      drag.current = null;
      setResizing(false);
      return;
    }
    const commit = () => {
      frame.current = null;
      if (!drag.current) return;
      const { minWidth: min, maxWidth: max, onWidthChange: change } = options.current;
      change(Math.min(max, Math.max(min, drag.current.width + drag.current.x - drag.current.lastX)));
    };
    const matches = (event: PointerEvent) => drag.current && event.pointerId === drag.current.pointerId;
    const move = (event: PointerEvent) => {
      if (!matches(event) || !drag.current) return;
      drag.current.lastX = event.clientX;
      if (frame.current === null) frame.current = window.requestAnimationFrame(commit);
    };
    const stop = (event?: PointerEvent) => {
      if (event && !matches(event)) return;
      if (frame.current !== null) window.cancelAnimationFrame(frame.current);
      if (drag.current && event?.type === "pointerup") drag.current.lastX = event.clientX;
      commit();
      drag.current = null;
      setResizing(false);
    };
    const blur = () => stop();
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", stop);
    document.addEventListener("pointercancel", stop);
    window.addEventListener("blur", blur);
    return () => {
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", stop);
      document.removeEventListener("pointercancel", stop);
      window.removeEventListener("blur", blur);
      if (frame.current !== null) window.cancelAnimationFrame(frame.current);
      frame.current = null;
      drag.current = null;
    };
  }, [enabled]);
  return {
    resizing,
    onPointerDown(event: ReactPointerEvent<HTMLElement>) {
      if (!enabled || (event.button !== undefined && event.button !== 0)) return;
      event.preventDefault();
      drag.current = { x: event.clientX, lastX: event.clientX, width, pointerId: event.pointerId };
      setResizing(true);
    }
  };
}
