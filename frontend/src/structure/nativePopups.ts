/** Associate body portals only when an editor control opened/anchors them. */
export function ownNativePopups(root: HTMLElement, session: string) {
  const owned = new Set<HTMLElement>();
  const selector = '.MuiModal-root, .MuiPopper-root, .MuiTooltip-popper';
  let opener: Element | null = null;
  const displayed = () => root.isConnected && root.getClientRects().length > 0 && !root.closest('[inert], [hidden]');
  const belongs = (target: Element) => root.contains(target) ||
    target.closest('[data-ketcher-owner]')?.getAttribute('data-ketcher-owner') === session;
  const mark = (node: HTMLElement) => {
    if (root.contains(node) || node.closest('[data-ketcher-owner]') || !displayed()) return;
    // MUI's modal manager hides #root and moves focus into the portal before
    // this observer runs. Keep the activation's owner through that focus move.
    // aria-controls commonly names the nested listbox, not the modal's own ID.
    const anchors = [root, ...owned].flatMap(parent => [...parent.querySelectorAll('[aria-controls], [aria-describedby]')]);
    const anchored = anchors.some(anchor =>
      [anchor.getAttribute('aria-controls'), anchor.getAttribute('aria-describedby')].some(value =>
        value?.split(/\s+/).some(id => {
          const target = id ? document.getElementById(id) : null;
          return target && (target === node || node.contains(target));
        })));
    if (!(opener?.isConnected && belongs(opener)) && !anchored) return;
    node.dataset.ketcherOwner = session;
    owned.add(node);
    opener = null;
  };
  const interaction = (event: Event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    if (event.type === 'focusin') {
      if (belongs(target)) return;
      const popup = target.closest<HTMLElement>(selector);
      if (popup) mark(popup);
      if (!belongs(target)) opener = null;
      return;
    }
    // An owned modal remains interactive while MUI hides its host root.
    const inOwnedPopup = target.closest('[data-ketcher-owner]')?.getAttribute('data-ketcher-owner') === session;
    opener = displayed() && belongs(target) && (inOwnedPopup || !root.closest('[aria-hidden="true"]')) ? target : null;
  };
  const events = ["pointerdown", "click", "keydown", "focusin"];
  events.forEach(event => document.addEventListener(event, interaction, true));
  const observer = new MutationObserver(records => {
    for (const record of records) for (const node of record.addedNodes) {
      if (!(node instanceof HTMLElement)) continue;
      if (node.matches(selector)) mark(node);
      node.querySelectorAll<HTMLElement>(selector).forEach(mark);
    }
    // Detached portals should not be retained by the ownership bookkeeping.
    owned.forEach(node => { if (!node.isConnected) owned.delete(node); });
  });
  observer.observe(document.body, { childList: true, subtree: true });
  return () => {
    opener = null;
    observer.disconnect();
    events.forEach(event => document.removeEventListener(event, interaction, true));
    owned.forEach(node => { if (node.dataset.ketcherOwner === session) delete node.dataset.ketcherOwner; });
    owned.clear();
  };
}
