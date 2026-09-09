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
    const previousAnimation = animationRef.current;
    if (previousAnimation) {
      previousAnimation.onfinish = null;
      previousAnimation.cancel();
    }
    animationRef.current = null;
    if (!changed || reduced || !element || typeof element.animate !== "function") return;
    // A changed identity means new visible content, even if its DOM is reused.
    // Do not inherit the previous tab's near-complete opacity (or read opacity
    // from another kept-alive panel selected by the new selector).
    try {
      const animation = element.animate([{ opacity: contentMotionStartOpacity[token] }, { opacity: 1 }], {
        duration: motionDuration(element, token), easing: motionEasing(element, "enter")
      });
      animationRef.current = animation;
      animation.onfinish = () => {
        if (animationRef.current === animation) animationRef.current = null;
      };
    } catch {
      // Opacity is not written inline: an unsupported animation leaves the
      // existing tab visible and interactive, with no business state changes.
    }
  }, [identity, reduced, ref, token, selector]);
  useLayoutEffect(() => () => { animationRef.current?.cancel(); }, []);
}
