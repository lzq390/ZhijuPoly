import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import { motionDuration } from "../lib/motion";

export type NavigationTarget = { id: string; label: string };
export type NavigationGuard = (signal: AbortSignal) => Promise<void | boolean>;
const GUARD_TIMEOUT_MS = 1500;

export function waitForGuard(guard: NavigationGuard) {
  const controller = new AbortController();
  let cancel = () => {};
  const promise = new Promise<boolean>((resolve) => {
    let settled = false;
    let timer: number | undefined;
    const finish = (allowed: boolean, reason?: DOMException) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      // Deadline cleanup (including retiring an old editor) must finish before
      // routing is released. Cancellation uses a different reason and never
      // requests recovery of the page the user chose to keep.
      if (reason) controller.abort(reason);
      resolve(allowed);
    };
    cancel = () => finish(false, new DOMException("Navigation cancelled", "AbortError"));
    timer = window.setTimeout(() => finish(true, new DOMException("Navigation timed out", "TimeoutError")), GUARD_TIMEOUT_MS);
    try { Promise.resolve(guard(controller.signal)).then((value) => finish(value !== false), () => finish(true)); }
    catch { finish(true); }
  });
  return { promise, cancel: () => cancel() };
}

export function useGuardedNavigation({ beforeNavigate, onCommit, activeModule, containerRef }: {
  beforeNavigate?: NavigationGuard;
  onCommit: () => void;
  activeModule: string;
  containerRef: RefObject<HTMLElement | null>;
}) {
  const [pendingTarget, setPendingTarget] = useState<NavigationTarget | null>(null);
  const [showPending, setShowPending] = useState(false);
  const pending = useRef<{
    action: () => void; target?: NavigationTarget; cancel: () => void;
  } | null>(null);
  const commitRef = useRef(onCommit);
  commitRef.current = onCommit;
  const cancel = useCallback(() => {
    pending.current?.cancel();
    pending.current = null;
    setPendingTarget(null);
    setShowPending(false);
  }, []);
  useEffect(() => {
    setShowPending(false);
    if (!pendingTarget) return;
    const timer = window.setTimeout(() => setShowPending(true), motionDuration(containerRef.current, "feedbackDelay"));
    return () => window.clearTimeout(timer);
  }, [pendingTarget, containerRef]);
  useEffect(() => { cancel(); }, [activeModule, cancel]);
  useEffect(() => {
    window.addEventListener("popstate", cancel);
    return () => {
      window.removeEventListener("popstate", cancel);
      pending.current?.cancel();
      pending.current = null;
    };
  }, [cancel]);

  function navigate(action: () => void, target?: NavigationTarget) {
    const current = pending.current;
    if (current) {
      // Commands are single-shot, never queued/replayed as navigation targets.
      if (current.target && target) {
        current.action = action;
        current.target = target;
        setPendingTarget(target);
      }
      return;
    }
    if (!beforeNavigate) {
      action();
      commitRef.current();
      return;
    }
    const wait = waitForGuard(beforeNavigate);
    const request = { action, target, cancel: wait.cancel };
    pending.current = request;
    setPendingTarget(target ?? null);
    void wait.promise.then((allowed) => {
      if (pending.current !== request) return;
      pending.current = null;
      setPendingTarget(null);
      setShowPending(false);
      if (allowed) {
        request.action();
        commitRef.current();
      }
    });
  }
  return { navigate, cancel, pendingTarget, showPending };
}
