import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";

export function focusableWithin(container: HTMLElement) {
  return Array.from(container.querySelectorAll<HTMLElement>(
    'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
  )).filter((element) => !element.closest('[inert], [hidden], [aria-hidden="true"]'));
}

/** Modal constraint lasts until visual exit, including portals owned by the panel. */
export function useModalFocus({ active, open, scopeRef, panelRef, initialFocusRef, onClose, ownerId, global = true }: {
  active: boolean; open: boolean;
  scopeRef: RefObject<HTMLElement | null>; panelRef: RefObject<HTMLElement | null>;
  initialFocusRef?: RefObject<HTMLElement | null>;
  onClose: () => void; ownerId?: string;
  // Workbench overlays historically constrain only their own workspace; the
  // platform sidebar remains available for guarded module navigation.
  global?: boolean;
}) {
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const openRef = useRef(open);
  openRef.current = open;
  const [visible, setVisible] = useState(false);
  useLayoutEffect(() => {
    if (!active || !scopeRef.current) { setVisible(false); return; }
    const ancestors: HTMLElement[] = [];
    let parent = scopeRef.current.parentElement;
    while (parent) { ancestors.push(parent); parent = parent.parentElement; }
    const update = () => setVisible(!ancestors.some((element) =>
      element.hidden || element.hasAttribute("inert") || element.dataset.moduleTransitioning === "true"
      || element.getAttribute("aria-hidden") === "true" || getComputedStyle(element).display === "none"
    ));
    update();
    // Some workspaces are intentionally kept mounted while hidden by App.
    // Their modal must never keep the newly selected module inert.
    const observer = new MutationObserver(update);
    ancestors.forEach((element) => observer.observe(element, { attributes: true, attributeFilter: ["hidden", "inert", "data-module-transitioning", "aria-hidden", "class", "style"] }));
    return () => observer.disconnect();
  }, [active, scopeRef]);
  useEffect(() => {
    if (!active || !visible || !scopeRef.current) return;
    const altered: Array<[HTMLElement, boolean]> = [];
    let node: HTMLElement | null = scopeRef.current;
    while (global && node && node !== document.body) {
      for (const sibling of Array.from(node.parentElement?.children ?? [])) {
        if (!(sibling instanceof HTMLElement) || sibling === node || (ownerId && sibling.dataset.modalOwner === ownerId)) continue;
        altered.push([sibling, sibling.hasAttribute("inert")]);
        sibling.setAttribute("inert", "");
      }
      node = node.parentElement;
    }
    const owned = () => ownerId ? Array.from(document.querySelectorAll<HTMLElement>(`[data-modal-owner="${ownerId}"][role="menu"]`)) : [];
    const transitioning = () => Boolean(scopeRef.current?.closest('[data-module-transitioning="true"]'));
    const items = () => [panelRef.current, ...owned()].flatMap((root) => root ? focusableWithin(root) : []);
    function keydown(event: KeyboardEvent) {
      if (transitioning()) return;
      if (event.defaultPrevented) return;
      if (event.key === "Escape") {
        // The nested project menu owns the first Escape, irrespective of
        // document listener registration order.
        if (owned().some((root) => root.getAttribute("aria-hidden") !== "true")) return;
        event.preventDefault();
        closeRef.current();
      }
      if (event.key !== "Tab") return;
      const focusable = items();
      const index = focusable.indexOf(document.activeElement as HTMLElement);
      if (!openRef.current || !focusable.length) {
        event.preventDefault();
        return;
      }
      if (!global && index === -1) return;
      if (event.shiftKey && index <= 0) {
        event.preventDefault();
        focusable.at(-1)?.focus({ preventScroll: true });
      } else if (!event.shiftKey && (index === -1 || index === focusable.length - 1)) {
        event.preventDefault();
        focusable[0]?.focus({ preventScroll: true });
      }
    }
    function focusin(event: FocusEvent) {
      if (transitioning()) return;
      const target = event.target as Node;
      if (global && openRef.current && !panelRef.current?.contains(target) && !owned().some((root) => root.contains(target))) {
        (items()[0] ?? panelRef.current)?.focus({ preventScroll: true });
      }
    }
    document.addEventListener("keydown", keydown);
    document.addEventListener("focusin", focusin);
    return () => {
      document.removeEventListener("keydown", keydown);
      document.removeEventListener("focusin", focusin);
      for (const [element, wasInert] of altered) if (!wasInert) element.removeAttribute("inert");
    };
  }, [active, visible, scopeRef, panelRef, ownerId, global]);
  useEffect(() => {
    if (!active || !visible || !open) return;
    let frame = 0;
    const focusWhenVisible = () => {
      const panel = panelRef.current;
      if (!panel?.isConnected || !openRef.current || panel.closest('[inert], [hidden], [aria-hidden="true"], [data-module-transitioning="true"]')) return;
      if (panel.contains(document.activeElement) && document.activeElement !== panel) return;
      if (ownerId && document.querySelector(`[data-modal-owner="${ownerId}"][role="menu"]:not([aria-hidden="true"])`)) return;
      const target = initialFocusRef?.current ?? focusableWithin(panel)[0] ?? panel;
      // The presence state may settle before inherited CSS visibility does,
      // particularly with reduced motion. Focus on a still-hidden control is
      // ignored, so wait until the browser can accept it.
      const style = getComputedStyle(target);
      if (style.visibility !== "visible" || style.display === "none") {
        frame = window.requestAnimationFrame(focusWhenVisible);
        return;
      }
      target.focus({ preventScroll: true });
    };
    frame = window.requestAnimationFrame(() => { frame = window.requestAnimationFrame(focusWhenVisible); });
    return () => window.cancelAnimationFrame(frame);
  }, [active, visible, open, initialFocusRef, panelRef, ownerId]);
}
