import test from 'node:test';
import assert from 'node:assert/strict';
import { MacroController, containsMacro } from './macro-controller.mjs';

globalThis.window = {};
function fixture() {
  const events = [];
  const model = { _type: 0, disposed: false,
    switchToMacromolecules() { events.push('to-macro'); this._type = 1; },
    switchToMicromolecules() { events.push('to-micro'); this._type = 0; },
    destroy() { this.disposed = true; } };
  const controller = new MacroController({
    load: generation => queueMicrotask(() => controller.initialized(model, generation)),
    show: async value => { events.push(value ? 'show-macro' : 'show-micro'); },
    fail: error => events.push(error.message)
  });
  controller.attach({ editor: { focusCliparea() {} }, structService: {} });
  return { controller, model, events };
}
test('same visible mode acknowledges a command after import changed the owning model', async () => {
  const { controller, model, events } = fixture();
  await controller.ensure();
  model._type = 1;
  await controller.requestMode(false);
  assert.equal(model._type, 0);
  assert.deepEqual(events, ['to-micro', 'show-micro']);
  await controller.requestMode(false);
  assert.deepEqual(events, ['to-micro', 'show-micro', 'show-micro']);
  controller.dispose();
});
test('opposite concurrent commands preserve order and the document owner', async () => {
  const { controller, model, events } = fixture();
  await Promise.all([controller.requestMode(true), controller.requestMode(false), controller.requestMode(true)]);
  assert.equal(model._type, 1);
  assert.deepEqual(events, ['to-macro','show-macro','to-micro','show-micro','to-macro','show-macro']);
  controller.dispose();
});
test('an unused hidden Macro never overwrites the micro document', async () => {
  const { controller, events } = fixture();
  await controller.ensure();
  assert.deepEqual(events, []);
  controller.dispose();
});
test('failed load retries with a new generation; old initialization cannot take ownership', async () => {
  const { controller, model } = fixture();
  controller.load = () => Promise.reject(new Error('network'));
  await assert.rejects(controller.ensure(), /network/);
  controller.load = generation => queueMicrotask(() => controller.initialized(model, generation));
  await controller.ensure();
  const late = { destroy() { this.destroyed = true; } };
  controller.initialized(late, 1);
  assert.equal(late.destroyed, true);
  assert.equal(controller.macro, model);
  controller.dispose();
});
test('retirement settles pending initialization and mode commands', async () => {
  const { controller } = fixture();
  controller.load = () => new Promise(() => {});
  const initialized = controller.ensure();
  const mode = controller.requestMode(true);
  controller.dispose();
  await assert.rejects(initialized, { name: 'AbortError' });
  await assert.rejects(mode, { name: 'AbortError' });
});
test('KET templates and ambiguous entities require Macro; ordinary text annotations do not', () => {
  assert.equal(containsMacro('{"root":{"nodes":[]},"t":{"type":"monomerGroupTemplate"}}'), true);
  assert.equal(containsMacro('{"root":{"nodes":[]},"t":{"type":"ambiguousMonomer"}}'), true);
  assert.equal(containsMacro('{"root":{"nodes":[{"type":"text","data":"monomer"}]}}'), false);
});
