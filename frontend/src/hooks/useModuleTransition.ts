import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";
import { motionDuration, motionEasing } from "../lib/motion";
import { waitForGuard, type NavigationGuard, type NavigationTarget } from "./useGuardedNavigation";
import { useReducedMotion } from "./useReducedMotion";

export type ModulePhase = "idle" | "guarding" | "exiting" | "blank" | "entering";
export type ModuleNavigationRequest = {
  target: NavigationTarget;
  href: string;
  kind?: "module" | "command";
  source?: "navigation" | "state" | "history";
};
type ModuleTransitionSnapshot = {
  phase: ModulePhase;
  blocked: boolean;
  exitRevision: number;
  pendingTarget: NavigationTarget | null;
  showPending: boolean;
};
export type ModuleTransitionView = ModuleTransitionSnapshot & {
  contentRef: RefObject<HTMLDivElement | null>;
  mainRef: RefObject<HTMLElement | null>;
  subscribe: (listener: () => void) => () => void;
  getSnapshot: () => ModuleTransitionSnapshot;
  cancel: () => void;
  setCovered: (covered: boolean) => void;
};

/** One transaction owns the guard, old-page exit, route commit and new-page entry.
 * No retained React snapshots or duplicate workspaces: the caller commits its
 * normal route state only when this existing content node is fully transparent.
 */
export function useModuleTransition<R extends ModuleNavigationRequest>({ activeModule, contentRef, mainRef, guard, commit }: {
  activeModule: string;
  contentRef: RefObject<HTMLDivElement | null>;
  mainRef: RefObject<HTMLElement | null>;
  guard: (request: R) => NavigationGuard | undefined;
  // Returns whether this transaction owns a new (as yet unseen) history entry.
  commit: (request: R, replaceUnseen: boolean) => boolean;
}) {
  const reduced = useReducedMotion();
  const options = useRef({ guard, commit, reduced });
  options.current = { guard, commit, reduced };
  const currentModule = useRef(activeModule);
  currentModule.current = activeModule;
  const mounted = useRef(true);
  const sequence = useRef(0);
  const pending = useRef<{ id: number; request: R; approved: boolean; committed: boolean } | null>(null);
  const phaseRef = useRef<ModulePhase>("idle");
  // Only AppShell subscribes to visual changes. Running a fade must not render
  // all business pages again at every phase; App renders only on route commits.
  const snapshot = useRef<ModuleTransitionSnapshot>({ phase: "idle", blocked: false, exitRevision: 0, pendingTarget: null, showPending: false });
  const subscribers = useRef(new Set<() => void>());
  const subscribe = useCallback((listener: () => void) => {
    subscribers.current.add(listener);
    return () => { subscribers.current.delete(listener); };
  }, []);
  const getSnapshot = useCallback(() => snapshot.current, []);
  const publish = (patch: Partial<ModuleTransitionSnapshot>) => {
    snapshot.current = { ...snapshot.current, ...patch };
    subscribers.current.forEach((listener) => listener());
  };
  const setPhase = (value: ModulePhase) => publish({ phase: value });
  const setBlocked = (value: boolean) => publish({ blocked: value });
  const setShowPending = (value: boolean) => publish({ showPending: value });
  const setExitRevision = (update: (value: number) => number) => publish({ exitRevision: update(snapshot.current.exitRevision) });
  const [commitRevision, setCommitRevision] = useState(0);
  const feedbackTimer = useRef<number | undefined>(undefined);
  const setPendingTarget = (target: NavigationTarget | null) => {
    const changed = snapshot.current.pendingTarget?.id !== target?.id;
    if (changed || !target) {
      window.clearTimeout(feedbackTimer.current);
      feedbackTimer.current = undefined;
      publish({ pendingTarget: target, showPending: false });
      if (target) feedbackTimer.current = window.setTimeout(() => {
        feedbackTimer.current = undefined;
        if (mounted.current && snapshot.current.pendingTarget?.id === target.id) setShowPending(true);
      }, motionDuration(contentRef.current, "feedbackDelay"));
    } else publish({ pendingTarget: target });
  };
  const animation = useRef<Animation | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const frame = useRef<number | undefined>(undefined);
  const cancelGuard = useRef<(() => void) | null>(null);
  const unseenEntry = useRef(false);
  const covered = useRef(false);
  const enterWhenUncovered = useRef<(() => void) | null>(null);
  const focusOnFinish = useRef(false);

  const changePhase = (value: ModulePhase) => {
    phaseRef.current = value;
    if (contentRef.current) contentRef.current.dataset.modulePhase = value;
    setPhase(value);
  };
  const opacity = () => {
    const raw = contentRef.current ? Number.parseFloat(getComputedStyle(contentRef.current).opacity) : 1;
    return Number.isFinite(raw) ? raw : 1;
  };
  const writeOpacity = (value: number) => { if (contentRef.current) contentRef.current.style.opacity = String(value); };
  const stopVisual = useCallback(() => {
    window.clearTimeout(timer.current);
    if (frame.current !== undefined) window.cancelAnimationFrame(frame.current);
    timer.current = undefined;
    frame.current = undefined;
    enterWhenUncovered.current = null;
    const running = animation.current;
    if (running) {
      if (contentRef.current) contentRef.current.style.opacity = getComputedStyle(contentRef.current).opacity || "1";
      running.onfinish = null;
      running.oncancel = null;
      running.cancel();
      animation.current = null;
    }
  }, [contentRef]);

  function lock() {
    const element = contentRef.current;
    if (element) {
      // Modal listeners consult this synchronously, before their effect cleanup.
      element.dataset.moduleTransitioning = "true";
      if (element.contains(document.activeElement) || document.activeElement?.closest('[data-module-owned-portal]')) {
        mainRef.current?.focus({ preventScroll: true });
      }
      element.setAttribute("inert", "");
      element.setAttribute("aria-hidden", "true");
    }
    setBlocked(true);
    focusOnFinish.current = true;
  }

  function finish() {
    stopVisual();
    writeOpacity(1);
    const element = contentRef.current;
    element?.removeAttribute("inert");
    element?.removeAttribute("aria-hidden");
    element?.removeAttribute("data-module-transitioning");
    unseenEntry.current = false;
    pending.current = null;
    setPendingTarget(null);
    setShowPending(false);
    setBlocked(false);
    changePhase("idle");
    if (focusOnFinish.current && !covered.current && document.visibilityState !== "hidden") {
      focusOnFinish.current = false;
      mainRef.current?.focus({ preventScroll: true });
    }
  }

  function skipMotion() {
    return options.current.reduced || document.visibilityState === "hidden"
      || !contentRef.current || typeof contentRef.current.animate !== "function";
  }

  function commitPending() {
    const transaction = pending.current;
    if (!transaction || transaction.committed || !mounted.current) return;
    transaction.committed = true;
    const from = currentModule.current;
    currentModule.current = transaction.request.target.id;
    try {
      unseenEntry.current = options.current.commit(transaction.request, unseenEntry.current && transaction.request.source !== "history");
    } catch (error) {
      currentModule.current = from;
      console.error("Module navigation could not be committed", error);
      finish();
      return;
    }
    setCommitRevision((value) => value + 1);
  }

  function settleWithoutMotion() {
    stopVisual();
    if (pending.current?.approved) commitPending();
    if (!pending.current || pending.current.approved) finish();
    else {
      // A real leave guard is never bypassed by an accessibility preference.
      writeOpacity(1);
      unseenEntry.current = false;
    }
  }

  function animateOpacity(to: number, token: "moduleExit" | "moduleEnter", done: () => void) {
    stopVisual();
    const element = contentRef.current;
    if (!element || skipMotion()) { settleWithoutMotion(); return; }
    const id = sequence.current;
    try {
      const instance = element.animate([{ opacity: opacity() }, { opacity: to }], {
        id: to === 0 ? "np-module-exit" : "np-module-enter",
        duration: motionDuration(element, token), easing: motionEasing(element, "module"), fill: "forwards"
      });
      animation.current = instance;
      const complete = () => {
        if (!mounted.current || sequence.current !== id || animation.current !== instance) return;
        window.clearTimeout(timer.current);
        timer.current = undefined;
        writeOpacity(to);
        animation.current = null;
        instance.onfinish = null;
        instance.oncancel = null;
        instance.cancel();
        done();
      };
      instance.onfinish = complete;
      instance.oncancel = () => {
        if (animation.current === instance && sequence.current === id) settleWithoutMotion();
      };
      const armFallback = () => {
        if (animation.current !== instance || sequence.current !== id) return;
        window.clearTimeout(timer.current);
        timer.current = window.setTimeout(complete, motionDuration(element, token) + 80);
      };
      armFallback();
      // WAAPI can remain pending until the first paint. Its startup delay must
      // not consume the visual interval; retain the initial watchdog if
      // readiness never arrives, and re-arm it at the actual timeline start.
      instance.ready?.then(armFallback, () => {
        if (animation.current === instance && sequence.current === id) settleWithoutMotion();
      });
    } catch { settleWithoutMotion(); }
  }

  function enter() {
    if (skipMotion()) { settleWithoutMotion(); return; }
    if (covered.current) { enterWhenUncovered.current = enter; return; }
    enterWhenUncovered.current = null;
    unseenEntry.current = false;
    writeOpacity(0);
    changePhase("entering");
    animateOpacity(1, "moduleEnter", finish);
  }

  function restore() {
    stopVisual();
    setPendingTarget(null);
    setShowPending(false);
    if (skipMotion() || opacity() >= 0.999) { finish(); return; }
    // Recovery is an entry too: once this page starts appearing, later
    // navigation must preserve its history even if recovery is interrupted.
    unseenEntry.current = false;
    lock();
    changePhase("entering");
    animateOpacity(1, "moduleEnter", finish);
  }

  function approved() {
    const transaction = pending.current;
    if (!transaction) return;
    transaction.approved = true;
    setExitRevision((value) => value + 1);
    focusOnFinish.current = true;
    if (transaction.request.target.id === currentModule.current) {
      commitPending();
      restore();
      return;
    }
    if (skipMotion()) { settleWithoutMotion(); return; }
    lock();
    const blank = () => {
      writeOpacity(0);
      changePhase("blank");
      commitPending();
    };
    if (opacity() <= 0.001) blank();
    else { changePhase("exiting"); animateOpacity(0, "moduleExit", blank); }
  }

  function startGuard(request: R) {
    const check = options.current.guard(request);
    if (!check) { approved(); return; }
    changePhase("guarding");
    const transaction = pending.current;
    const wait = waitForGuard(check);
    cancelGuard.current = wait.cancel;
    void wait.promise.then((allowed) => {
      if (!mounted.current || pending.current !== transaction) return;
      cancelGuard.current = null;
      if (allowed) approved();
      else { pending.current = null; restore(); }
    });
  }

  function selectedCurrentModule() {
    // Selecting the visible module still dismisses mobile navigation. It does
    // not replay a fade (or restart an already running recovery/entry).
    setExitRevision((value) => value + 1);
    focusOnFinish.current = true;
    if (phaseRef.current === "idle") finish();
  }

  // Methods are stable for effects and bridge/history listeners; their body
  // always sees the latest props and transaction functions.
  const actions = useRef({ request: (_request: R) => {}, cancel: () => {}, settleWithoutMotion });
  actions.current = {
    settleWithoutMotion,
    cancel: () => {
      sequence.current += 1;
      cancelGuard.current?.();
      cancelGuard.current = null;
      pending.current = null;
      focusOnFinish.current = false;
      restore();
      // Explicit cancellation belongs to the caller (e.g. the mobile menu's
      // close button). A recovery fade must not later steal its restored focus.
      focusOnFinish.current = false;
    },
    request: (request: R) => {
      if (!mounted.current) return;
      // A job/query update belonging to the displayed page is not a new
      // navigation intent and must not replace an already pending module.
      if (request.source === "state") {
        // popstate has already moved the browser URL. A late update from the
        // outgoing page must not overwrite that snapshot while its guard runs.
        if (pending.current?.request.source === "history" && !pending.current.committed) return;
        if (request.target.id === currentModule.current) options.current.commit(request, unseenEntry.current);
        return;
      }
      const previous = pending.current;
      if (previous && !previous.committed) {
        if (request.source !== "history" && (previous.request.kind === "command" || request.kind === "command")) return;
        if (request.target.id === currentModule.current) {
          const replaceUnseen = unseenEntry.current && request.source !== "history";
          actions.current.cancel();
          options.current.commit(request, replaceUnseen);
          selectedCurrentModule();
          return;
        }
        previous.request = request;
        setPendingTarget(request.target);
        return;
      }
      if (request.target.id === currentModule.current && request.kind !== "command") {
        const replaceUnseen = unseenEntry.current && request.source !== "history";
        if (request.source === "history") actions.current.cancel();
        options.current.commit(request, replaceUnseen);
        selectedCurrentModule();
        return;
      }
      sequence.current += 1;
      stopVisual();
      cancelGuard.current?.();
      cancelGuard.current = null;
      // Browser history has its own entry; never replace it as an unseen push.
      if (request.source === "history") unseenEntry.current = false;
      pending.current = { id: sequence.current, request, approved: false, committed: false };
      setPendingTarget(request.kind === "command" ? null : request.target);
      startGuard(request);
    }
  };
  const request = useCallback((value: R) => actions.current.request(value), []);
  const cancel = useCallback(() => actions.current.cancel(), []);
  const setCovered = useCallback((value: boolean) => {
    covered.current = value;
    if (!value) {
      const continuation = enterWhenUncovered.current;
      enterWhenUncovered.current = null;
      continuation?.();
      if (phaseRef.current === "idle" && focusOnFinish.current && document.visibilityState !== "hidden") {
        focusOnFinish.current = false;
        mainRef.current?.focus({ preventScroll: true });
      }
    }
  }, [mainRef]);

  const phase = snapshot.current.phase;
  useLayoutEffect(() => {
    if (phase !== "blank" || !pending.current?.committed) return;
    const id = sequence.current;
    let gapFrame: number | undefined;
    let gapTimer: number | undefined;
    // Count the gap only after React committed the new (transparent) page and
    // the browser had a chance to paint that pose. No network readiness wait.
    frame.current = gapFrame = window.requestAnimationFrame(() => {
      frame.current = gapFrame = window.requestAnimationFrame(() => {
        frame.current = undefined;
        gapFrame = undefined;
        timer.current = gapTimer = window.setTimeout(() => {
          timer.current = undefined;
          gapTimer = undefined;
          if (mounted.current && sequence.current === id) enter();
        }, motionDuration(contentRef.current, "moduleGap"));
      });
    });
    return () => {
      if (gapFrame !== undefined) window.cancelAnimationFrame(gapFrame);
      window.clearTimeout(gapTimer);
    };
  // The transaction revision, not arbitrary page updates, starts a fresh gap.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase, commitRevision]);

  useLayoutEffect(() => {
    if (reduced && phaseRef.current !== "idle") actions.current.settleWithoutMotion();
  }, [reduced]);
  useEffect(() => {
    mounted.current = true;
    // Effect reconnection (including dev Fast Refresh) can preserve the DOM
    // after cleanup cancelled its transaction. Restore that page, never replay
    // a cancelled navigation or leave its old inert/opacity state behind.
    if (!pending.current && phaseRef.current !== "idle") actions.current.settleWithoutMotion();
    const visibility = () => {
      if (document.visibilityState === "hidden" && phaseRef.current !== "idle") actions.current.settleWithoutMotion();
    };
    document.addEventListener("visibilitychange", visibility);
    return () => {
      mounted.current = false;
      sequence.current += 1;
      document.removeEventListener("visibilitychange", visibility);
      cancelGuard.current?.();
      cancelGuard.current = null;
      pending.current = null;
      window.clearTimeout(feedbackTimer.current);
      stopVisual();
    };
  }, [stopVisual]);

  return {
    request, contentRef, mainRef, subscribe, getSnapshot, cancel, setCovered,
    get phase() { return snapshot.current.phase; },
    get blocked() { return snapshot.current.blocked; },
    get exitRevision() { return snapshot.current.exitRevision; },
    get pendingTarget() { return snapshot.current.pendingTarget; },
    get showPending() { return snapshot.current.showPending; }
  };
}
