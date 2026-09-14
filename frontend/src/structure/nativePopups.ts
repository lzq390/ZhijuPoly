/** Associate body portals only when an editor control opened/anchors them. */
export function ownNativePopups(root: HTMLElement, session: string) {
  const owned = new Set<HTMLElement>();
  let fromEditor = false;
  const visible = () => root.isConnected && root.getClientRects().length > 0 && !root.closest('[inert], [hidden], [aria-hidden="true"]');
  const interaction = (event: Event) => {
    const target = event.target;
    fromEditor = target instanceof Element && visible() &&
      (root.contains(target) || target.closest('[data-ketcher-owner]')?.getAttribute('data-ketcher-owner') === session);
  };
  const events = ["pointerdown", "keydown", "focusin"];
  events.forEach(event => document.addEventListener(event, interaction, true));
  const mark = (node: HTMLElement) => {
    if (root.contains(node) || node.closest('[data-ketcher-owner]') || !visible()) return;
    const id = node.id || node.querySelector<HTMLElement>("[id]")?.id;
    const anchored = id && [...root.querySelectorAll('[aria-controls], [aria-describedby]')].some(anchor =>
      [anchor.getAttribute('aria-controls'), anchor.getAttribute('aria-describedby')].some(value => value?.split(' ').includes(id)));
    if (!fromEditor && !anchored) return;
    node.dataset.ketcherOwner = session;
    owned.add(node);
  };
  const observer = new MutationObserver(records => {
    for (const record of records) for (const node of record.addedNodes) {
      if (!(node instanceof HTMLElement)) continue;
      const selector = '.MuiModal-root, .MuiPopper-root, .MuiTooltip-popper';
      if (node.matches(selector)) mark(node);
      node.querySelectorAll<HTMLElement>(selector).forEach(mark);
    }
    // Detached portals should not be retained by the ownership bookkeeping.
    owned.forEach(node => { if (!node.isConnected) owned.delete(node); });
  });
  observer.observe(document.body, { childList: true, subtree: true });
  return () => {
    observer.disconnect();
    events.forEach(event => document.removeEventListener(event, interaction, true));
    owned.forEach(node => { if (node.dataset.ketcherOwner === session) delete node.dataset.ketcherOwner; });
    owned.clear();
  };
}
