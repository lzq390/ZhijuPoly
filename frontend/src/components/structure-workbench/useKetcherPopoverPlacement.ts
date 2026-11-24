import { useEffect, type RefObject } from "react";

const EDGE = 8;

function isPopoverRoot(node: Node): node is HTMLElement {
  return node instanceof HTMLElement && node.classList.contains("MuiPopover-root");
}

/**
 * Puts each Ketcher menu under `host` where MUI meant it to be (viewport coordinates), then keeps
 * it inside the host, whose overflow would otherwise clip it and make the clipped part unclickable.
 */
export function placeKetcherPopovers(host: HTMLElement) {
  const bounds = host.getBoundingClientRect();
  for (const root of host.querySelectorAll<HTMLElement>(".MuiPopover-root")) {
    const paper = root.querySelector<HTMLElement>(".MuiPaper-root");
    if (!paper) continue;
    // The root is fixed with inset 0, so its box starts at the origin of its containing block.
    const origin = root.getBoundingClientRect();
    let dx = -origin.left;
    let dy = -origin.top;
    // MUI writes the intended viewport position as top/left; sizes ignore its scale animation.
    const left = Number.parseFloat(paper.style.left);
    const top = Number.parseFloat(paper.style.top);
    if (Number.isFinite(left)) {
      const overRight = left + paper.offsetWidth - (bounds.right - EDGE);
      if (overRight > 0) dx -= Math.min(overRight, Math.max(0, left - (bounds.left + EDGE)));
    }
    if (Number.isFinite(top)) {
      const overBottom = top + paper.offsetHeight - (bounds.bottom - EDGE);
      if (overBottom > 0) dy -= Math.min(overBottom, Math.max(0, top - (bounds.top + EDGE)));
    }
    paper.style.translate = dx || dy ? `${Math.round(dx)}px ${Math.round(dy)}px` : "";
  }
}

/**
 * Ketcher places its own menus (zoom and others) with viewport coordinates but renders them inside
 * its root. In the workbench, the 2D/3D flip layers (transform) and container queries make an
 * ancestor the containing block for fixed elements, so those menus land offset by that ancestor's
 * position: off the trigger, clipped by the canvas stage, or off screen. This puts them back under
 * their triggers and inside the stage. The separate `translate` property leaves MUI's own
 * transform animation untouched.
 */
export function useKetcherPopoverPlacement(hostRef: RefObject<HTMLElement | null>) {
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const frames: number[] = [];
    let timer = 0;
    const place = () => placeKetcherPopovers(host);
    // MUI may write the position a frame after insertion, so check again then and after its
    // entering transition.
    const placeSoon = () => {
      place();
      frames.push(window.requestAnimationFrame(() => {
        place();
        frames.push(window.requestAnimationFrame(place));
      }));
      window.clearTimeout(timer);
      timer = window.setTimeout(place, 320);
    };
    // Only react when a popover is inserted; drawing on the canvas also adds nodes all the time.
    const observer = new MutationObserver((records) => {
      if (records.some((record) => [...record.addedNodes].some(isPopoverRoot))) placeSoon();
    });
    observer.observe(host, { childList: true, subtree: true });
    window.addEventListener("resize", place);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", place);
      frames.forEach((frame) => window.cancelAnimationFrame(frame));
      window.clearTimeout(timer);
    };
  }, [hostRef]);
}
