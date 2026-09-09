import { useLayoutEffect, useRef, type RefObject } from "react";
import { contentMotionStartOpacity, motionDuration, motionEasing } from "../lib/motion";
import { useReducedMotion } from "./useReducedMotion";

/** Animate the existing container, never remount its contents or change layout. */
export function useContentMotion(ref: RefObject<HTMLElement | null>, identity: string, token: "tab" = "tab", selector?: string) {
  const reduced = useReducedMotion();
  const previous = useRef(identity);
  const animationRef = useRef<Animation | null>(null);
  useLayoutEffect(() => {
    const element = selector ? ref.current?.querySelector<HTMLElement>(selector) : ref.current;
    const changed = previous.current !== identity;
    previous.current = identity;
    const opacity = animationRef.current && element ? getComputedStyle(element).opacity : contentMotionStartOpacity[token];
    animationRef.current?.cancel();
    animationRef.current = null;
    if (!changed || reduced || !element || typeof element.animate !== "function") return;
    const animation = element.animate([{ opacity }, { opacity: 1 }], {
      duration: motionDuration(element, token), easing: motionEasing(element, "enter")
    });
    animationRef.current = animation;
    animation.onfinish = () => {
      if (animationRef.current === animation) animationRef.current = null;
    };
  }, [identity, reduced, ref, token, selector]);
  useLayoutEffect(() => () => { animationRef.current?.cancel(); }, []);
}
