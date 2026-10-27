import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import { clearForClipboardPaste, pasteClipboardAt } from '../scripts/ketcher-clipboard-probe.mjs';

function browserFixture({ iframe = false, failDown = false, cancelBeforeInitialReady = false } = {}) {
  const top = new EventTarget(), frame = new EventTarget();
  const atoms = new Map([[0, {}], [1, {}], [2, {}]]);
  const draft = { value: 'CCN', disabled: false };
  let selected = [], commandsEnabled = false;
  const editor = { _tool: {}, struct: () => ({ atoms }), selection: () => ({ atoms: selected }),
    render: { clientArea: { contains: () => true } } };
  const events = [], pending = [];
  let initializedAction = null, action = 0;
  top.__probeEditor = () => ({ editor, setMolecule: () => { throw new Error('unexpected asynchronous import'); } });
  const document = { querySelector: selector => selector.includes('iframe')
    ? iframe ? { contentWindow: frame, getBoundingClientRect: () => ({ x: 0, y: 0, width: 1000 }) } : null
    : selector.includes('aria-label') ? draft : { disabled: !commandsEnabled },
    elementFromPoint: () => ({ nodeName: 'svg' }) };
  top.innerWidth = frame.innerWidth = 1000;
  top.requestAnimationFrame = frame.requestAnimationFrame = callback => { events.push('frame'); callback(0); };
  top.document = frame.document = document;
  const context = vm.createContext({ window: top, document });
  const win = iframe ? frame : top;
  function cancelPreview() {
    delete editor._tool.action;
    atoms.clear();
    pending.length = 0;
    events.push('cancel-preview');
  }
  function initialize() {
    const tool = editor._tool, current = tool.action;
    pending.length = 0; // The real SDK debounce replaces the previous move.
    pending.push(() => {
      initializedAction = current;
      // No overlapping atoms is a valid initialized result.
      tool.mergeItems = null;
      win.dispatchEvent(new Event('itemsToMergeInitializationComplete'));
      events.push('ready');
    });
  }
  const page = {
    locator: selector => selector.includes('iframe') ? { count: async () => Number(iframe) } : clearLocator(false, selector),
    frameLocator: () => ({ locator: selector => clearLocator(true, selector) }),
    evaluate: async (fn, arg) => structuredClone(await vm.runInContext(`(${fn.toString()})(${JSON.stringify(arg) ?? 'undefined'})`, context)),
    waitForFunction: async (fn, arg) => {
      for (let i = 0; i < 10; i++) {
        if (await page.evaluate(fn, arg)) return;
        // A deterministic delayed SDK callback: a truthy preview predicate
        // returns before this runs, while an event predicate must wait for it.
        pending.shift()?.();
      }
      throw new Error('condition did not become true');
    },
    keyboard: { press: async key => {
      if (key === 'Control+a') {
        selected = [...atoms.keys()];
        pending.push(() => { commandsEnabled = true; });
        events.push('select-all');
        return;
      }
      if (key === 'Delete') {
        assert.ok(commandsEnabled && selected.length === atoms.size);
        events.push('delete-selected');
        pending.push(() => atoms.clear());
        return;
      }
      assert.equal(key, 'Control+v');
      editor._tool = { struct: { atoms: new Map([[0, {}], [1, {}], [2, {}]]) }, action: { id: ++action } };
      events.push('paste-preview');
      initialize();
      if (iframe) top.dispatchEvent(new Event('itemsToMergeInitializationComplete'));
      if (cancelBeforeInitialReady) cancelPreview();
    } },
    mouse: {
      move: async (x, y) => {
        events.push(['move', x, y]);
        if (editor._tool.struct) {
          if (x === 1 && y === 1) { cancelPreview(); return; }
          for (const id of editor._tool.struct.atoms.keys()) atoms.set(id, {});
          editor._tool.mergeItems = null;
          editor._tool.action = { id: ++action };
          initialize();
        }
      },
      down: async () => {
        assert.equal(initializedAction, editor._tool.action, 'paste committed before move initialization');
        if (failDown) throw new Error('pointer failed');
        events.push('down');
      },
      up: async () => { editor._tool = {}; events.push('committed'); }
    }
  };
  function clearLocator(inFrame, selector) {
    assert.equal(inFrame, iframe, 'clear action must use the active editor window');
    assert.equal(selector, '[data-testid="clear-canvas"]:visible:enabled');
    return { click: async () => {
      events.push('clear-canvas');
      draft.disabled = true;
      pending.push(() => atoms.clear());
      // Host publication happens after the synchronous SDK graph clear.
      pending.push(() => { draft.value = ''; draft.disabled = false; events.push('host-published-empty'); });
    } };
  }
  return { page, editor, events, top, draft };
}

for (const iframe of [false, true]) test(`native clear waits for the graph and host publication (${iframe ? 'iframe' : 'native'})`, async () => {
  const { page, editor, events, draft } = browserFixture({ iframe });
  assert.equal(editor.selection().atoms.length, 0); // SDK copy deselects.
  await clearForClipboardPaste(page);
  assert.equal(editor.struct().atoms.size, 0);
  assert.equal(draft.value, '');
  assert.equal(draft.disabled, false);
  assert.deepEqual(events, ['clear-canvas', 'host-published-empty']);
});

test('a truthy paste preview permits commit before its deferred initialization', async () => {
  const { page, editor, events } = browserFixture();
  await page.keyboard.press('Control+v');
  await page.waitForFunction(() => Boolean(window.__probeEditor().editor._tool?.struct?.atoms));
  assert.equal(editor.struct().atoms.size, 3);
  assert.ok(!events.includes('ready'));
  await page.mouse.move(100, 100); // the implicit move in mouse.click
  await assert.rejects(page.mouse.down(), /before move initialization/);
});

for (const iframe of [false, true]) test(`paste waits for initial and target readiness (${iframe ? 'iframe' : 'native'})`, async () => {
  const { page, events, top } = browserFixture({ iframe });
  assert.deepEqual(await pasteClipboardAt(page, { x: 100, y: 100 }), { readyEvents: 2, committed: true });
  assert.deepEqual(events, [['move', 1, 1], 'paste-preview', ['move', 101, 100], 'ready',
    'frame', ['move', 100, 100], 'ready', 'down', 'committed']);
  assert.equal(top.__probePaste, undefined);
});

for (const iframe of [false, true]) test(`paste enters with a new ready action after initial cancellation (${iframe ? 'iframe' : 'native'})`, async () => {
  const { page, editor } = browserFixture({ iframe, cancelBeforeInitialReady: true });
  assert.deepEqual(await pasteClipboardAt(page, { x: 100, y: 100 }), { readyEvents: 2, committed: true });
  assert.equal(editor.struct().atoms.size, 3);
});

test('paste removes its probe after a pointer failure', async () => {
  const { page, top } = browserFixture({ failDown: true });
  await assert.rejects(pasteClipboardAt(page, { x: 100, y: 100 }), /pointer failed/);
  assert.equal(top.__probePaste, undefined);
});

for (const cancelBeforeInitialReady of [false, true]) test(`iframe paste recovers a canceled preview (already canceled: ${cancelBeforeInitialReady})`, async () => {
  const { page, editor, events, top } = browserFixture({ iframe: true, cancelBeforeInitialReady });
  assert.deepEqual(await pasteClipboardAt(page, { x: 100, y: 100 }, { exerciseCancel: true }),
    { readyEvents: 3, committed: true, canceledPreviewResumed: true });
  assert.equal(editor.struct().atoms.size, 3);
  assert.ok(events.includes('cancel-preview'));
  const cancel = events.lastIndexOf('cancel-preview');
  assert.ok(events.slice(0, cancel).includes('frame'), 'real entry must finish before deliberate leave');
  assert.equal(events.slice(cancel + 1).filter(event => event === 'ready').length, 2);
  assert.deepEqual(events.filter(Array.isArray), [['move', 1, 1], ['move', 101, 100],
    ['move', 1, 1], ['move', 101, 100], ['move', 100, 100]]);
  assert.equal(top.__probePaste, undefined);
});
