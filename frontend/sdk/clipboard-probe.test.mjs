import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import { clearForClipboardPaste, pasteClipboardAt } from '../scripts/ketcher-clipboard-probe.mjs';

function browserFixture({ iframe = false, failDown = false } = {}) {
  const top = new EventTarget(), frame = new EventTarget();
  const atoms = new Map([[0, {}], [1, {}], [2, {}]]);
  let selected = [], commandsEnabled = false;
  const editor = { _tool: {}, struct: () => ({ atoms }), selection: () => ({ atoms: selected }) };
  const events = [], pending = [];
  let initializedAction = null, action = 0;
  top.__probeEditor = () => ({ editor, setMolecule: () => { throw new Error('unexpected asynchronous import'); } });
  const document = { querySelector: selector => selector.includes('iframe')
    ? iframe ? { contentWindow: frame } : null : { disabled: !commandsEnabled } };
  top.document = frame.document = document;
  const context = vm.createContext({ window: top, document });
  const win = iframe ? frame : top;
  function initialize() {
    const tool = editor._tool, current = tool.action;
    pending.push(() => {
      initializedAction = current;
      // No overlapping atoms is a valid initialized result.
      tool.mergeItems = null;
      win.dispatchEvent(new Event('itemsToMergeInitializationComplete'));
      events.push('ready');
    });
  }
  const page = {
    evaluate: async fn => structuredClone(await vm.runInContext(`(${fn.toString()})()`, context)),
    waitForFunction: async fn => {
      for (let i = 0; i < 10; i++) {
        if (await page.evaluate(fn)) return;
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
    } },
    mouse: {
      move: async (x, y) => {
        events.push(['move', x, y]);
        if (editor._tool.struct) {
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
  return { page, editor, events, top };
}

test('clear deletes the copied selection and waits without starting an import', async () => {
  const { page, editor, events } = browserFixture();
  assert.equal(editor.selection().atoms.length, 0); // SDK copy deselects.
  await clearForClipboardPaste(page);
  assert.equal(editor.struct().atoms.size, 0);
  assert.deepEqual(events, ['select-all', 'delete-selected']);
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
  assert.deepEqual(events, [['move', 101, 100], 'paste-preview', 'ready', ['move', 100, 100], 'ready', 'down', 'committed']);
  assert.equal(top.__probePaste, undefined);
});

test('paste removes its probe after a pointer failure', async () => {
  const { page, top } = browserFixture({ failDown: true });
  await assert.rejects(pasteClipboardAt(page, { x: 100, y: 100 }), /pointer failed/);
  assert.equal(top.__probePaste, undefined);
});
