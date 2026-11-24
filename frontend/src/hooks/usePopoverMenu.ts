import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent } from "react";

const ITEM_SELECTOR = '[role="menuitem"], [role="menuitemradio"], [role="menuitemcheckbox"]';

function menuItems(root: HTMLElement | null) {
  return [...(root?.querySelectorAll<HTMLElement>(ITEM_SELECTOR) ?? [])].filter(
    (item) => !item.hasAttribute("disabled") && item.getAttribute("aria-disabled") !== "true"
  );
}

/**
 * One behaviour for every popover menu on a page (examples, export, comparison): the trigger opens
 * it (click, ArrowDown or ArrowUp), focus lands on the checked item or the first one, arrows, Home
 * and End move, Escape closes and returns focus to the trigger, and a pointer press or focus outside
 * closes it. Items are rendered with tabIndex -1, so Tab leaves the menu instead of walking it.
 */
export function usePopoverMenu() {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuId = useId();

  const close = useCallback((restoreFocus = true) => {
    setOpen(false);
    if (restoreFocus) triggerRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!open) return;
    const root = rootRef.current;
    // Long menus scroll inside the room left below the trigger instead of running off the page.
    const menu = root?.querySelector<HTMLElement>('[role="menu"]');
    const trigger = triggerRef.current;
    if (menu && trigger) {
      const room = window.innerHeight - trigger.getBoundingClientRect().bottom - 32;
      menu.style.setProperty("--np-pop-room", `${Math.max(160, Math.round(room))}px`);
    }
    const items = menuItems(root);
    (items.find((item) => item.getAttribute("aria-checked") === "true") ?? items[0])?.focus();

    function handlePointer(event: PointerEvent) {
      if (event.target instanceof Node && !root?.contains(event.target)) setOpen(false);
    }
    function handleFocusOut(event: FocusEvent) {
      const next = event.relatedTarget;
      if (next instanceof Node && !root?.contains(next)) setOpen(false);
    }
    document.addEventListener("pointerdown", handlePointer);
    root?.addEventListener("focusout", handleFocusOut);
    return () => {
      document.removeEventListener("pointerdown", handlePointer);
      root?.removeEventListener("focusout", handleFocusOut);
    };
  }, [open]);

  function onTriggerKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setOpen(true);
    } else if (event.key === "Escape" && open) {
      event.preventDefault();
      close();
    }
  }

  function onMenuKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      return;
    }
    if (event.key === "Tab") {
      setOpen(false);
      return;
    }
    const items = menuItems(rootRef.current);
    const index = items.indexOf(document.activeElement as HTMLElement);
    const next =
      event.key === "ArrowDown" ? (index + 1) % items.length
        : event.key === "ArrowUp" ? (index - 1 + items.length) % items.length
          : event.key === "Home" ? 0
            : event.key === "End" ? items.length - 1
              : null;
    if (next === null || items.length === 0) return;
    event.preventDefault();
    items[next]?.focus();
  }

  return {
    open,
    close,
    rootRef,
    triggerProps: {
      ref: triggerRef,
      "aria-haspopup": "menu" as const,
      "aria-expanded": open,
      "aria-controls": open ? menuId : undefined,
      onClick: () => setOpen((current) => !current),
      onKeyDown: onTriggerKeyDown
    },
    menuProps: { id: menuId, role: "menu" as const, onKeyDown: onMenuKeyDown }
  };
}
