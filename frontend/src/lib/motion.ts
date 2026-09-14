// CSS custom properties are the source of truth. These values also cover SSR,
// jsdom, and browsers where the stylesheet has not loaded yet.
export const motionDefaults = {
  control: 200,
  moduleExit: 400,
  moduleGap: 200,
  moduleEnter: 400,
  tab: 300,
  enter: 300,
  exit: 240,
  drawerEnter: 400,
  drawerExit: 320,
  backdrop: 240,
  flip: 400,
  feedbackDelay: 120
} as const;

export type MotionToken = keyof typeof motionDefaults;

export const contentMotionStartOpacity = { tab: "0" } as const;

const easingDefaults = {
  enter: "ease-in-out",
  exit: "ease-in-out",
  module: "ease-in-out"
} as const;

const cssNames: Record<MotionToken, string> = {
  control: "control", tab: "tab", enter: "enter", exit: "exit",
  moduleExit: "module-exit", moduleGap: "module-gap", moduleEnter: "module-enter",
  drawerEnter: "drawer-enter", drawerExit: "drawer-exit", backdrop: "backdrop",
  flip: "flip", feedbackDelay: "feedback-delay"
};

export function motionDuration(element: Element | null, token: MotionToken) {
  const raw = element && typeof getComputedStyle === "function"
    ? getComputedStyle(element).getPropertyValue(`--np-motion-${cssNames[token]}`).trim()
    : "";
  const value = Number.parseFloat(raw);
  return Number.isFinite(value) && value >= 0
    ? value * (raw.endsWith("ms") ? 1 : raw.endsWith("s") ? 1000 : 1)
    : motionDefaults[token];
}

export function motionEasing(element: Element, kind: keyof typeof easingDefaults = "enter") {
  return getComputedStyle(element).getPropertyValue(`--np-ease-${kind}`).trim()
    || easingDefaults[kind];
}
