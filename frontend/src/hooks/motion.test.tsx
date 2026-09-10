// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { useRef, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useMotionPresence } from "./useMotionPresence";
import { useContentMotion } from "./useContentMotion";
import { useFlipMotion } from "./useFlipMotion";
import { useDrawerResize } from "./useDrawerResize";
import { useModalFocus } from "./useModalFocus";
import { useGuardedNavigation } from "./useGuardedNavigation";

let reduce = false;
let mediaListeners: Set<() => void>;
beforeEach(() => {
  vi.useFakeTimers();
  reduce = false;
  mediaListeners = new Set();
  vi.stubGlobal("matchMedia", (query: string) => ({
    get matches() { return query.includes("prefers-reduced-motion") && reduce; },
    addEventListener: (_event: string, callback: () => void) => mediaListeners.add(callback),
    removeEventListener: (_event: string, callback: () => void) => mediaListeners.delete(callback)
  }));
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
function preferReduced() { act(() => { reduce = true; mediaListeners.forEach((listener) => listener()); }); }
function tick(ms: number) { act(() => { vi.advanceTimersByTime(ms); }); }
function end(element: Element, propertyName = "opacity") {
  const event = new Event("transitionend", { bubbles: true });
  Object.defineProperty(event, "propertyName", { value: propertyName });
  fireEvent(element, event);
}

function Presence({ open }: { open: boolean }) {
  const motion = useMotionPresence(open);
  return motion.present ? <div ref={motion.ref} {...motion.motionProps} data-testid="panel"><span data-testid="child" /></div> : null;
}

describe("motion presence", () => {
  it("retains exit nodes and ignores child/unrelated transition events", () => {
    const view = render(<Presence open />);
    view.rerender(<Presence open={false} />);
    const panel = screen.getByTestId("panel");
    expect(panel.dataset.motionPhase).toBe("exiting");
    end(screen.getByTestId("child"));
    end(panel, "color");
    expect(panel.isConnected).toBe(true);
    end(panel);
    expect(screen.queryByTestId("panel")).toBeNull();
  });
  it("cancels an obsolete exit when reopened and settles without browser events", () => {
    const view = render(<Presence open />);
    view.rerender(<Presence open={false} />);
    tick(80);
    view.rerender(<Presence open />);
    tick(110);
    expect(screen.getByTestId("panel").dataset.motionPhase).toBe("entering");
    tick(249);
    expect(screen.getByTestId("panel").dataset.motionPhase).toBe("entering");
    tick(1);
    expect(screen.getByTestId("panel").dataset.motionPhase).toBe("open");
  });
  it("handles close-before-first-frame and runtime reduced motion without waiting", () => {
    const view = render(<Presence open={false} />);
    view.rerender(<Presence open />);
    view.rerender(<Presence open={false} />);
    preferReduced();
    expect(screen.queryByTestId("panel")).toBeNull();
    view.rerender(<Presence open />);
    expect(screen.getByTestId("panel").dataset.motionPhase).toBe("open");
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
  it("measures the fallback from the browser's transition start, not a delayed React commit", () => {
    const view = render(<Presence open />);
    view.rerender(<Presence open={false} />);
    tick(250);
    const event = new Event("transitionrun", { bubbles: true });
    Object.defineProperty(event, "propertyName", { value: "opacity" });
    fireEvent(screen.getByTestId("panel"), event);
    tick(299);
    expect(screen.getByTestId("panel").dataset.motionPhase).toBe("exiting");
    tick(1);
    expect(screen.queryByTestId("panel")).toBeNull();
  });
  it("settles a backgrounded panel even before its first entering frame", () => {
    const view = render(<Presence open={false} />);
    view.rerender(<Presence open />);
    vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    fireEvent(document, new Event("visibilitychange"));
    expect(screen.getByTestId("panel").dataset.motionPhase).toBe("open");
    expect(screen.getByTestId("panel").dataset.motionActive).toBe("true");
    view.rerender(<Presence open={false} />);
    expect(screen.queryByTestId("panel")).toBeNull();
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe("tab content motion", () => {
  it("starts each kept-alive target at zero and leaves it visible if animation is unavailable", () => {
    const descriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "animate");
    const targets: HTMLElement[] = [];
    const animate = vi.fn(function (this: HTMLElement) {
      targets.push(this);
      return { cancel: vi.fn(), onfinish: null };
    });
    Object.defineProperty(HTMLElement.prototype, "animate", { configurable: true, value: animate });
    function Tabs({ tab }: { tab: string }) {
      const ref = useRef<HTMLDivElement | null>(null);
      useContentMotion(ref, tab, "tab", ".active");
      return <div ref={ref}>{["a", "b"].map((id) => <section key={id} data-testid={id} className={tab === id ? "active" : ""} hidden={tab !== id}><input defaultValue={id} /></section>)}</div>;
    }
    try {
      const view = render(<Tabs tab="a" />);
      const first = screen.getByTestId("a");
      view.rerender(<Tabs tab="b" />);
      view.rerender(<Tabs tab="a" />);
      expect(targets).toEqual([screen.getByTestId("b"), first]);
      expect(animate.mock.calls).toEqual([
        [[{ opacity: "0" }, { opacity: 1 }], { duration: 300, easing: "ease-in-out" }],
        [[{ opacity: "0" }, { opacity: 1 }], { duration: 300, easing: "ease-in-out" }]
      ]);
      animate.mockImplementation(() => { throw new Error("animation unavailable"); });
      expect(() => view.rerender(<Tabs tab="b" />)).not.toThrow();
      expect(screen.getByTestId("b").hidden).toBe(false);
      expect(screen.getByTestId("b").style.opacity).toBe("");
      expect(screen.getByTestId("a")).toBe(first);
    } finally {
      if (descriptor) Object.defineProperty(HTMLElement.prototype, "animate", descriptor);
      else Reflect.deleteProperty(HTMLElement.prototype, "animate");
    }
  });

  it.each(["tab"] as const)("uses scoped %s defaults, skips unchanged content and cancels rapid changes/reduced motion", (token) => {
    const animations: { cancel: ReturnType<typeof vi.fn> }[] = [];
    const animate = vi.fn(() => { const animation = { cancel: vi.fn() }; animations.push(animation); return animation as unknown as Animation; });
    vi.stubGlobal("Animation", class {});
    const descriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "animate");
    Object.defineProperty(HTMLElement.prototype, "animate", { configurable: true, value: animate });
    function Content({ module }: { module: string }) {
      const ref = useRef<HTMLDivElement | null>(null);
      useContentMotion(ref, module, token);
      return <div ref={ref}><iframe title="retained editor" /></div>;
    }
    try {
      const view = render(<Content module="a" />);
      const frame = screen.getByTitle("retained editor");
      expect(animate).not.toHaveBeenCalled();
      view.rerender(<Content module="b" />);
      view.rerender(<Content module="b" />);
      expect(animate).toHaveBeenCalledTimes(1);
      const expectedTiming = { duration: 300, easing: "ease-in-out" };
      expect(animate).toHaveBeenNthCalledWith(1,
        [{ opacity: "0" }, { opacity: 1 }], expectedTiming);
      // A rapidly selected new tab must not inherit an almost-finished fade.
      const computedStyle = vi.spyOn(window, "getComputedStyle").mockReturnValue({ opacity: "0.98", getPropertyValue: () => "" } as unknown as CSSStyleDeclaration);
      view.rerender(<Content module="c" />);
      expect(animations[0].cancel).toHaveBeenCalledOnce();
      expect(animate).toHaveBeenNthCalledWith(2, [{ opacity: "0" }, { opacity: 1 }], expectedTiming);
      computedStyle.mockRestore();
      expect(screen.getByTitle("retained editor")).toBe(frame);
      preferReduced();
      expect(animations[1].cancel).toHaveBeenCalledOnce();
      view.rerender(<Content module="d" />);
      expect(animate).toHaveBeenCalledTimes(2);
      view.unmount();
    } finally {
      if (descriptor) Object.defineProperty(HTMLElement.prototype, "animate", descriptor);
      else Reflect.deleteProperty(HTMLElement.prototype, "animate");
    }
  });
});

describe("visual flip lock", () => {
  function Flip() {
    const flip = useFlipMotion();
    const [face, setFace] = useState(false);
    return <div ref={flip.ref} data-testid="flip" data-busy={flip.busy} data-face={face}>
      <button onClick={() => { if (flip.start()) setFace((value) => !value); }}>flip</button>
    </div>;
  }
  it("locks through the 400ms visual duration and filters child events", () => {
    render(<Flip />);
    const button = screen.getByText("flip");
    const panel = screen.getByTestId("flip");
    fireEvent.click(button);
    tick(399);
    fireEvent.click(button);
    expect(panel.dataset.face).toBe("true");
    end(button, "transform");
    expect(panel.dataset.busy).toBe("true");
    tick(1);
    end(panel, "transform");
    expect(panel.dataset.busy).toBe("false");
    fireEvent.click(button);
    expect(panel.dataset.face).toBe("false");
    preferReduced();
    expect(panel.dataset.busy).toBe("false");
  });
  it("releases on cancellation and clears the fallback on unmount", () => {
    const view = render(<Flip />);
    fireEvent.click(screen.getByText("flip"));
    const event = new Event("transitioncancel", { bubbles: true });
    Object.defineProperty(event, "propertyName", { value: "transform" });
    fireEvent(screen.getByTestId("flip"), event);
    expect(screen.getByTestId("flip").dataset.busy).toBe("false");
    fireEvent.click(screen.getByText("flip"));
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe("guarded navigation", () => {
  function setup(guard: () => Promise<void | boolean>) {
    const onCommit = vi.fn();
    const containerRef = { current: null };
    return { onCommit, ...renderHook(({ activeModule }) => useGuardedNavigation({ beforeNavigate: guard, onCommit, activeModule, containerRef }), { initialProps: { activeModule: "a" } }) };
  }
  it("announces after 120ms and executes only the last target using one guard", async () => {
    let resolve!: () => void;
    const guard = vi.fn(() => new Promise<void>((done) => { resolve = done; }));
    const { result, onCommit } = setup(guard);
    const actions = [vi.fn(), vi.fn(), vi.fn()];
    act(() => actions.forEach((action, index) => result.current.navigate(action, { id: String(index), label: `模块${index}` })));
    expect(result.current.pendingTarget?.id).toBe("2");
    tick(119);
    expect(result.current.showPending).toBe(false);
    tick(1);
    expect(result.current.showPending).toBe(true);
    await act(async () => resolve());
    expect(guard).toHaveBeenCalledOnce();
    expect(actions[0]).not.toHaveBeenCalled();
    expect(actions[1]).not.toHaveBeenCalled();
    expect(actions[2]).toHaveBeenCalledOnce();
    expect(onCommit).toHaveBeenCalledOnce();
    expect(result.current.pendingTarget).toBeNull();
  });
  it.each(["refuse", "reject", "throw", "timeout"])("cleans pending on guard %s", async (mode) => {
    const guard = () => {
      if (mode === "throw") throw new Error("sync guard failure");
      if (mode === "reject") return Promise.reject(new Error("guard failure"));
      if (mode === "refuse") return Promise.resolve(false);
      return new Promise<void>(() => {});
    };
    const { result } = setup(guard);
    const action = vi.fn();
    await act(async () => { result.current.navigate(action, { id: "b", label: "模块B" }); });
    if (mode === "timeout") await act(async () => { vi.advanceTimersByTime(1500); });
    expect(action).toHaveBeenCalledTimes(mode === "refuse" ? 0 : 1);
    expect(result.current.pendingTarget).toBeNull();
    expect(result.current.showPending).toBe(false);
  });
  it("does not replace or replay a command and ignores late resolution after cancellation", async () => {
    let resolve!: () => void;
    const { result } = setup(() => new Promise<void>((done) => { resolve = done; }));
    const command = vi.fn();
    const module = vi.fn();
    act(() => {
      result.current.navigate(command);
      result.current.navigate(command);
      result.current.navigate(module, { id: "b", label: "B" });
    });
    await act(async () => resolve());
    expect(command).toHaveBeenCalledOnce();
    expect(module).not.toHaveBeenCalled();
    act(() => { result.current.navigate(module, { id: "b", label: "B" }); result.current.cancel(); });
    await act(async () => resolve());
    expect(module).not.toHaveBeenCalled();
  });
  it.each(["route", "popstate", "unmount"])("invalidates pending intent on %s", async (mode) => {
    let resolve!: () => void;
    const view = setup(() => new Promise<void>((done) => { resolve = done; }));
    const action = vi.fn();
    act(() => view.result.current.navigate(action, { id: "b", label: "B" }));
    if (mode === "route") view.rerender({ activeModule: "c" });
    else if (mode === "popstate") act(() => window.dispatchEvent(new PopStateEvent("popstate")));
    else view.unmount();
    await act(async () => resolve());
    expect(action).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe("drawer resizing and modal visibility", () => {
  it("coalesces pointer updates, flushes pointerup, and cleans blur/close/unmount", () => {
    const change = vi.fn();
    const view = renderHook(({ enabled }) => useDrawerResize({ width: 380, minWidth: 320, maxWidth: 560, onWidthChange: change, enabled }), { initialProps: { enabled: true } });
    const down = () => act(() => view.result.current.onPointerDown({ clientX: 800, pointerId: 1, button: 0, preventDefault: vi.fn() } as never));
    const pointer = (type: string, x: number) => {
      const event = new Event(type, { bubbles: true });
      Object.assign(event, { clientX: x, pointerId: 1 });
      act(() => document.dispatchEvent(event));
    };
    down();
    pointer("pointermove", 780); pointer("pointermove", 760); pointer("pointermove", 740);
    expect(change).not.toHaveBeenCalled();
    tick(16);
    expect(change).toHaveBeenLastCalledWith(440);
    expect(change).toHaveBeenCalledOnce();
    pointer("pointerup", 710);
    expect(change).toHaveBeenLastCalledWith(470);
    expect(view.result.current.resizing).toBe(false);
    down(); pointer("pointermove", 200); act(() => window.dispatchEvent(new Event("blur")));
    expect(change).toHaveBeenLastCalledWith(560);
    expect(view.result.current.resizing).toBe(false);
    down(); pointer("pointermove", 750); view.rerender({ enabled: false });
    expect(view.result.current.resizing).toBe(false);
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
  it("releases background inert when a kept-alive workspace becomes hidden", async () => {
    function Modal({ hidden }: { hidden: boolean }) {
      const ref = useRef<HTMLDivElement | null>(null);
      useModalFocus({ active: true, open: true, scopeRef: ref, panelRef: ref, onClose: vi.fn() });
      return <><button data-testid="background">background</button><section hidden={hidden}><div ref={ref}><button>close</button></div></section></>;
    }
    const view = render(<Modal hidden={false} />);
    expect(screen.getByTestId("background").hasAttribute("inert")).toBe(true);
    view.rerender(<Modal hidden />);
    await act(async () => {});
    expect(screen.getByTestId("background").hasAttribute("inert")).toBe(false);
  });
});
