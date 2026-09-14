// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ownNativePopups } from './nativePopups';

const cleanups: Array<() => void> = [];
afterEach(() => {
  cleanups.splice(0).forEach(cleanup => cleanup());
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

function fixture() {
  const host = document.createElement('main');
  const root = document.createElement('div');
  const button = document.createElement('button');
  root.append(button);
  host.append(root);
  document.body.append(host);
  vi.spyOn(root, 'getClientRects').mockReturnValue([{}] as unknown as DOMRectList);
  const cleanup = ownNativePopups(root, 'editor-session');
  cleanups.push(cleanup);
  return { host, root, button, cleanup };
}

function popup(id = '') {
  const portal = document.createElement('div');
  portal.className = 'MuiModal-root';
  portal.id = 'outer-' + id;
  const list = document.createElement('ul');
  list.id = id;
  const option = document.createElement('li');
  option.tabIndex = -1;
  list.append(option);
  portal.append(list);
  document.body.append(portal);
  return { portal, option };
}

describe('native popup ownership', () => {
  it('retains the editor activation through MUI background hiding and portal autofocus', async () => {
    const { host, button } = fixture();
    button.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true }));
    host.setAttribute('aria-hidden', 'true');
    const { portal, option } = popup();
    option.focus(); // MUI focuses before MutationObserver delivers the added node.
    await Promise.resolve();
    expect(portal.dataset.ketcherOwner).toBe('editor-session');

    option.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    const nested = popup('nested');
    nested.option.focus();
    await Promise.resolve();
    expect(nested.portal.dataset.ketcherOwner).toBe('editor-session');
  });

  it('resolves the referenced nested listbox instead of the outer portal ID', async () => {
    const { button } = fixture();
    button.setAttribute('aria-controls', 'other-list\tsettings-list');
    const { portal } = popup('settings-list');
    await Promise.resolve();
    expect(portal.dataset.ketcherOwner).toBe('editor-session');
  });

  it('does not claim a host popup after an outside activation or focus move', async () => {
    const { button } = fixture();
    const outside = document.createElement('button');
    document.body.append(outside);
    button.click();
    outside.click();
    const first = popup('host-click');
    first.option.focus();
    await Promise.resolve();
    expect(first.portal.hasAttribute('data-ketcher-owner')).toBe(false);

    button.click();
    outside.focus();
    const second = popup('host-focus');
    await Promise.resolve();
    expect(second.portal.hasAttribute('data-ketcher-owner')).toBe(false);
  });

  it.each(['inert', 'hidden'])('does not claim portals for a %s editor', async attribute => {
    const { host, button } = fixture();
    button.click();
    host.setAttribute(attribute, '');
    button.setAttribute('aria-controls', 'hidden-list');
    const { portal, option } = popup('hidden-list');
    option.focus();
    await Promise.resolve();
    expect(portal.hasAttribute('data-ketcher-owner')).toBe(false);
  });

  it('does not steal another session and removes only its own labels on disposal', async () => {
    const { button, cleanup } = fixture();
    button.click();
    const own = popup('own');
    own.option.focus();
    button.click();
    const other = popup('other');
    other.portal.dataset.ketcherOwner = 'another-session';
    other.option.focus();
    await Promise.resolve();
    expect(own.portal.dataset.ketcherOwner).toBe('editor-session');
    expect(other.portal.dataset.ketcherOwner).toBe('another-session');
    cleanup();
    expect(own.portal.hasAttribute('data-ketcher-owner')).toBe(false);
    expect(other.portal.dataset.ketcherOwner).toBe('another-session');
    button.click();
    const retired = popup('retired');
    retired.option.focus();
    await Promise.resolve();
    expect(retired.portal.hasAttribute('data-ketcher-owner')).toBe(false);
  });
});
