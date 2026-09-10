// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import { useRef, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useModuleTransition, type ModuleNavigationRequest } from "./useModuleTransition";

type Request = ModuleNavigationRequest;
type MockAnimation = { onfinish: (() => void) | null; oncancel: (() => void) | null; cancel: ReturnType<typeof vi.fn> };
let reduce: boolean;
let listeners: Set<() => void>;
let animations: { instance: MockAnimation; frames: Keyframe[]; options: KeyframeAnimationOptions }[];
let originalAnimate: PropertyDescriptor | undefined;
beforeEach(() => {
  vi.useFakeTimers();
  reduce = false;
  listeners = new Set();
  animations = [];
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => window.setTimeout(() => callback(performance.now()), 16));
  vi.spyOn(window, "cancelAnimationFrame").mockImplementation((id) => window.clearTimeout(id));
  vi.stubGlobal("matchMedia", (query: string) => ({
    get matches() { return query.includes("prefers-reduced-motion") && reduce; },
    addEventListener: (_: string, fn: () => void) => listeners.add(fn),
    removeEventListener: (_: string, fn: () => void) => listeners.delete(fn)
  }));
  originalAnimate = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "animate");
  Object.defineProperty(HTMLElement.prototype, "animate", { configurable: true, value: vi.fn((frames, options) => {
    const instance: MockAnimation = { onfinish: null, oncancel: null, cancel: vi.fn() };
    animations.push({ instance, frames, options });
    return instance;
  }) });
});
afterEach(() => {
  cleanup();
  if (originalAnimate) Object.defineProperty(HTMLElement.prototype, "animate", originalAnimate);
  else Reflect.deleteProperty(HTMLElement.prototype, "animate");
  vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals();
});
const target = (id: string, extra: Partial<Request> = {}): Request => ({ target: { id, label: id.toUpperCase() }, href: `/${id}`, ...extra });
const tick = (ms: number) => act(() => vi.advanceTimersByTime(ms));
const end = (index = animations.length - 1) => act(() => animations[index].instance.onfinish?.());
const reduced = () => {
  act(() => { reduce = true; listeners.forEach((fn) => fn()); });
  // jsdom queues a 0ms selectionchange when focus leaves the editor.
  tick(0);
};

function setup(check?: () => Promise<void | boolean>) {
  const committed = vi.fn((_request: Request, _replace: boolean) => {});
  let control!: ReturnType<typeof useModuleTransition<Request>>;
  function Harness() {
    const [module, setModule] = useState("a");
    const contentRef = useRef<HTMLDivElement | null>(null);
    const mainRef = useRef<HTMLElement | null>(null);
    control = useModuleTransition({ activeModule: module, contentRef, mainRef, guard: () => check,
      commit: (request, replace) => {
        committed(request, replace); setModule(request.target.id);
        return request.source !== "history";
      }
    });
    return <main ref={mainRef} tabIndex={-1} data-testid="main">
      <div ref={contentRef} data-testid="content" data-module={module} data-phase={control.phase}>
        <input aria-label="editor" /><iframe title="retained canvas" />
      </div>
    </main>;
  }
  const view = render(<Harness />);
  return { view, committed, get control() { return control; }, go: (id: string, extra?: Partial<Request>) => act(() => control.request(target(id, extra))) };
}

describe("serial module transition", () => {
  it("keeps the old route for 400ms, paints a 200ms blank, then fades the new page from zero for 400ms", () => {
    const h = setup();
    const content = screen.getByTestId("content");
    const iframe = screen.getByTitle("retained canvas");
    screen.getByRole("textbox").focus();
    expect(animations).toHaveLength(0);
    h.go("b");
    expect(h.control.phase).toBe("exiting");
    expect(content.hasAttribute("inert")).toBe(true);
    expect(document.activeElement).toBe(screen.getByTestId("main"));
    expect(animations[0]).toMatchObject({ frames: [{ opacity: 1 }, { opacity: 0 }], options: { duration: 400, easing: "ease-in-out", id: "np-module-exit" } });
    tick(399);
    expect(content.dataset.module).toBe("a");
    expect(h.committed).not.toHaveBeenCalled();
    tick(1); end();
    expect(h.control.phase).toBe("blank");
    expect(content.dataset.module).toBe("b");
    expect(content.style.opacity).toBe("0");
    expect(h.committed).toHaveBeenCalledOnce();
    tick(32); tick(199);
    expect(h.control.phase).toBe("blank");
    tick(1);
    expect(h.control.phase).toBe("entering");
    expect(animations[1]).toMatchObject({ frames: [{ opacity: 0 }, { opacity: 1 }], options: { duration: 400, easing: "ease-in-out", id: "np-module-enter" } });
    tick(400); end();
    expect(h.control.phase).toBe("idle");
    expect(content.style.opacity).toBe("1");
    expect(content.hasAttribute("inert")).toBe(false);
    expect(content.hasAttribute("aria-hidden")).toBe(false);
    expect(h.control.pendingTarget).toBeNull();
    expect(screen.getByTitle("retained canvas")).toBe(iframe);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("coalesces targets across one guard and exit without restarting either", async () => {
    let resolve!: (allowed: boolean) => void;
    const guard = vi.fn(() => new Promise<boolean>((done) => { resolve = done; }));
    const h = setup(guard);
    h.go("b"); tick(120);
    expect(h.control.showPending).toBe(true);
    h.go("c"); h.go("d");
    expect(guard).toHaveBeenCalledOnce();
    await act(async () => resolve(true));
    tick(200); h.go("b"); h.go("c");
    expect(animations).toHaveLength(1);
    expect(guard).toHaveBeenCalledOnce();
    tick(200); end();
    expect(h.committed).toHaveBeenCalledExactlyOnceWith(target("c"), false);
    expect(h.control.pendingTarget?.id).toBe("c");
  });

  it("replaces unseen blank-phase history, restarts only the gap and ignores stale callbacks", () => {
    const h = setup();
    h.go("b");
    const obsolete = animations[0].instance.onfinish!;
    end(); tick(32); tick(100);
    h.go("c"); h.go("d");
    expect(animations).toHaveLength(1);
    expect(h.committed.mock.calls.map(([r, replace]) => [r.target.id, replace])).toEqual([["b", false], ["c", true], ["d", true]]);
    act(obsolete);
    expect(h.committed).toHaveBeenCalledTimes(3);
    tick(232);
    expect(animations.at(-1)?.frames).toEqual([{ opacity: 0 }, { opacity: 1 }]);
    expect(screen.getByTestId("content").dataset.module).toBe("d");
  });

  it("interrupts entry from its current opacity but always starts the next page at zero", () => {
    const h = setup();
    h.go("b"); end(); tick(232);
    screen.getByTestId("content").style.opacity = "0.42";
    const obsolete = animations[1].instance.onfinish!;
    h.go("c"); h.go("d");
    expect(animations[1].instance.cancel).toHaveBeenCalledOnce();
    expect(animations[2].frames).toEqual([{ opacity: 0.42 }, { opacity: 0 }]);
    act(obsolete);
    expect(h.control.phase).toBe("exiting");
    end(); tick(232);
    expect(animations[3].frames).toEqual([{ opacity: 0 }, { opacity: 1 }]);
    expect(h.committed.mock.calls.map(([r, replace]) => [r.target.id, replace])).toEqual([["b", false], ["d", false]]);
  });

  it("rejects a guard without disappearing and recovers a rejected interrupted entry", async () => {
    const guard = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true).mockResolvedValueOnce(false);
    const h = setup(guard);
    h.go("b"); await act(async () => {});
    expect(animations).toHaveLength(0);
    expect(h.committed).not.toHaveBeenCalled();
    expect(h.control.phase).toBe("idle");
    h.go("b"); await act(async () => {}); end(); tick(232);
    screen.getByTestId("content").style.opacity = "0.3";
    h.go("c"); await act(async () => {});
    expect(animations.at(-1)?.frames).toEqual([{ opacity: 0.3 }, { opacity: 1 }]);
    end();
    expect(h.control.phase).toBe("idle");
    expect(h.committed).toHaveBeenCalledOnce();
  });

  it("keeps a recovered page's history once a rejected blank-phase navigation starts showing it", async () => {
    const guard = vi.fn().mockResolvedValueOnce(true).mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    const h = setup(guard);
    h.go("b"); await act(async () => {}); end();
    h.go("c"); await act(async () => {});
    expect(h.control.phase).toBe("entering");
    screen.getByTestId("content").style.opacity = "0.3";
    h.go("d"); await act(async () => {}); end();
    expect(h.committed.mock.calls.map(([request, replace]) => [request.target.id, replace])).toEqual([["b", false], ["d", false]]);
  });

  it.each(["state", "navigation"] as const)("replaces rather than pushes %s URL updates while a committed page is still unseen", (source) => {
    const h = setup(); h.go("b"); end();
    h.go("b", { source, href: "/b?jobId=restored" });
    expect(h.control.phase).toBe("blank");
    expect(animations).toHaveLength(1);
    expect(h.committed).toHaveBeenLastCalledWith(target("b", { source, href: "/b?jobId=restored" }), true);
    tick(232); end();
    h.go("b", { source, href: "/b?jobId=selected" });
    expect(h.committed).toHaveBeenLastCalledWith(target("b", { source, href: "/b?jobId=selected" }), false);
  });

  it("replaces a still-unseen URL before recovering it when a new guard is cancelled", async () => {
    let resolve!: (allowed: boolean) => void;
    const guard = vi.fn().mockResolvedValueOnce(true).mockImplementationOnce(() => new Promise<boolean>((done) => { resolve = done; }));
    const h = setup(guard);
    h.go("b"); await act(async () => {}); end();
    h.go("c");
    h.go("b", { href: "/b?query=updated" });
    expect(h.committed).toHaveBeenLastCalledWith(target("b", { href: "/b?query=updated" }), true);
    await act(async () => resolve(true));
    end();
    expect(h.committed).toHaveBeenCalledTimes(2);
    expect(h.control.phase).toBe("idle");
  });

  it.each(["throw", "reject", "timeout"])("preserves the %s guard fail-open rule and clears pending feedback", async (mode) => {
    reduce = true;
    const guard = vi.fn(() => {
      if (mode === "throw") throw new Error("sync guard failure");
      return mode === "reject" ? Promise.reject(new Error("async failure")) : new Promise<void>(() => {});
    });
    const h = setup(guard);
    h.go("b");
    if (mode === "timeout") { tick(1499); expect(h.committed).not.toHaveBeenCalled(); tick(1); }
    await act(async () => {});
    expect(h.committed).toHaveBeenCalledOnce();
    expect(h.control.phase).toBe("idle");
    expect(h.control.pendingTarget).toBeNull();
    expect(h.control.showPending).toBe(false);
  });

  it("cancels an uncommitted target when selecting the original module", () => {
    const h = setup(); h.go("b");
    screen.getByTestId("content").style.opacity = "0.6";
    h.go("a");
    expect(animations.at(-1)?.frames).toEqual([{ opacity: 0.6 }, { opacity: 1 }]);
    end();
    expect(h.committed.mock.calls.every(([r]) => r.target.id === "a")).toBe(true);
    expect(h.control.phase).toBe("idle");
  });

  it("commands cannot be replaced or replayed before their commit", () => {
    const h = setup();
    h.go("b", { kind: "command" }); h.go("c"); h.go("b", { kind: "command" });
    end();
    expect(h.committed).toHaveBeenCalledExactlyOnceWith(target("b", { kind: "command" }), false);
    h.go("d");
    expect(h.committed).toHaveBeenCalledTimes(2);
  });

  it("history supersedes a pending navigation, never replaces an existing history entry", () => {
    const h = setup(); h.go("b");
    h.go("c", { source: "history", href: "/c?jobId=123" });
    end();
    expect(h.committed).toHaveBeenCalledExactlyOnceWith(target("c", { source: "history", href: "/c?jobId=123" }), false);
  });

  it("same-module job updates do not restart an exit or replace its target", () => {
    const h = setup(); h.go("b");
    h.go("a", { source: "state", href: "/a?jobId=123" });
    expect(animations).toHaveLength(1);
    expect(h.control.phase).toBe("exiting");
    end();
    expect(h.committed.mock.calls.map(([r]) => r.target.id)).toEqual(["a", "b"]);
    h.go("a", { source: "state", href: "/a?jobId=late-response" });
    expect(h.committed).toHaveBeenCalledTimes(2);
  });

  it("a late job update cannot overwrite the browser's pending history snapshot", async () => {
    let resolve!: () => void;
    const h = setup(() => new Promise<void>((done) => { resolve = done; }));
    h.go("b", { source: "history", href: "/b?jobId=latest" });
    h.go("a", { source: "state", href: "/a?jobId=late-response" });
    expect(h.committed).not.toHaveBeenCalled();
    await act(async () => resolve());
    end();
    expect(h.committed).toHaveBeenCalledExactlyOnceWith(target("b", { source: "history", href: "/b?jobId=latest" }), false);
  });

  it.each(["exiting", "blank", "entering"])("settles runtime reduced motion during %s with no residual lock", (stage) => {
    const h = setup(); h.go("b");
    if (stage !== "exiting") end();
    if (stage === "entering") tick(232);
    reduced();
    expect(h.committed).toHaveBeenCalledOnce();
    expect(h.control.phase).toBe("idle");
    expect(screen.getByTestId("content").style.opacity).toBe("1");
    expect(screen.getByTestId("content").hasAttribute("inert")).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("does not bypass an unfinished guard when motion is reduced", async () => {
    let resolve!: () => void;
    const h = setup(() => new Promise<void>((done) => { resolve = done; }));
    h.go("b"); reduced();
    expect(h.committed).not.toHaveBeenCalled();
    await act(async () => resolve());
    expect(h.committed).toHaveBeenCalledOnce();
    expect(animations).toHaveLength(0);
  });

  it("preserves a blank page's history after reduced motion reveals it while its leave guard is pending", async () => {
    let resolve!: () => void;
    const guard = vi.fn().mockResolvedValueOnce(true).mockImplementationOnce(() => new Promise<void>((done) => { resolve = done; }));
    const h = setup(guard);
    h.go("b"); await act(async () => {}); end();
    h.go("c"); reduced();
    expect(screen.getByTestId("content").style.opacity).toBe("1");
    expect(h.committed).toHaveBeenCalledOnce();
    await act(async () => resolve());
    expect(h.committed.mock.calls.map(([request, replace]) => [request.target.id, replace])).toEqual([["b", false], ["c", false]]);
    expect(h.control.phase).toBe("idle");
  });

  it("waits until mobile navigation uncovers the page before entering or focusing it", () => {
    const h = setup();
    act(() => h.control.setCovered(true));
    h.go("b"); end(); tick(232);
    expect(h.control.phase).toBe("blank");
    expect(animations).toHaveLength(1);
    act(() => h.control.setCovered(false));
    expect(h.control.phase).toBe("entering");
    end();
    expect(document.activeElement).toBe(screen.getByTestId("main"));
  });

  it.each(["current", "cancel-target"])("closes mobile navigation when selecting the original module (%s) without replaying motion", async (mode) => {
    let resolve!: () => void;
    const h = setup(() => new Promise<void>((done) => { resolve = done; }));
    act(() => h.control.setCovered(true));
    if (mode === "cancel-target") h.go("b");
    h.go("a");
    expect(h.control.exitRevision).toBe(1);
    expect(h.control.phase).toBe("idle");
    expect(animations).toHaveLength(0);
    act(() => h.control.setCovered(false));
    expect(document.activeElement).toBe(screen.getByTestId("main"));
    if (resolve) await act(async () => resolve());
    expect(h.committed).toHaveBeenCalledExactlyOnceWith(target("a"), false);
  });

  it("does not steal the caller's restored focus after cancelling a partially faded page", () => {
    const h = setup(); h.go("b");
    screen.getByTestId("content").style.opacity = "0.4";
    act(() => h.control.cancel());
    const menuButton = document.createElement("button");
    document.body.append(menuButton);
    menuButton.focus();
    end();
    expect(h.control.phase).toBe("idle");
    expect(document.activeElement).toBe(menuButton);
    expect(h.committed).not.toHaveBeenCalled();
    menuButton.remove();
  });

  it("measures the animation watchdog from readiness", async () => {
    let ready!: () => void;
    const animationReady = new Promise<void>((resolve) => { ready = resolve; });
    vi.mocked(HTMLElement.prototype.animate).mockImplementationOnce((frames, options) => {
      const instance = { onfinish: null, oncancel: null, cancel: vi.fn(), ready: animationReady };
      animations.push({ instance, frames: frames as Keyframe[], options: options as KeyframeAnimationOptions });
      return instance as unknown as Animation;
    });
    const h = setup(); h.go("b");
    tick(100);
    await act(async () => ready());
    tick(479);
    expect(h.committed).not.toHaveBeenCalled();
    tick(1);
    expect(h.committed).toHaveBeenCalledOnce();
    h.go("c"); tick(232); end();
    expect(h.control.phase).toBe("idle");
    expect(screen.getByTestId("content").dataset.module).toBe("c");
  });

  it("ignores animation readiness delivered after cancellation", async () => {
    let ready!: () => void;
    const animationReady = new Promise<void>((resolve) => { ready = resolve; });
    vi.mocked(HTMLElement.prototype.animate).mockImplementationOnce((frames, options) => {
      const instance = { onfinish: null, oncancel: null, cancel: vi.fn(), ready: animationReady };
      animations.push({ instance, frames: frames as Keyframe[], options: options as KeyframeAnimationOptions });
      return instance as unknown as Animation;
    });
    const h = setup(); h.go("b");
    act(() => h.control.cancel());
    await act(async () => ready());
    tick(1000);
    expect(h.control.phase).toBe("idle");
    expect(h.committed).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("an unexpected animation cancellation exposes the approved target without a residual lock", () => {
    const h = setup(); h.go("b");
    act(() => animations[0].instance.oncancel?.());
    expect(h.committed).toHaveBeenCalledExactlyOnceWith(target("b"), false);
    expect(h.control.phase).toBe("idle");
    expect(screen.getByTestId("content").hasAttribute("inert")).toBe(false);
    expect(screen.getByTestId("content").style.opacity).toBe("1");
  });

  it("has a short fallback for missing finish events, and no indefinite blank on animation failure", () => {
    const h = setup(); h.go("b");
    tick(480); tick(232); tick(480);
    expect(h.control.phase).toBe("idle");
    vi.mocked(HTMLElement.prototype.animate).mockImplementation(() => { throw new Error("animation unavailable"); });
    h.go("c");
    expect(h.control.phase).toBe("idle");
    expect(h.committed).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("content").style.opacity).toBe("1");
  });

  it("settles a hidden tab and cleans up all asynchronous work on unmount", async () => {
    const h = setup(); h.go("b");
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    expect(h.control.phase).toBe("idle");
    expect(h.committed).toHaveBeenCalledOnce();
    h.view.unmount();
    let resolve!: () => void;
    const pending = setup(() => new Promise<void>((done) => { resolve = done; }));
    pending.go("b"); pending.view.unmount();
    await act(async () => resolve());
    expect(pending.committed).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("effect reconnection restores retained DOM without committing a cancelled navigation", () => {
    const committed = vi.fn(() => false);
    let control!: ReturnType<typeof useModuleTransition<Request>>;
    function Harness({ contentRef }: { contentRef: { current: HTMLDivElement | null } }) {
      const mainRef = useRef<HTMLElement | null>(null);
      control = useModuleTransition({ activeModule: "a", contentRef, mainRef, guard: () => undefined, commit: committed });
      return <main ref={mainRef} tabIndex={-1}><div ref={contentRef} data-testid="reconnected">content</div></main>;
    }
    const view = render(<Harness contentRef={{ current: null }} />);
    act(() => control.request(target("b")));
    const node = screen.getByTestId("reconnected");
    expect(node.hasAttribute("inert")).toBe(true);
    view.rerender(<Harness contentRef={{ current: null }} />);
    expect(screen.getByTestId("reconnected")).toBe(node);
    expect(control.phase).toBe("idle");
    expect(node.style.opacity).toBe("1");
    expect(node.hasAttribute("inert")).toBe(false);
    expect(committed).not.toHaveBeenCalled();
  });
});
