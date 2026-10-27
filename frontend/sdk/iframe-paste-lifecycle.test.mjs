import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';
import { verifyIframePatch } from '../scripts/patch-ketcher-iframe.mjs';

const recipe = await verifyIframePatch();
const source = await readFile(new URL('../' + recipe.file, import.meta.url), 'utf8');
const callbackStart = source.indexOf('KM=(0,p.debounce)(') + 'KM=(0,p.debounce)('.length;
const callback = source.slice(callbackStart, source.indexOf(',50),qM=', callbackStart));
const cancelAnchor = source.indexOf('this.editor._tool===this&&KM.cancel()');
const cancelStart = source.lastIndexOf('{key:"cancel",value:', cancelAnchor) + '{key:"cancel",value:'.length;
const cancel = source.slice(cancelStart, source.indexOf('},{key:"mouseleave"', cancelStart));

function fixture() {
  const calls = [];
  let pending;
  const editor = {
    render: { ctab: {}, update: () => calls.push('update') },
    hover: (...args) => calls.push(['hover', ...args])
  };
  const action = { perform: ctab => { assert.equal(ctab, editor.render.ctab); calls.push('rollback'); } };
  const tool = { editor, action, setMergeItems: value => calls.push(['merge', value]) };
  editor._tool = tool;
  const KM = { cancel: () => { pending = undefined; calls.push('cancel-timer'); } };
  const context = vm.createContext({ KM, N: {
    getItemsToFuse: () => { calls.push('read-preview'); return null; },
    getHoverToFuse: value => value,
    notifyItemsToMergeInitializationComplete: () => calls.push('ready')
  } });
  const run = vm.runInContext(`(${callback})`, context);
  tool.cancel = vm.runInContext(`(${cancel})`, context);
  return { calls, editor, tool, schedule: () => { pending = () => run(editor, {}, tool); },
    flush: () => { const work = pending; pending = undefined; work?.(); },
    captured: () => () => run(editor, {}, tool) };
}

test('the shipped iframe patch reverses to the exact pinned upstream bundle', () => {
  assert.equal(recipe.sdkVersion, '3.7.0');
  assert.equal(recipe.replacements.length, 2);
});

test('actual paste cancel clears its timer before rolling back preview atoms', () => {
  const f = fixture();
  f.schedule();
  f.tool.cancel();
  f.flush();
  assert.equal(f.tool.action, undefined);
  assert.deepEqual(f.calls, ['cancel-timer', ['hover', null], 'rollback', 'update']);
});

test('an already captured callback cannot read a canceled or retired preview', () => {
  for (const retire of [false, true]) {
    const f = fixture(), callback = f.captured();
    if (retire) f.editor._tool = {}; else delete f.tool.action;
    callback();
    assert.deepEqual(f.calls, []);
  }
});

test('a retired paste cannot cancel its successor timer', () => {
  const f = fixture();
  f.editor._tool = {};
  f.tool.cancel();
  assert.ok(!f.calls.includes('cancel-timer'));
});

test('the same paste can reenter with a new preview and publish valid empty merge candidates', () => {
  const f = fixture();
  f.tool.cancel();
  f.calls.length = 0;
  f.tool.action = {};
  f.schedule();
  f.flush();
  assert.deepEqual(f.calls, ['read-preview', ['hover', null, undefined], ['merge', null], 'ready']);
});
