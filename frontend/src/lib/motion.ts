// CSS custom properties are the source of truth. These values also cover SSR,
// jsdom, and browsers where the stylesheet has not loaded yet.
export const motionDefaults = {
  control: 120,
  moduleExit: 400,
  moduleGap: 200,
  moduleEnter: 400,
  tab: 120,
  enter: 160,
  exit: 120,
  drawerEnter: 200,
  drawerExit: 160,
  backdrop: 140,
  flip: 260,
  feedbackDelay: 120
} as const;

export type MotionToken = keyof typeof motionDefaults;

export const contentMotionStartOpacity = { tab: "0.88" } as const;

const easingDefaults = {
  enter: "cubic-bezier(0.22, 1, 0.36, 1)",
  exit: "cubic-bezier(0.4, 0, 1, 1)",
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
