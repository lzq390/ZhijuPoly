import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { motionDuration } from "../lib/motion";
import { useReducedMotion } from "./useReducedMotion";

/** Visual lock only: validation/data preparation belongs to the caller. */
export function useFlipMotion() {
  const ref = useRef<HTMLDivElement | null>(null);
  const locked = useRef(false);
  const [busy, setBusy] = useState(false);
  const reduced = useReducedMotion();
  const finish = useCallback(() => { locked.current = false; setBusy(false); }, []);
  useLayoutEffect(() => {
    if (!busy) return;
    if (reduced) { finish(); return; }
    const element = ref.current;
    const timer = window.setTimeout(finish, motionDuration(element, "flip") + 60);
    const end = (event: TransitionEvent) => {
      if (event.target === element && event.propertyName === "transform") finish();
    };
    element?.addEventListener("transitionend", end);
    element?.addEventListener("transitioncancel", end);
    return () => {
      window.clearTimeout(timer);
      element?.removeEventListener("transitionend", end);
      element?.removeEventListener("transitioncancel", end);
    };
  }, [busy, reduced, finish]);
  useLayoutEffect(() => () => { locked.current = false; }, []);
  return {
    ref, busy, locked, finish,
    start() {
      if (locked.current) return false;
      locked.current = !reduced;
      setBusy(!reduced);
      return true;
    }
  };
}
