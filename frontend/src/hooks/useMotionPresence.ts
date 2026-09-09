import { useCallback, useLayoutEffect, useRef, useState, type RefObject, type TransitionEvent } from "react";
import { motionDuration, type MotionToken } from "../lib/motion";
import { useReducedMotion } from "./useReducedMotion";

export type MotionPhase = "entering" | "open" | "exiting" | "closed";

/** Keeps the visual node/slot alive, without delaying the caller's business state. */
export function useMotionPresence<T extends HTMLElement = HTMLDivElement>(
  open: boolean,
  { enter = "enter", exit = "exit", property = "opacity", elementRef }: {
    enter?: MotionToken; exit?: MotionToken; property?: "opacity" | "transform";
    elementRef?: RefObject<T | null>;
  } = {}
) {
  const reduced = useReducedMotion();
  const internalRef = useRef<T | null>(null);
  const ref = elementRef ?? internalRef;
  const [phase, setPhase] = useState<MotionPhase>(open ? "open" : "closed");
  const [active, setActive] = useState(open);
  const phaseRef = useRef(phase);
  const finishRef = useRef<(() => void) | null>(null);
  const armFallbackRef = useRef<(() => void) | null>(null);

  useLayoutEffect(() => {
    let frame = 0;
    let timer = 0;
    let cancelled = false;
    const target = open ? "open" : "closed";
    const finish = () => {
      if (cancelled) return;
      window.cancelAnimationFrame(frame);
      window.clearTimeout(timer);
      phaseRef.current = target;
      setPhase(target);
      setActive(open);
      finishRef.current = null;
      armFallbackRef.current = null;
    };
    if (reduced || document.hidden || phaseRef.current === target) {
      finish();
      return;
    }
    const wasClosed = phaseRef.current === "closed";
    phaseRef.current = open ? "entering" : "exiting";
    setPhase(phaseRef.current);
    finishRef.current = finish;
    armFallbackRef.current = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(finish, motionDuration(ref.current, open ? enter : exit) + 60);
    };
    const element = ref.current;
    const transitionRun = (event: globalThis.TransitionEvent) => {
      // Rearm from the browser's actual start when iframe/layout work delayed it.
      if (event.target === element && event.propertyName === property) armFallbackRef.current?.();
    };
    element?.addEventListener("transitionrun", transitionRun);
    // An entering node needs one painted closed frame. Reversals start directly
    // from the current interpolated CSS value instead of jumping back to zero.
    const start = () => {
      setActive(open);
      armFallbackRef.current?.();
    };
    if (open && wasClosed) {
      frame = window.requestAnimationFrame(() => {
        frame = window.requestAnimationFrame(start);
      });
    } else start();
    return () => {
      cancelled = true;
      window.cancelAnimationFrame(frame);
      window.clearTimeout(timer);
      element?.removeEventListener("transitionrun", transitionRun);
      finishRef.current = null;
      armFallbackRef.current = null;
    };
  }, [open, reduced, enter, exit, ref, property]);

  const onTransitionEnd = useCallback((event: TransitionEvent<T>) => {
    if (event.target === ref.current && event.propertyName === property) finishRef.current?.();
  }, [property, ref]);
  const finish = useCallback(() => finishRef.current?.(), []);

  useLayoutEffect(() => {
    const settleInBackground = () => { if (document.hidden) finish(); };
    document.addEventListener("visibilitychange", settleInBackground);
    return () => document.removeEventListener("visibilitychange", settleInBackground);
  }, [finish]);

  return {
    ref,
    phase,
    present: open || phase !== "closed",
    active,
    finish,
    motionProps: {
      "data-motion-phase": phase,
      "data-motion-active": active,
      onTransitionEnd
    }
  };
}
